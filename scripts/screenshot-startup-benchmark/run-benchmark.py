import argparse
import csv
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time


spec = importlib.util.spec_from_file_location("screenshot_smoke", Path(__file__).with_name("run-cap-smoke.py"))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def schedule(samples, warmups):
    for phase, count in [("warmup", warmups), ("measure", samples)]:
        for pair in range(count):
            yield phase, pair, ["baseline", "revised"] if pair % 2 == 0 else ["revised", "baseline"]


def validate_capture(record):
    if not record.get("passed") or record.get("editorImageRecording") is not False:
        raise ValueError(f"Invalid timed capture: {record.get('error', 'capture failed or editor recording enabled')}")
    preview = record["diagnosticPreview"]["elapsed_ms"]
    gpu = record["diagnosticGPU"]["elapsed_ms"]
    if not all(math.isfinite(value) and value > 0 for value in [preview, gpu]) or gpu < preview:
        raise ValueError("Missing, nonfinite, or inconsistent capture-to-frame timings")


def summarize(records, samples):
    measured = [record for record in records if record["phase"] == "measure"]
    if len(measured) != samples * 2:
        raise ValueError("Incomplete benchmark: refusing to summarize partial samples")
    for record in records:
        validate_capture(record)
    pairs = {}
    for record in measured:
        pair = pairs.setdefault(record["pair"], {})
        if record["variant"] in pair:
            raise ValueError("Duplicate variant within a measured pair")
        pair[record["variant"]] = record
    if set(pairs) != set(range(samples)) or any(set(pair) != {"baseline", "revised"} for pair in pairs.values()):
        raise ValueError("Measured pairs are incomplete")
    result = {}
    for endpoint, key in [("preview", "diagnosticPreview"), ("gpu", "diagnosticGPU")]:
        groups = {}
        for variant in ["baseline", "revised"]:
            values = sorted(pair[variant][key]["elapsed_ms"] for pair in pairs.values())
            groups[variant] = {"count": len(values), "medianMs": statistics.median(values),
                               "p90Ms": values[math.ceil(len(values) * 0.9) - 1],
                               "minMs": values[0], "maxMs": values[-1]}
        groups["medianReductionPercent"] = 100 * (1 - groups["revised"]["medianMs"] / groups["baseline"]["medianMs"])
        deltas = [pair["baseline"][key]["elapsed_ms"] - pair["revised"][key]["elapsed_ms"] for pair in pairs.values()]
        groups["pairedMedianReductionMs"] = statistics.median(deltas)
        groups["revisedFasterPairs"] = sum(value > 0 for value in deltas)
        result[endpoint] = groups
    return result


