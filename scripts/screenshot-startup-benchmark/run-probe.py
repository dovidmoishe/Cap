import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    output = Path(sys.argv[1]).resolve()
    log = output / "probe.log"
    try:
        with log.open("w") as stream:
            result = subprocess.run(
                [str(output / "runner-probe"), str(output)],
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=90,
                check=False,
            )
        code = result.returncode
    except subprocess.TimeoutExpired:
        code = 124
        with log.open("a") as stream:
            stream.write("\nRunner probe exceeded 90 seconds; capabilities are unverified.\n")
    except OSError as error:
        code = 126
        with log.open("a") as stream:
            stream.write(f"\nCannot execute runner probe: {error}\n")
    print(log.read_text())
    report_path = output / "probe.json"
    try:
        report = json.loads(report_path.read_text()) if report_path.exists() else {}
    except (OSError, json.JSONDecodeError) as error:
        report = {"reportReadError": str(error)}
    passed = code == 0 and report.get("allChecksPassed") is True
    text = "\n".join(
        [
            "## Screenshot runner capability probe",
            "",
            f"Result: {'PASS' if passed else 'FAIL / UNVERIFIED'}",
            "",
            "This checks runner capabilities; it does not benchmark Cap or validate the PR.",
            "A pass still requires building and running Cap with its own capture permissions.",
            "",
            "```json",
            json.dumps(report, indent=2),
            "```",
            "",
        ]
    )
    (output / "summary.md").write_text(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write(text)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
