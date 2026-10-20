import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


FILES = ["screenshot.rs", "screenshot_editor.rs", "app_windows.rs", "main.rs"]
HELPERS = r'''
static BENCH_CAPTURE: std::sync::Mutex<Option<std::time::Instant>> = std::sync::Mutex::new(None);

pub(crate) fn bench_capture_started() {
    *BENCH_CAPTURE.lock().unwrap() = Some(std::time::Instant::now());
    tracing::info!("CAP_BENCH_CAPTURE");
}

pub(crate) fn bench_signal(name: &str) {
    if let Ok(directory) = std::env::var("CAP_BENCH_IPC_DIR") {
        std::fs::write(std::path::Path::new(&directory).join(name), b"ready")
            .expect("writing benchmark IPC signal");
    }
}
'''
FIELDS = r'''
    bench_started: Option<std::time::Instant>,
    bench_preview_done: Rc<Cell<bool>>,
    bench_gpu_done: Rc<Cell<bool>>,
    bench_gpu_frame: bool,
'''
INIT = r'''
            bench_started: *BENCH_CAPTURE.lock().unwrap(),
            bench_preview_done: Rc::new(Cell::new(false)),
            bench_gpu_done: Rc::new(Cell::new(false)),
            bench_gpu_frame: false,
'''
PAINT = r'''
                                        .child(canvas(
                                            |bounds, _, _| bounds,
                                            move |_, bounds, window, _| {
                                                if window.paint_image(bounds, gpui::Corners::default(), frame.clone(), 0, false).is_ok()
                                                    && crate::platform::window_is_visible(window)
                                                    && let Some(started) = bench_started
                                                {
                                                    if !bench_preview_done.replace(true) {
                                                        window.on_next_frame(move |_, _| {
                                                            tracing::info!(kind = "preview", elapsed_ms = started.elapsed().as_secs_f64() * 1000.0, bounds = ?bounds, "CAP_BENCH_DRAW");
                                                        });
                                                        window.refresh();
                                                    }
                                                    if bench_is_gpu && !bench_gpu_done.replace(true) {
                                                        window.on_next_frame(move |window, cx| {
                                                            tracing::info!(kind = "gpu", elapsed_ms = started.elapsed().as_secs_f64() * 1000.0, bounds = ?bounds, "CAP_BENCH_DRAW");
                                                            bench_signal("gpu-drawn");
                                                            if let Some(native) = crate::platform::native_window(window) {
                                                                cx.spawn(async move |cx| {
                                                                    cx.background_executor().timer(std::time::Duration::from_secs(6)).await;
                                                                    crate::platform::close_native(&native);
                                                                }).detach();
                                                            }
                                                        });
                                                        window.refresh();
                                                    }
                                                }
                                            },
                                        ).size_full())
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Expected one instrumentation anchor, got {text.count(old)}: {old[:90]!r}")
    return text.replace(old, new, 1)


def instrument(files):
    result = dict(files)
    text = result["screenshot.rs"]
    text = replace_once(
        text,
        '    tracing::info!(kind = target.kind_str(), "taking a screenshot");',
        '    crate::screenshot_editor::bench_capture_started();\n    tracing::info!(kind = target.kind_str(), "taking a screenshot");',
    )
    text = replace_once(
        text,
        '    tracing::info!(path = %png.display(), "screenshot captured");',
        '    tracing::info!(path = %png.display(), "screenshot captured");\n    crate::screenshot_editor::bench_signal("capture-saved");',
    )
    result["screenshot.rs"] = text
    text = result["screenshot_editor.rs"]
    text = replace_once(text, "pub struct ScreenshotEditorWindow {", HELPERS + "\npub struct ScreenshotEditorWindow {" + FIELDS)
    text = replace_once(text, "        let mut this = Self {", "        let mut this = Self {" + INIT)
    text = replace_once(
        text,
        "        let frame = self.frame.clone();",
        "        let frame = self.frame.clone();\n        let bench_started = self.bench_started;\n        let bench_preview_done = self.bench_preview_done.clone();\n        let bench_gpu_done = self.bench_gpu_done.clone();\n        let bench_is_gpu = self.bench_gpu_frame;",
    )
    text = replace_once(text, "                                        .child(img(frame).size_full())", PAINT.rstrip())
    text = replace_once(
        text,
        "view.frame_arrived(image, rgba, size, window, cx)",
        "view.bench_gpu_frame = true;\n                    view.frame_arrived(image, rgba, size, window, cx)",
    )
    result["screenshot_editor.rs"] = text
    return result


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    script = Path(__file__).resolve()
    root = script.parents[2]
    source = root / "apps/desktop-gpui/src"
    snapshots = script.parent / "snapshots"
    manifest = json.loads((snapshots / "source-manifest.json").read_text())
    patch = snapshots / "revised.patch"
    patch_contents = patch.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(patch_contents).hexdigest() != manifest["patch_sha256"]:
        raise ValueError("Revised snapshot patch does not match its manifest")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    patch = output / "revised-source.patch"
    patch.write_bytes(patch_contents)
    originals = {name: (source / name).read_bytes() for name in FILES}
    baseline = {name: data.decode().replace("\r\n", "\n") for name, data in originals.items()}
    if {name: text_hash(text) for name, text in baseline.items()} != manifest["baseline_sources"]:
        raise ValueError("Checkout sources differ from the pinned upstream baseline")
    binaries = []
    try:
        for variant in ["baseline", "revised"]:
            for name, data in originals.items():
                (source / name).write_bytes(data)
            if variant == "revised":
                subprocess.run(["git", "apply", "--unidiff-zero", "--check", str(patch)], cwd=root, check=True)
                subprocess.run(["git", "apply", "--unidiff-zero", str(patch)], cwd=root, check=True)
            files = {name: (source / name).read_text(encoding="utf-8") for name in FILES}
            expected = manifest["baseline_sources" if variant == "baseline" else "revised_sources"]
            if {name: text_hash(text) for name, text in files.items()} != expected:
                raise ValueError(f"{variant} sources differ from snapshot hashes")
            instrumented = instrument(files)
            variant_output = output / variant
            variant_output.mkdir(exist_ok=True)
            for name, text in instrumented.items():
                (variant_output / name).write_text(text, encoding="utf-8")
                if not args.validate_only:
                    (source / name).write_text(text, encoding="utf-8")
            if args.validate_only:
                continue
            log_path = output / f"{variant}-build.log"
            subprocess.run(
                ["rustup", "run", "1.95.0", "rustfmt", "--edition", "2024",
                 str(source / "screenshot.rs"), str(source / "screenshot_editor.rs")], check=True,
            )
            for name in FILES:
                (variant_output / name).write_bytes((source / name).read_bytes())
            print(f"Building {variant}: release profile, Rust 1.95.0, two build jobs", flush=True)
            with log_path.open("w", encoding="utf-8") as log:
                with subprocess.Popen(
                    ["cargo", "+1.95.0", "build", "--release", "--locked", "--bin", "cap-gpui", "-j", "2"],
                    cwd=root / "apps/desktop-gpui", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                ) as process:
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        print(line, end="", flush=True)
                    code = process.wait()
            if code:
                raise RuntimeError(f"{variant} release build failed; see {log_path}")
            executable = variant_output / "cap-gpui"
            shutil.copy2(root / "apps/desktop-gpui/target/release/cap-gpui", executable)
            binaries.append({
                "variant": variant,
                "executable": str(executable),
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "uninstrumented_sources": expected,
            })
        (output / "binaries.json").write_text(json.dumps({
            "baseline_commit": manifest["baseline_commit"],
            "revised_snapshot": manifest,
            "profile": "release, unmodified Cargo profile, Rust 1.95.0",
            "binaries": binaries,
            "validate_only": args.validate_only,
        }, indent=2))
    finally:
        for name, data in originals.items():
            (source / name).write_bytes(data)
        if any((source / name).read_bytes() != data for name, data in originals.items()):
            raise RuntimeError("Source restoration failed")
        print("Restored all four source files byte-for-byte", flush=True)


if __name__ == "__main__":
    main()
