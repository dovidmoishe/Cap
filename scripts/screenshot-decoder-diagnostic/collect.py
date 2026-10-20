import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def run_json(command, log_prefix):
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    log_prefix.with_suffix(".stdout.log").write_text(result.stdout)
    log_prefix.with_suffix(".stderr.log").write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(f"Image helper exited {result.returncode}; inspect {log_prefix}")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    inputs = output / "input"
    images = sorted(inputs.rglob("*.png"))
    if len(images) != 5 or sum(path.name == "original.png" for path in images) != 2:
        raise ValueError("Expected five evidence PNGs, including two original captures")
    results = []
    for index, image in enumerate(images):
        original = run_json([str(output / "original-checker"), "--inspect-image", str(image)], output / f"image-{index}-original")
        diagnostic = run_json([str(output / "inspect"), str(image)], output / f"image-{index}-diagnostic")
        report = {"path": image.relative_to(inputs).as_posix(),
                  "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                  "originalChecker": original, "diagnostic": diagnostic}
        (output / f"image-{index}.json").write_text(json.dumps(report, indent=2))
        results.append(report)
        (output / "partial-results.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(report, indent=2), flush=True)
        checks = diagnostic["deviceRGBChecks"]
        if len(checks) != 9:
            raise ValueError(f"Incomplete device-RGB inspection: {image}")
        if all(check["passed"] for check in checks) != original["fixtureMatches"]:
            raise ValueError(f"Diagnostic predicates disagree with the original checker: {image}")
    summary = {"sourceRun": 39415518245, "purpose": "Image-decoder diagnosis only; no Cap build or benchmark",
               "imageCount": len(results), "images": results}
    (output / "diagnostic.json").write_text(json.dumps(summary, indent=2))
    text = "\n".join([
        "## Screenshot image-decoder diagnostic", "",
        "Existing images from run 39415518245. No Cap build, capture, or timing run was performed.",
        "A successful job means the diagnostic ran, not that the original smoke test passed.", "",
        "```json", json.dumps(summary, indent=2), "```", "",
    ])
    (output / "summary.md").write_text(text)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as stream:
            stream.write(text)


if __name__ == "__main__":
    main()
