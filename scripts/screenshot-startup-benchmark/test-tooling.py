import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundle = load("bundle-cap")
images = load("validate-images")


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.builds = self.root / "builds"
        binaries = []
        for variant in ["baseline", "revised"]:
            directory = self.builds / variant
            directory.mkdir(parents=True)
            executable = directory / "cap-gpui"
            executable.write_bytes(variant.encode())
            executable.chmod(0o755)
            source = directory / "screenshot.rs"
            source.write_text(variant)
            binaries.append({"variant": variant, "executable": str(executable), "sha256": bundle.sha256(executable),
                             "instrumented_sources": {source.name: bundle.sha256(source)}})
        (self.builds / "binaries.json").write_text(json.dumps({"binaries": binaries, "validate_only": False}))
        for directory in ["target/Frameworks", "target/native-deps/lib", "target/native-deps/onnxruntime/lib"]:
            path = self.root / directory
            path.mkdir(parents=True)
            (path / "runtime.dylib").write_bytes(directory.encode())
        self.output = self.root / "output"
        with patch.object(bundle.subprocess, "check_output", return_value="fake otool listing"), patch.dict(
            os.environ, {"GITHUB_REPOSITORY": "dovidmoishe/Cap", "GITHUB_RUN_ID": "123", "GITHUB_SHA": "test"},
        ):
            bundle.create(self.root, self.builds, self.output)
        self.package = self.output / "package"
        self.archive = self.output / "cap-screenshot-runtime.tar.gz"

    def test_relocates_without_original_runtime_or_builds(self):
        self.builds.rename(self.root / "removed-builds")
        (self.root / "target").rename(self.root / "removed-target")
        package = bundle.extract(self.archive, self.root / "extracted", "123")
        manifest = bundle.verify(package)
        self.assertEqual(manifest["buildRun"], "123")
        self.assertTrue((package / "runtime/onnxruntime/runtime.dylib").is_file())

    def test_rejects_wrong_archive_checksum(self):
        with self.archive.open("ab") as stream:
            stream.write(b"modified")
        with self.assertRaisesRegex(ValueError, "checksum"):
            bundle.extract(self.archive, self.root / "extracted")

    def test_rejects_missing_runtime(self):
        (self.package / "runtime/lib/runtime.dylib").unlink()
        with self.assertRaisesRegex(ValueError, "manifest"):
            bundle.verify(self.package)

    def test_rejects_modified_executable(self):
        (self.package / "builds/revised/cap-gpui").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "manifest"):
            bundle.verify(self.package)

    def test_rejects_unlisted_file(self):
        (self.package / "unexpected.dylib").write_bytes(b"unexpected")
        with self.assertRaisesRegex(ValueError, "manifest"):
            bundle.verify(self.package)

    def test_rejects_wrong_source_run(self):
        with self.assertRaisesRegex(ValueError, "provenance"):
            bundle.extract(self.archive, self.root / "extracted", "456")

    def test_rejects_stale_source_provenance(self):
        metadata_path = self.package / "builds/binaries.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["binaries"][0]["instrumented_sources"]["screenshot.rs"] = "wrong"
        metadata_path.write_text(json.dumps(metadata))
        manifest_path = self.package / "package.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"] = bundle.inventory(self.package)
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "source"):
            bundle.verify(self.package)

    def test_generated_pngs_round_trip(self):
        from struct import unpack
        import zlib

        for channels in [1, 3, 4]:
            path = self.root / f"generated-{channels}.png"
            pixels = bytes(range(6 * 4 * channels))
            images.write_png(path, 6, 4, pixels, channels)
            data = path.read_bytes()
            offset = 8
            payload = b""
            while offset < len(data):
                size = unpack(">I", data[offset:offset + 4])[0]
                kind = data[offset + 4:offset + 8]
                chunk = data[offset + 8:offset + 8 + size]
                crc = unpack(">I", data[offset + 8 + size:offset + 12 + size])[0]
                self.assertEqual(crc, zlib.crc32(kind + chunk))
                if kind == b"IDAT":
                    payload += chunk
                offset += size + 12
            rows = zlib.decompress(payload)
            self.assertEqual(b"".join(rows[y * (6 * channels + 1) + 1:(y + 1) * (6 * channels + 1)] for y in range(4)), pixels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
