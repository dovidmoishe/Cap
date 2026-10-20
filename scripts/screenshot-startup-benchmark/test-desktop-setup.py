import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import time


spec = importlib.util.spec_from_file_location("desktop_smoke", Path(__file__).with_name("run-cap-smoke.py"))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def wait_json(path, children):
    deadline = time.monotonic() + 10
    while not path.exists():
        if any(child.poll() is not None for child in children) or time.monotonic() > deadline:
            raise RuntimeError(f"Native desktop setup check did not produce {path}; inspect helper logs")
        time.sleep(0.05)
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    fixture = args.fixture.resolve()
    output = args.output.resolve()
    helper_dir, target_dir = output / "helper", output / "target"
    helper_dir.mkdir(parents=True)
    target_dir.mkdir()
    children = []
    report = {"passed": False, "purpose": "Native desktop preparation self-check; no Cap build or timings"}
    try:
        with (output / "helper.log").open("w") as helper_log, (output / "target.log").open("w") as target_log:
            children.append(subprocess.Popen([str(fixture), "--run-desktop", str(helper_dir)], stdout=helper_log, stderr=subprocess.STDOUT))
            children.append(subprocess.Popen([str(fixture), "--test-window", str(target_dir)], stdout=target_log, stderr=subprocess.STDOUT))
            ready = wait_json(helper_dir / "fixture-ready.json", children)
            target_ready = wait_json(target_dir / "fixture-ready.json", children)
            report.update({"helper": ready, "target": target_ready})
            if ready.get("helperWindowCount") != 0 or ready.get("activeDisplay") is not True or target_ready.get("visible") is not True:
                raise ValueError("Expected a windowless desktop helper and visible stand-in main panel")
            pending = helper_dir / "cap-pid.pending"
            pending.write_text(str(children[1].pid))
            pending.replace(helper_dir / "cap-pid")
            hidden = wait_json(helper_dir / "desktop-hidden.json", children)
            report["hidden"] = hidden
            if hidden.get("ownerPID") != children[1].pid or hidden.get("hidden") is not True or hidden.get("visibleMainWindows") != 0 or hidden.get("stableHiddenMs", 0) < 250:
                raise ValueError("Stand-in main panel did not become stably hidden")
            if hidden.get("hideRequestCount") != 1 or type(hidden.get("hideReturned")) is not bool:
                raise ValueError("Expected one hide request with its return value retained as evidence")
            (target_dir / "unhide-request").write_text("ready")
            resumed = wait_json(target_dir / "resumed.json", children)
            report["resumed"] = resumed
            if resumed.get("hidden") is not False or resumed.get("visible") is not True or resumed.get("visibleWindowCount", 0) < 1:
                raise ValueError("Desktop helper interfered after acknowledging setup")
            report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        for child in children:
            smoke.terminate(child)
        (output / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
