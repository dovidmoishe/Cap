import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def terminate(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def draw(log, kind):
    pattern = rf'^(\S+) .*CAP_BENCH_DRAW kind="{kind}" elapsed_ms=([0-9.]+) bounds=(.*)$'
    match = re.search(pattern, log, re.MULTILINE)
    if not match:
        return None
    return {"utc": match[1], "elapsed_ms": float(match[2]), "bounds": match[3]}


def inspect_image(fixture, path, output):
    result = subprocess.run(
        [str(fixture), "--inspect-image", str(path)], check=False, capture_output=True, text=True, timeout=15,
    )
    (output / "input-check.stdout.log").write_text(result.stdout)
    (output / "input-check.stderr.log").write_text(result.stderr)
    result.check_returncode()
    return json.loads(result.stdout)


def run_variant(package, output, fixture, binary, *, capture_editor=True):
    directory = output / binary["variant"]
    directory.mkdir()
    profile = directory / "profile"
    store = directory / "store.json"
    store.write_text(json.dumps({"general_settings": {
        "hasCompletedStartup": True, "hasCompletedOnboarding": True, "postScreenshotBehaviour": "openEditor",
    }}))
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CAP_GPUI_AUTO_", "CAP_BENCH_", "CAP_GPUI_SCREENSHOT_", "DYLD_"))}
    environment.update({
        "CAP_GPUI_APP_DATA_DIR": str(profile),
        "CAP_GPUI_TAURI_STORE": str(store),
        "CAP_GPUI_AUTO_HOTKEY": "screenshotDisplay",
        "CAP_BENCH_IPC_DIR": str(directory),
        "RUST_LOG": "warn,cap_gpui=info",
        "NO_COLOR": "1",
        "DYLD_FRAMEWORK_PATH": str(package / "runtime/Frameworks"),
        "DYLD_LIBRARY_PATH": str(package / "runtime/lib") + os.pathsep + str(package / "runtime/onnxruntime"),
        "ORT_DYLIB_PATH": str(package / "runtime/onnxruntime/libonnxruntime.dylib"),
    })
    if capture_editor:
        environment["DYLD_PRINT_LIBRARIES"] = "1"
    executable = package / binary["executable"]
    if hashlib.sha256(executable.read_bytes()).hexdigest() != binary["sha256"]:
        raise ValueError("Executable hash differs from build provenance")
    child = None
    scene = None
    record = {"variant": binary["variant"], "passed": False, "editorImageRecording": capture_editor}
    try:
        with (directory / "fixture.log").open("w") as fixture_log, (directory / "cap.log").open("w") as cap_log:
            scene = subprocess.Popen([str(fixture), "--run", str(directory)], stdout=fixture_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 15
            while not (directory / "fixture-ready.json").exists():
                if scene.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Fixed display fixture did not become ready")
                time.sleep(0.05)
            ready = json.loads((directory / "fixture-ready.json").read_text())
            if ready.get("visible") is not True:
                raise RuntimeError("Fixture is not visible")
            child = subprocess.Popen(
                [str(executable)], cwd=package, env=environment,
                stdout=cap_log, stderr=subprocess.STDOUT,
            )
            record["ownedPID"] = child.pid
            deadline = time.monotonic() + 90
            requested = False
            while True:
                log = ANSI.sub("", (directory / "cap.log").read_text(errors="replace"))
                preview = draw(log, "preview")
                gpu = draw(log, "gpu")
                closed = "screenshot editor window closed" in log
                if gpu and capture_editor and not requested:
                    (directory / "editor-pid").write_text(str(child.pid))
                    requested = True
                if preview and gpu and closed:
                    break
                if child.poll() is not None:
                    raise RuntimeError(f"Cap exited before the smoke check completed: {child.returncode}")
                if scene.poll() is not None:
                    raise RuntimeError("Fixture exited before recording evidence")
                if time.monotonic() > deadline:
                    raise RuntimeError("Timed out waiting for screenshot draw and normal editor close; inspect cap.log")
                time.sleep(0.05)
            if not (directory / "capture-saved").exists():
                raise RuntimeError("Capture completion signal missing")
            hidden = json.loads((directory / "fixture-hidden.json").read_text())
            preview_time = datetime.fromisoformat(preview["utc"].replace("Z", "+00:00"))
            hidden_time = datetime.fromisoformat(hidden["hiddenUTC"].replace("Z", "+00:00"))
            exposure_margin = (preview_time - hidden_time).total_seconds() * 1000
            if exposure_margin < 25:
                raise RuntimeError(f"Fixture hidden too late to verify visible image draw: {exposure_margin:.1f}ms margin")
            captures = list(profile.glob("screenshots/*/original.png"))
            if len(captures) != 1:
                raise RuntimeError(f"Expected one saved PNG, found {len(captures)}")
            capture = captures[0]
            image = inspect_image(fixture, capture, directory)
            record.update({"input": image, "inputPNG": capture.relative_to(output).as_posix(),
                           "inputPNGsha256": hashlib.sha256(capture.read_bytes()).hexdigest()})
            if image.get("fixtureMatches") is not True or image.get("containsFixtureColors") is not True:
                raise RuntimeError("Saved screenshot does not contain the fixed full-display scene")
            if (image["width"], image["height"]) != (ready["pixelWidth"], ready["pixelHeight"]):
                raise RuntimeError("Saved screenshot dimensions differ from the fixture display")
            editor = None
            if capture_editor:
                editor = json.loads((directory / "editor-evidence.json").read_text())
                record["editor"] = editor
                if editor.get("containsFixtureColors") is not True or editor.get("ownerPID") != child.pid:
                    raise RuntimeError(f"Native editor image did not verify the captured scene: {editor}")
            if preview["bounds"] != gpu["bounds"]:
                raise RuntimeError("Preview and GPU image bounds differ")
            record.update({
                "passed": True,
                "diagnosticPreview": preview,
                "diagnosticGPU": gpu,
                "fixtureHiddenBeforePreviewMs": exposure_margin,
                "input": image,
                "inputPNGsha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                "editor": editor,
                "normalEditorCloseObserved": True,
            })
    except Exception as error:
        record["error"] = str(error)
    finally:
        terminate(child)
        terminate(scene)
        (directory / "result.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2), flush=True)
    return record


def load_package(package):
    metadata = json.loads((package / "builds/binaries.json").read_text())
    if metadata.get("validate_only") or len(metadata["binaries"]) != 2:
        raise ValueError("Expected two compiled release binaries")
    scripts = Path(__file__).resolve().parent
    if metadata["revised_snapshot"] != json.loads((scripts / "snapshots/source-manifest.json").read_text()):
        raise ValueError("Runtime package does not match the current production snapshot")
    if metadata["instrumentation_sha256"] != hashlib.sha256((scripts / "build-cap.py").read_text().encode()).hexdigest():
        raise ValueError("Runtime package uses different measurement instrumentation")
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    package = args.package.resolve()
    metadata = load_package(package)
    fixture = output / "cap-fixture"
    runs = [run_variant(package, output, fixture, binary) for binary in metadata["binaries"]]
    passed = all(run["passed"] for run in runs)
    comparison = None
    if passed:
        try:
            result = subprocess.run(
                [str(fixture), "--compare-images", *[str(output / run["inputPNG"]) for run in runs]],
                capture_output=True, text=True, timeout=60, check=False,
            )
            (output / "input-comparison.stdout.log").write_text(result.stdout)
            (output / "input-comparison.stderr.log").write_text(result.stderr)
            result.check_returncode()
            comparison = json.loads(result.stdout)
        except (subprocess.SubprocessError, ValueError, OSError) as error:
            comparison = {"error": str(error)}
        (output / "input-comparison.json").write_text(json.dumps(comparison, indent=2))
        passed = comparison.get("interiorMatches") is True
    manifest = json.loads((package / "package.json").read_text())
    summary = {"passed": passed, "purpose": "Diagnostic smoke run; timings are not benchmark results",
               "package": {key: manifest[key] for key in ["repository", "buildCommit", "buildRun", "platform"]},
               "inputComparison": comparison, "runs": runs}
    (output / "smoke.json").write_text(json.dumps(summary, indent=2))
    text = "\n".join([
        "## Cap screenshot editor macOS smoke run", "",
        f"Result: {'PASS' if passed else 'FAIL / UNVERIFIED'}", "",
        "Baseline and revised release binaries; one fresh-process capture each.",
        "Diagnostic image recording is enabled. These timings are not a performance comparison.",
        "Input RGB must match exactly after excluding a fixed 16-pixel border; all border differences are reported.",
        "No claim is made about preview flashing, repeated captures, exports, or clipboard behavior.", "",
        "```json", json.dumps(summary, indent=2), "```", "",
    ])
    (output / "smoke.md").write_text(text)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as stream:
            stream.write(text)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
