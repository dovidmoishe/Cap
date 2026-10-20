import argparse
import json
from pathlib import Path
import struct
import subprocess
import zlib


def write_png(path, width, height, pixels, channels=3):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\0" + pixels[y * width * channels:(y + 1) * width * channels] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, {1: 0, 3: 2, 4: 6}[channels], 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def invoke(fixture, arguments, prefix):
    result = subprocess.run([str(fixture), *map(str, arguments)], capture_output=True, text=True, timeout=60, check=False)
    prefix.with_suffix(".stdout.log").write_text(result.stdout)
    prefix.with_suffix(".stderr.log").write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(f"Image helper failed; inspect {prefix}")
    return json.loads(result.stdout)


def validate(fixture, inputs, output):
    output.mkdir(parents=True)
    results = []

    def check(name, arguments, expected, field):
        report = invoke(fixture, arguments, output / name)
        results.append({"name": name, "expected": expected, "field": field, "report": report})
        (output / "checks.json").write_text(json.dumps(results, indent=2))
        if report.get(field) is not expected:
            raise ValueError(f"{name}: expected {field}={expected}")
        return report

    images = sorted(inputs.rglob("*.png"))
    originals = [path for path in images if path.name == "original.png"]
    if len(images) != 5 or len(originals) != 2:
        raise ValueError("Expected the five pinned smoke evidence images")
    for index, image in enumerate(images):
        report = check(f"saved-{index}", ["--inspect-image", image], True, "fixtureMatches")
        if not report.get("containsFixtureColors"):
            raise ValueError(f"Saved evidence lacks substantial fixture color regions: {image}")
    previous = check("saved-comparison", ["--compare-images", *originals], True, "interiorMatches")
    if previous["exteriorDifferentPixels"] != 36:
        raise ValueError("Pinned run 39426190700 no longer reproduces its investigated 36 border pixels")

    width, height = 96, 64
    colors = [(237, 40, 39), (15, 75, 235), (0, 209, 66)]
    pixels = bytes(channel for _y in range(height) for x in range(width) for channel in colors[x * 3 // width])
    positive = output / "positive.png"
    write_png(positive, width, height, pixels)
    check("positive", ["--inspect-image", positive], True, "fixtureMatches")
    variants = {
        "blank": (bytes(len(pixels)), 3),
        "swapped": (bytes(channel for _y in range(height) for x in range(width) for channel in colors[2 - x * 3 // width]), 3),
        "missing-blue": (bytes(channel for _y in range(height) for x in range(width) for channel in colors[0 if x * 3 // width == 1 else x * 3 // width]), 3),
        "transparent": (bytes(channel for offset in range(0, len(pixels), 3) for channel in (*pixels[offset:offset + 3], 0)), 4),
        "grayscale": (bytes([128]) * width * height, 1),
    }
    for name, (data, channels) in variants.items():
        path = output / f"{name}.png"
        write_png(path, width, height, data, channels)
        check(name, ["--inspect-image", path], False, "fixtureMatches")
    sparse = bytearray(len(pixels))
    for x, rgb in zip([width // 6, width // 2, width * 5 // 6], colors):
        offset = (height // 2 * width + x) * 3
        sparse[offset:offset + 3] = bytes(rgb)
    sparse_path = output / "sparse.png"
    write_png(sparse_path, width, height, sparse)
    check("sparse", ["--inspect-image", sparse_path], False, "containsFixtureColors")
    for name, x, y, expected in [
        ("interior-change", width // 2, height // 2, False), ("border-change", 0, 0, True),
        ("border-last-column", 15, 16, True), ("interior-first-column", 16, 16, False),
        ("border-last-row", 16, 15, True), ("interior-last-column", width - 17, 16, False),
        ("border-first-right-column", width - 16, 16, True),
    ]:
        changed = bytearray(pixels)
        changed[(y * width + x) * 3] ^= 1
        path = output / f"{name}.png"
        write_png(path, width, height, changed)
        report = check(name, ["--compare-images", positive, path], expected, "interiorMatches")
        if (report["interiorDifferentPixels"], report["exteriorDifferentPixels"]) != ((1, 0) if not expected else (0, 1)):
            raise ValueError(f"{name}: difference reporting failed")
    for name, x, y, expected in [
        ("desktop-menu-last-row", 16, 23, True), ("desktop-first-row", 16, 24, False),
        ("desktop-menu-previously-interior", 16, 16, True), ("desktop-interior-middle", 48, 32, False),
        ("desktop-border-last-column", 15, 24, True), ("desktop-interior-last-column", width - 17, 24, False),
        ("desktop-border-first-right-column", width - 16, 24, True),
        ("desktop-interior-last-row", 16, height - 17, False), ("desktop-border-first-bottom-row", 16, height - 16, True),
    ]:
        changed = bytearray(pixels)
        changed[(y * width + x) * 3] ^= 1
        path = output / f"{name}.png"
        write_png(path, width, height, changed)
        report = check(name, ["--compare-desktop-images", positive, path, 24], expected, "interiorMatches")
        if report.get("excludedTopPixels") != 24 or report.get("excludedBorderPixels") != 16:
            raise ValueError(f"{name}: comparison geometry changed")
        if (report["interiorDifferentPixels"], report["exteriorDifferentPixels"]) != ((1, 0) if not expected else (0, 1)):
            raise ValueError(f"{name}: difference reporting failed")
    for value in ["invalid", -1, 15, height - 16, height]:
        name = f"desktop-invalid-top-{value}"
        result = subprocess.run([str(fixture), "--compare-desktop-images", str(positive), str(positive), str(value)],
                                capture_output=True, text=True, timeout=60, check=False)
        (output / f"{name}.stdout.log").write_text(result.stdout)
        (output / f"{name}.stderr.log").write_text(result.stderr)
        results.append({"name": name, "expected": "nonzero exit", "returncode": result.returncode})
        (output / "checks.json").write_text(json.dumps(results, indent=2))
        if result.returncode == 0:
            raise ValueError(f"{name}: invalid comparison geometry was accepted")
    print(f"Validated {len(results)} image checks", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    validate(args.fixture.resolve(), args.inputs.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
