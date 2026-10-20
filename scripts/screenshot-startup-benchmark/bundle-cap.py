import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(directory):
    entries = {}
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory).as_posix()
        if name == "package.json":
            continue
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(directory.resolve()):
                raise ValueError(f"Runtime symlink escapes the package: {name}")
            entries[name] = {"symlink": os.readlink(path)}
        elif path.is_file():
            entries[name] = {"sha256": sha256(path), "executable": bool(path.stat().st_mode & 0o111)}
    return entries


def verify(directory):
    manifest = json.loads((directory / "package.json").read_text())
    if inventory(directory) != manifest["files"]:
        raise ValueError("Package files, permissions, or checksums differ from the manifest")
    metadata = json.loads((directory / "builds/binaries.json").read_text())
    if metadata.get("validate_only") or [entry["variant"] for entry in metadata["binaries"]] != ["baseline", "revised"]:
        raise ValueError("Package must contain both compiled release variants")
    for binary in metadata["binaries"]:
        relative = Path(binary["executable"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Package executable must use an internal relative path")
        executable = directory / relative
        if sha256(executable) != binary["sha256"] or not os.access(executable, os.X_OK):
            raise ValueError(f"Invalid packaged executable: {relative}")
        for name, expected in binary["instrumented_sources"].items():
            if Path(name).name != name or sha256(executable.parent / name) != expected:
                raise ValueError("Packaged instrumented source differs from build provenance")
    return manifest


def create(root, builds, output):
    package = output / "package"
    package.mkdir(parents=True)
    metadata = json.loads((builds / "binaries.json").read_text())
    if metadata.get("validate_only") or len(metadata["binaries"]) != 2:
        raise ValueError("Expected two completed release builds")
    for binary in metadata["binaries"]:
        source = Path(binary["executable"])
        if sha256(source) != binary["sha256"]:
            raise ValueError("Built executable differs from provenance")
        destination = package / "builds" / binary["variant"]
        shutil.copytree(builds / binary["variant"], destination, symlinks=True)
        binary["executable"] = (destination / "cap-gpui").relative_to(package).as_posix()
    (package / "builds/binaries.json").write_text(json.dumps(metadata, indent=2))
    mappings = [
        (root / "target/Frameworks", package / "runtime/Frameworks"),
        (root / "target/native-deps/lib", package / "runtime/lib"),
        (root / "target/native-deps/onnxruntime/lib", package / "runtime/onnxruntime"),
    ]
    for source, destination in mappings:
        shutil.copytree(source, destination)
    dependency_log = {}
    for binary in metadata["binaries"]:
        dependency_log[binary["variant"]] = subprocess.check_output(
            ["otool", "-L", str(package / binary["executable"])], text=True,
        )
    (package / "dependencies.json").write_text(json.dumps(dependency_log, indent=2))
    manifest = {"schema": 1, "repository": os.environ.get("GITHUB_REPOSITORY"),
                "buildCommit": os.environ.get("GITHUB_SHA"), "buildRun": os.environ.get("GITHUB_RUN_ID"),
                "platform": "macos-15-arm64", "files": inventory(package)}
    (package / "package.json").write_text(json.dumps(manifest, indent=2))
    verify(package)
    archive = output / "cap-screenshot-runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(package, arcname="package")
    (output / "cap-screenshot-runtime.sha256").write_text(sha256(archive) + "\n")
    print(f"Packaged {len(manifest['files'])} runtime/source files: {archive}", flush=True)


def extract(archive, destination, expected_run=None):
    expected = archive.with_name("cap-screenshot-runtime.sha256").read_text().strip()
    if sha256(archive) != expected:
        raise ValueError("Downloaded runtime archive checksum differs")
    destination.mkdir(parents=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    package = destination / "package"
    manifest = verify(package)
    if expected_run is not None and (manifest["repository"] != "dovidmoishe/Cap" or manifest["buildRun"] != expected_run):
        raise ValueError("Package provenance differs from the selected fork run")
    return package


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("builds", type=Path)
    create_parser.add_argument("output", type=Path)
    extract_parser = commands.add_parser("extract")
    extract_parser.add_argument("archive", type=Path)
    extract_parser.add_argument("destination", type=Path)
    extract_parser.add_argument("--expected-run")
    args = parser.parse_args()
    if args.command == "create":
        create(Path(__file__).resolve().parents[2], args.builds.resolve(), args.output.resolve())
    else:
        print(extract(args.archive.resolve(), args.destination.resolve(), args.expected_run))


if __name__ == "__main__":
    main()
