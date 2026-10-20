import argparse
import itertools
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import time


def read_state(directory):
    path = directory / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def command(directory, value):
    pending = directory / "command.pending"
    pending.write_text(value)
    pending.replace(directory / "command")


def hidden(state):
    return state.get("windowListAvailable") is True and state.get("hidden") is True and state.get("visibleWindowCount") == 0


def visible(state):
    return state.get("windowListAvailable") is True and state.get("hidden") is False and state.get("visibleWindowCount", 0) > 0


def observe(directory, predicate, children, timeout=3, stable=0.3):
    deadline = time.monotonic() + timeout
    since = None
    state = {}
    while time.monotonic() < deadline:
        if any(child.poll() is not None for child in children):
            raise RuntimeError("An owned diagnostic process exited; inspect this case's logs")
        state = read_state(directory)
        now = time.monotonic()
        if predicate(state):
            if since is None:
                since = now
            if now - since >= stable:
                return {"matched": True, "state": state}
        else:
            since = None
        time.sleep(0.05)
    return {"matched": False, "state": state}


def bundle(binary, directory, identifier):
    contents = directory / "Contents"
    executable = contents / "MacOS" / "WindowProbe"
    executable.parent.mkdir(parents=True)
    shutil.copy2(binary, executable)
    with (contents / "Info.plist").open("wb") as output:
        plistlib.dump({"CFBundleExecutable": "WindowProbe", "CFBundleIdentifier": identifier,
                      "CFBundleName": "WindowProbe", "CFBundlePackageType": "APPL",
                      "CFBundleVersion": "1", "NSHighResolutionCapable": True}, output)
    return directory


def run_case(binary, directory, packaging, helper_policy, target_policy):
    directory.mkdir(parents=True)
    target_dir, helper_dir = directory / "target", directory / "helper"
    target_dir.mkdir()
    helper_dir.mkdir()
    report = {"packaging": packaging, "helperPolicy": helper_policy, "targetPolicy": target_policy}
    children = []
    target_pid = None
    try:
        with (directory / "target.log").open("w") as target_log, (directory / "helper.log").open("w") as helper_log:
            target_args = ["target", str(target_dir), target_policy]
            if packaging == "bundle":
                app = bundle(binary, directory / "Target.app", "dev.cap.fork.window-diagnostic.target")
                launch = ["/usr/bin/open", "-n", "-W", str(app), "--args", *target_args]
            else:
                launch = [str(binary), *target_args]
            children.append(subprocess.Popen(launch, stdout=target_log, stderr=subprocess.STDOUT))
            initial = observe(target_dir, lambda item: visible(item.get("self", {})), children, timeout=10)
            report["initial"] = initial
            target_pid = initial["state"].get("self", {}).get("pid")
            if not initial["matched"] or not isinstance(target_pid, int) or target_pid <= 1:
                raise RuntimeError("Target did not become visible with an identifiable PID")
            children.append(subprocess.Popen([str(binary), "helper", str(helper_dir), helper_policy, str(target_pid)],
                                             stdout=helper_log, stderr=subprocess.STDOUT))
            helper_ready = observe(helper_dir, lambda item: "self" in item, children, timeout=10)
            report["helperInitial"] = helper_ready
            if not helper_ready["matched"]:
                raise RuntimeError("Helper did not initialize")
            helper_state = helper_ready["state"]["self"]
            if helper_state.get("windowListAvailable") is not True or helper_state.get("visibleWindowCount") != 0:
                raise RuntimeError("Helper was not demonstrably windowless")
            command(helper_dir, "remote-hide")
            remote = observe(helper_dir, lambda item: item.get("lastCommand") == "remote-hide"
                             and hidden(item.get("target", {})), children)
            report["remoteHide"] = remote
            target_hidden = remote["matched"]
            if not target_hidden:
                command(target_dir, "self-hide")
                own = observe(target_dir, lambda item: item.get("lastCommand") == "self-hide"
                              and hidden(item.get("self", {})), children)
                report["selfHide"] = own
                target_hidden = own["matched"]
            if target_hidden:
                command(target_dir, "open-editor")
                reopened = observe(target_dir, lambda item: item.get("lastCommand") == "open-editor"
                                   and visible(item.get("self", {})), children)
                report["reopenByActivation"] = reopened
                if not reopened["matched"]:
                    command(target_dir, "explicit-unhide")
                    report["reopenExplicitly"] = observe(target_dir, lambda item: item.get("lastCommand") == "explicit-unhide"
                                                        and visible(item.get("self", {})), children)
            report["remoteMethodSupported"] = remote["matched"] and report.get("reopenByActivation", {}).get("matched") is True
            report["diagnosticComplete"] = True
    except Exception as error:
        report.update({"diagnosticComplete": False, "error": str(error)})
    finally:
        if target_pid is None:
            target_pid = read_state(target_dir).get("self", {}).get("pid")
        if isinstance(target_pid, int) and target_pid > 1:
            try:
                os.kill(target_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        (directory / "result.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases = []
    for packaging, helper_policy, target_policy in itertools.product(["raw", "bundle"], ["prohibited", "accessory"], ["regular", "accessory"]):
        name = f"{packaging}-{helper_policy}-{target_policy}"
        result = run_case(args.binary.resolve(), output / name, packaging, helper_policy, target_policy)
        cases.append(result)
        print(f"{name}: complete={result['diagnosticComplete']}, remote supported={result.get('remoteMethodSupported', False)}", flush=True)
        (output / "results.json").write_text(json.dumps({"purpose": "Native window diagnostic only; no Cap builds or timings",
                                                       "diagnosticComplete": len(cases) == 8 and all(case["diagnosticComplete"] for case in cases),
                                                       "cases": cases}, indent=2))
    raise SystemExit(0 if all(case["diagnosticComplete"] for case in cases) else 1)


if __name__ == "__main__":
    main()