def compare_inputs(fixture, first, second, output):
    result = subprocess.run(
        [str(fixture), "--compare-images", str(first), str(second)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    output.with_suffix(".stdout.log").write_text(result.stdout)
    output.with_suffix(".stderr.log").write_text(result.stderr)
    result.check_returncode()
    comparison = json.loads(result.stdout)
    output.with_suffix(".json").write_text(json.dumps(comparison, indent=2))
    if comparison.get("interiorMatches") is not True:
        raise ValueError(f"Capture input differs inside the fixed comparison region: {second}")
    return {key: value for key, value in comparison.items() if key != "exteriorDifferences"}


def collect(package, output, fixture, metadata, *, samples=12, warmups=2):
    if not 2 <= samples <= 30 or not 1 <= warmups <= 5:
        raise ValueError("Expected 2-30 measured pairs and 1-5 warmup pairs")
    output.mkdir(parents=True)
    records = []
    report = {"passed": False, "measuredSamplesPerVariant": samples, "excludedWarmupsPerVariant": warmups,
              "conditions": "fresh Cap process/profile per capture; warmed filesystem and driver caches",
              "editorImageRecording": False, "physicalScanoutMeasured": False,
              "excludedInputBorderPixels": 16, "outlierRemoval": "none", "results": None, "samples": records}
    binaries = {binary["variant"]: binary for binary in metadata["binaries"]}
    started = time.monotonic()
    try:
        for phase, pair, order in schedule(samples, warmups):
            directory = output / f"{phase}-{pair + 1:02d}"
            directory.mkdir()
            for position, variant in enumerate(order):
                if time.monotonic() - started > 900:
                    raise TimeoutError("Benchmark exceeded its 15-minute collection deadline")
                record = smoke.run_variant(package, directory, fixture, binaries[variant], capture_editor=False)
                record.update({"phase": phase, "pair": pair, "positionWithinPair": position})
                if "inputPNG" in record:
                    record["inputPNG"] = (directory / record["inputPNG"]).relative_to(output).as_posix()
                records.append(record)
                (output / "partial-results.json").write_text(json.dumps(report, indent=2))
                validate_capture(record)
        comparisons = output / "input-comparisons"
        comparisons.mkdir()
        reference = output / records[0]["inputPNG"]
        for index, record in enumerate(records):
            if time.monotonic() - started > 900:
                raise TimeoutError("Benchmark exceeded its 15-minute collection/validation deadline")
            record["inputComparison"] = compare_inputs(
                fixture, reference, output / record["inputPNG"], comparisons / f"sample-{index:02d}",
            )
            (output / "partial-results.json").write_text(json.dumps(report, indent=2))
        report["results"] = summarize(records, samples)
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        (output / "benchmark.json").write_text(json.dumps(report, indent=2))
        with (output / "samples.csv").open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["phase", "pair", "position", "variant", "passed", "preview_ms", "gpu_ms", "input_png"])
            for record in records:
                writer.writerow([record["phase"], record["pair"], record["positionWithinPair"], record["variant"],
                                 record["passed"], record.get("diagnosticPreview", {}).get("elapsed_ms"),
                                 record.get("diagnosticGPU", {}).get("elapsed_ms"), record.get("inputPNG")])
    return report


def render_report(report, provenance):
    probe = provenance["runnerProbe"]
    metadata = provenance["builds"]
    lines = [
        "# Cap screenshot startup on macOS CI", "",
        f"Status: {'PASS' if report['passed'] else 'FAIL / INCOMPLETE — no performance claim'}", "",
        f"Baseline: `{metadata['baseline_commit']}`. Revised: the source snapshot recorded in `provenance.json`.",
        f"Build: {metadata['profile']}. Saved build run: {provenance['package']['buildRun']}.",
        f"Platform: {probe['os']}; GPU: {probe['metalDevice']}; full display: "
        f"{probe['display']['pixelWidth']} × {probe['display']['pixelHeight']} at scale {probe['display']['scale']}.", "",
        f"{report['measuredSamplesPerVariant']} measured captures per variant; "
        f"{report['excludedWarmupsPerVariant']} additional warmup captures per variant excluded.",
        "Measured pairs alternate baseline-first and revised-first. No outliers are removed.",
        "Each capture starts a fresh process and isolated profile. Filesystem/driver caches are warm; "
        "the graphics probe and one smoke capture per variant also precede these warmups on the same runner.",
        "This is neither cold-machine startup nor repeated editor opens within one process.", "",
        "The full-display input is a fixed three-color scene. The timer starts at take_screenshot entry, "
        "before settings/device preparation and screenshot capture preparation. Endpoints are the first visible image paint followed "
        "by a GPUI frame callback, and the first GPU-result paint followed by that callback. "
        "They do not measure physical scanout. The baseline's first image is already GPU-rendered.",
        "Screenshot startup experiments are absent from the environment. Editor-window image recording "
        "and verbose dynamic-loader logging are disabled for warmup and measured samples. "
        "The fixture, capture IPC, draw hooks, and log polling remain enabled in both variants.",
        "Saved-PNG inspection runs after editor close; all cross-sample comparisons run after timing collection.", "",
        "Inputs must match the reference exactly after excluding a fixed 16-pixel border on all four sides. "
        "Full PNGs and individual border differences are retained. The excluded edges are not validated for equality.",
        "This run does not establish a flash-free preview handoff or performance on consumer Mac hardware.", "",
    ]
    if report["passed"]:
        for endpoint, label in [("preview", "First visible image"), ("gpu", "First GPU-rendered image")]:
            group = report["results"][endpoint]
            base, revised = group["baseline"], group["revised"]
            lines.extend([
                f"## {label}", "",
                f"Median: {base['medianMs']:.1f} ms → {revised['medianMs']:.1f} ms; "
                f"{group['medianReductionPercent']:.1f}% lower (a negative value means slower).",
                f"p90, nearest rank: {base['p90Ms']:.1f} ms → {revised['p90Ms']:.1f} ms.",
                f"Range: baseline {base['minMs']:.1f}–{base['maxMs']:.1f} ms; "
                f"revised {revised['minMs']:.1f}–{revised['maxMs']:.1f} ms.",
                f"Revised faster in {group['revisedFasterPairs']}/{report['measuredSamplesPerVariant']} pairs; "
                f"median paired reduction {group['pairedMedianReductionMs']:.1f} ms.", "",
            ])
    else:
        lines.append(f"Failure: {report.get('error', 'unknown')}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("smoke_output", type=Path)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--warmup-pairs", type=int, default=2)
    args = parser.parse_args()
    root = args.smoke_output.resolve()
    package = args.package.resolve()
    metadata = smoke.load_package(package)
    manifest = json.loads((package / "package.json").read_text())
    smoke_report = json.loads((root / "smoke.json").read_text())
    probe = json.loads((root / "preflight/probe.json").read_text())
    if not smoke_report.get("passed") or smoke_report["package"]["buildRun"] != manifest["buildRun"] or not probe.get("allChecksPassed"):
        raise ValueError("A passing smoke test of this package and runner is required before timing")
    provenance = {"builds": metadata, "package": {key: value for key, value in manifest.items() if key != "files"},
                  "runnerProbe": probe, "benchmarkCommit": os.environ.get("GITHUB_SHA"),
                  "benchmarkRun": os.environ.get("GITHUB_RUN_ID")}
    report = collect(package, root / "timings", root / "cap-fixture", metadata,
                     samples=args.samples, warmups=args.warmup_pairs)
    (root / "timings/provenance.json").write_text(json.dumps(provenance, indent=2))
    text = render_report(report, provenance)
    (root / "timings/report.md").write_text(text)
    print(text, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write(text)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
