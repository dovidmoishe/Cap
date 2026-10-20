import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re

import numpy as np
from PIL import Image, ImageDraw


class Inconclusive(ValueError):
    pass


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def load_frames(root, report):
    if not report.get("completed") or report.get("stream", {}).get("errors"):
        raise Inconclusive(f"Recorder did not finish cleanly: {report.get('error', report.get('stream', {}).get('errors'))}")
    frames = report.get("frames", [])
    if len(frames) < 3 or any(not finite(frame.get("utc")) for frame in frames):
        raise Inconclusive("Not enough timestamped recorder frames")
    if any(second["utc"] <= first["utc"] for first, second in zip(frames, frames[1:])):
        raise Inconclusive("Frame presentation times are not strictly increasing")
    geometry = report["geometry"]
    if geometry.get("scale") != 1:
        raise Inconclusive("Display-coordinate projection is verified only at scale one")
    images = []
    for frame in frames:
        path = (root / frame["file"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise Inconclusive("Recorded image path escapes its evidence directory")
        with Image.open(path) as image:
            if image.size != (geometry["width"], geometry["height"]):
                raise Inconclusive("Captured frame dimensions differ from recorder geometry")
            images.append(np.array(image.convert("RGB")))
    return frames, images


def color_present(pixels):
    r, g, b = (pixels[:, :, channel] for channel in range(3))
    return [float(mask.mean()) for mask in [(r > 153) & (g < 77) & (b < 102),
                                           (g > 128) & (r < 77) & (b < 102),
                                           (b > 153) & (r < 77) & (g < 102)]]


def self_test(root, report):
    frames, images = load_frames(root, report)
    phases = report.get("phaseEvents", [])
    if [phase["name"] for phase in phases] != ["blue", "green", "blue-again"]:
        raise Inconclusive("Native recorder did not run its three calibration phases")
    found = []
    for index, phase in enumerate(phases):
        end = phases[index + 1]["utc"] if index + 1 < len(phases) else frames[-1]["utc"] + 0.001
        rows = [(frame, image) for frame, image in zip(frames, images) if phase["utc"] + 0.08 <= frame["utc"] < end]
        if not rows:
            raise Inconclusive(f"No clock-aligned frames in native calibration phase {phase['name']}")
        for frame, image in rows:
            r, g, b = image[image.shape[0] // 4, image.shape[1] // 2] / 255
            matches = g > 0.5 and r < 0.3 and b < 0.4 if phase["name"] == "green" else b > 0.6 and r < 0.3 and g < 0.4
            if not matches:
                raise Inconclusive(f"Pixel/time mismatch in native calibration phase {phase['name']}")
        found.append({"phase": phase["name"], "samples": len(rows)})
    return {"status": "calibration-passed", "phases": found, "physicalDisplayVerified": False}


def editor_rect(report, capture, start, end):
    history = report.get("editorHistory", [])
    match = re.search(r"screenshot editor window opened number=([0-9]+)", capture["log"])
    if not match or not history:
        raise Inconclusive("No owned native screenshot-editor geometry was recorded")
    number = int(match[1])
    preceding = [row for row in history if row["utc"] <= start]
    if not preceding:
        raise Inconclusive("Editor geometry was not observed before the inspected transition interval")
    rows = [preceding[-1], *[row for row in history if start < row["utc"] <= end]]
    first = rows[0]["bounds"]
    for row in rows:
        if row.get("pid") != capture["ownedPID"] or row.get("windowNumber") != number or row.get("alpha") != 1:
            raise Inconclusive("Recorder geometry did not identify an opaque owned editor window")
        if row["bounds"] != first:
            raise Inconclusive("Editor bounds changed before or during the inspected interval")
    values = [first.get(key) for key in ["X", "Y", "Width", "Height"]]
    if not all(finite(value) for value in values):
        raise Inconclusive("Missing finite editor bounds")
    x, y, width, height = values
    x -= report["geometry"]["displayOriginX"]
    y -= report["geometry"]["displayOriginY"]
    rect = tuple(round(value) for value in [x, y, x + width, y + height])
    if rect[0] < 0 or rect[1] < 0 or rect[2] > report["geometry"]["width"] or rect[3] > report["geometry"]["height"] or min(width, height) <= 0:
        raise Inconclusive("Editor is not fully inside the recorded display")
    return rect


def transition(root, report, capture):
    frames, images = load_frames(root, report)
    if not capture.get("passed") or capture.get("editorImageRecording") is not False:
        raise Inconclusive("The owned Cap capture did not pass its normal capture/render/close checks")
    if capture.get("input", {}).get("fixtureMatches") is not True or capture.get("input", {}).get("containsFixtureColors") is not True:
        raise Inconclusive("Cap did not capture the static color pattern")
    preview = utc(capture["diagnosticPreview"]["utc"])
    gpu = utc(capture["diagnosticGPU"]["utc"])
    capture_match = re.search(r"^(\S+) .*CAP_BENCH_CAPTURE\s*$", capture["log"], re.MULTILINE)
    if not capture_match:
        raise Inconclusive("Missing Cap capture-entry timestamp")
    started = utc(capture_match[1])
    if not finite(report.get("readyUTC")) or report["readyUTC"] >= started or not started < preview < gpu:
        raise Inconclusive("Recorder did not start before capture, or a separate raw-preview interval was not observed")
    if gpu - preview < 0.05:
        raise Inconclusive("Callback separation below 50ms cannot establish a raw-preview interval")
    if capture["diagnosticPreview"]["bounds"] != capture["diagnosticGPU"]["bounds"]:
        raise Inconclusive("Software preview and GPU image bounds differ")
    rect = editor_rect(report, capture, preview, gpu + 0.6)
    x0, y0, x1, y1 = rect
    crops = [image[y0:y1, x0:x1] for image in images]
    ratios = [color_present(crop) for crop in crops]
    has_pattern = [all(value > 0.05 for value in row) for row in ratios]
    before = [index for index, frame in enumerate(frames) if started < frame["utc"] <= min(preview + 0.02, gpu - 0.025) and has_pattern[index]]
    after = [index for index, frame in enumerate(frames) if gpu + 0.05 <= frame["utc"] <= gpu + 0.6 and has_pattern[index]]
    if len(before) < 2 or len(after) < 2:
        raise Inconclusive("Recording missed enough visible raw-preview or post-GPU samples; no flash-free claim")
    first, last = before[0], after[-1]
    selected = list(range(first, last + 1))
    gaps = [(frames[b]["utc"] - frames[a]["utc"]) * 1000 for a, b in zip(selected, selected[1:])]
    if max(gaps, default=math.inf) > 50:
        raise Inconclusive("Recorded transition contains a presentation-sampling gap above 50ms")
    if any(frames[first]["utc"] <= row["utc"] <= frames[last]["utc"] for row in report["stream"].get("unusableEvents", [])):
        raise Inconclusive("A blank, suspended, or otherwise unusable stream event occurred during the transition")
    missing = [index for index in selected if not has_pattern[index]]
    reference = crops[after[-1]].astype(np.int16)
    differences = []
    for index in selected:
        delta = np.abs(crops[index].astype(np.int16) - reference)
        differences.append({"frame": index, "utc": frames[index]["utc"], "file": frames[index]["file"],
                            "freshPixels": frames[index]["freshPixels"], "patternColorRatios": ratios[index],
                            "wholeWindowChangedPixels": int(np.any(delta != 0, axis=2).sum()),
                            "wholeWindowMeanChannelDifference": float(delta.mean()),
                            "wholeWindowMaxChannelDifference": int(delta.max())})
    output = {"status": "possible-visual-discontinuity" if missing else "evidence-ready-for-review",
              "purpose": "Untimed transition evidence, not benchmark results",
              "manualVisualReviewRequired": True, "physicalDisplayVerified": False,
              "sourcePNGsha256": capture["inputPNGsha256"], "editorWindowRect": list(rect),
              "previewUTC": preview, "gpuUTC": gpu, "rawPreviewSamples": len(before), "postGPUSamples": len(after),
              "maximumPresentationGapMs": max(gaps), "possibleBlankFrames": missing, "frames": differences,
              "wholeWindowDifferenceCaveat": "Includes UI chrome; differences are diagnostic and do not automatically establish an image flash"}
    filmstrip(root, crops, frames, sorted(set(before[-3:] + after[:3] + missing)), preview, gpu)
    return output


def filmstrip(root, crops, frames, indices, preview, gpu):
    width, height = 496, 370
    sheet = Image.new("RGB", (width * 3, height * math.ceil(len(indices) / 3)), "#202020")
    draw = ImageDraw.Draw(sheet)
    for position, index in enumerate(indices):
        image = Image.fromarray(crops[index])
        image.thumbnail((width - 8, height - 42))
        x, y = position % 3 * width, position // 3 * height
        sheet.paste(image, (x + 4, y + 38))
        phase = "before GPU" if frames[index]["utc"] < gpu else "after GPU"
        draw.text((x + 6, y + 5), f"Frame {index}: {phase}", fill="white")
        draw.text((x + 6, y + 21), f"{(frames[index]['utc'] - preview) * 1000:+.1f} ms from preview callback", fill="white")
    sheet.save(root / "transition-filmstrip.png")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recording", type=Path)
    parser.add_argument("--capture", type=Path)
    args = parser.parse_args()
    root = args.recording.resolve()
    report = json.loads((root / "recording.json").read_text())
    try:
        if args.capture:
            capture = json.loads((args.capture / "result.json").read_text())
            capture["log"] = (args.capture / "cap.log").read_text()
            result = transition(root, report, capture)
        else:
            result = self_test(root, report)
    except (Inconclusive, OSError, ValueError, KeyError) as error:
        result = {"status": "inconclusive", "error": str(error), "physicalDisplayVerified": False}
    (root / "analysis.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({key: value for key, value in result.items() if key != "frames"}, indent=2))
    raise SystemExit(0 if result["status"] in ("calibration-passed", "evidence-ready-for-review") else 1)


if __name__ == "__main__":
    main()
