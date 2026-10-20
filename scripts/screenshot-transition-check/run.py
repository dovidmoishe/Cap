import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def load_smoke():
    path = Path(__file__).resolve().parents[1] / "screenshot-startup-benchmark/run-cap-smoke.py"
    spec = importlib.util.spec_from_file_location("transition_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_ready(directory, child):
    deadline = time.monotonic() + 10
    while not (directory / "ready.json").exists():
        if child.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("Recorder did not confirm its visible static pattern before Cap launch; inspect recorder.log")
        time.sleep(0.05)
    ready = json.loads((directory / "ready.json").read_text())
    if ready.get("pid") != child.pid or ready.get("patternVisible") is not True or ready.get("geometry", {}).get("backgroundLevel") != -1:
        raise ValueError("Recorder did not verify its owned background pattern below normal windows")
    return ready


def collect(package, output, fixture, recorder):
    smoke = load_smoke()
    metadata = smoke.load_package(package)
    manifest = json.loads((package / "package.json").read_text())
    revised = next(binary for binary in metadata["binaries"] if binary["variant"] == "revised")
    recordings = output / "recording"
    runs = output / "capture"
    recordings.mkdir(parents=True)
    runs.mkdir()
    capture = runs / "revised"
    child = None
    report = {"purpose": "Untimed preview-to-GPU visual evidence only", "status": "inconclusive",
              "benchmarkRun": 39488846038, "physicalDisplayVerified": False,
              "forkCommit": os.environ.get("GITHUB_SHA"), "forkRun": os.environ.get("GITHUB_RUN_ID"),
              "package": {key: manifest[key] for key in ("repository", "buildRun", "buildCommit", "platform")},
              "revisedExecutableSHA256": revised["sha256"],
              "sourceSnapshot": metadata["revised_snapshot"]}
    try:
        with (output / "recorder.log").open("w") as log:
            child = subprocess.Popen([str(recorder), "--record", str(recordings), str(capture)],
                                     stdout=log, stderr=subprocess.STDOUT)
            report["recorderReady"] = wait_ready(recordings, child)
            result = smoke.run_variant(package, runs, fixture, revised, capture_editor=False, desktop=True)
            report["capture"] = result
            if not result.get("passed"):
                raise RuntimeError(f"Cap capture failed: {result.get('error')}")
            child.wait(timeout=30)
            if child.returncode != 0:
                raise RuntimeError("Recorder did not finish cleanly; inspect recorder.log and recording.json")
        analysis = subprocess.run([sys.executable, str(Path(__file__).with_name("analyze.py")), str(recordings), "--capture", str(capture)],
                                  capture_output=True, text=True, timeout=60, check=False)
        (output / "analysis.stdout.log").write_text(analysis.stdout)
        (output / "analysis.stderr.log").write_text(analysis.stderr)
        if (recordings / "analysis.json").exists():
            report["analysis"] = json.loads((recordings / "analysis.json").read_text())
        analysis.check_returncode()
        report["status"] = "evidence-ready-for-review"
    except Exception as error:
        report["error"] = str(error)
    finally:
        smoke.terminate(child)
        (output / "transition.json").write_text(json.dumps(report, indent=2))
        lines = ["# Screenshot preview-to-GPU evidence", "", f"Status: {report['status']}", "",
                 "This is an untimed diagnostic using the saved revised release build. Recording adds load; these draw timings are not benchmark results.",
                 "A green collection job means timestamped evidence was collected with preview and post-GPU coverage. Manual visual review is still required.",
                 "The recorder samples a virtual macOS display; it cannot prove the absence of physical-display flicker or sub-frame events.",
                 "The background pattern stays below normal windows. All raw frames and metadata are retained. Whole-window difference metrics include UI chrome.", "",
                 "Inspect recording/transition-filmstrip.png, recording/frames/, and recording/analysis.json."]
        if "error" in report:
            lines.extend(["", f"Failure: {report['error']}"])
        text = "\n".join(lines) + "\n"
        (output / "report.md").write_text(text)
        print(text)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a") as stream:
                stream.write(text)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--recorder", type=Path, required=True)
    args = parser.parse_args()
    report = collect(args.package.resolve(), args.output.resolve(), args.fixture.resolve(), args.recorder.resolve())
    raise SystemExit(0 if report["status"] == "evidence-ready-for-review" else 1)


if __name__ == "__main__":
    main()
