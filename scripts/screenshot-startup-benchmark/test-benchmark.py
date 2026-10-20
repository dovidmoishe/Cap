import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("benchmark", Path(__file__).with_name("run-benchmark.py"))
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def capture(variant, preview=100, gpu=200):
    return {"variant": variant, "passed": True, "editorImageRecording": False,
            "diagnosticPreview": {"elapsed_ms": preview}, "diagnosticGPU": {"elapsed_ms": gpu},
            "inputPNG": f"{variant}/original.png"}


class StatisticsTests(unittest.TestCase):
    def test_warmups_excluded_and_paired_medians_reported(self):
        records = [dict(capture("baseline", 999, 999), phase="warmup", pair=0)]
        for pair, base, revised in [(0, 100, 50), (1, 110, 55)]:
            records.extend([dict(capture("baseline", base, base * 2), phase="measure", pair=pair),
                            dict(capture("revised", revised, revised * 2), phase="measure", pair=pair)])
        report = benchmark.summarize(records, 2)
        self.assertEqual(report["preview"]["baseline"], {"count": 2, "medianMs": 105, "p90Ms": 110, "minMs": 100, "maxMs": 110})
        self.assertEqual(report["preview"]["revised"]["medianMs"], 52.5)
        self.assertEqual(report["preview"]["medianReductionPercent"], 50)
        self.assertEqual(report["gpu"]["pairedMedianReductionMs"], 105)

    def test_measured_order_balanced_and_warmups_separate(self):
        plan = list(benchmark.schedule(12, 2))
        measured = [entry for entry in plan if entry[0] == "measure"]
        self.assertEqual(len(plan), 14)
        self.assertEqual(sum(order[0] == "baseline" for _, _, order in measured), 6)
        self.assertEqual(sum(order[0] == "revised" for _, _, order in measured), 6)

    def test_faster_preview_does_not_hide_slower_gpu_output(self):
        records = []
        for pair in range(2):
            records.extend([dict(capture("baseline", 100, 200), phase="measure", pair=pair),
                            dict(capture("revised", 50, 240), phase="measure", pair=pair)])
        report = benchmark.summarize(records, 2)
        self.assertEqual(report["preview"]["medianReductionPercent"], 50)
        self.assertAlmostEqual(report["gpu"]["medianReductionPercent"], -20)
        self.assertEqual(report["gpu"]["revisedFasterPairs"], 0)

    def test_rejects_missing_or_duplicate_pairs(self):
        records = [dict(capture("baseline"), phase="measure", pair=0)]
        with self.assertRaisesRegex(ValueError, "partial"):
            benchmark.summarize(records, 2)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            benchmark.summarize(records * 4, 2)

    def test_rejects_recording_and_invalid_timing(self):
        record = capture("baseline")
        record["editorImageRecording"] = True
        with self.assertRaisesRegex(ValueError, "recording"):
            benchmark.validate_capture(record)
        for preview, gpu in [(math.nan, 200), (100, math.inf), (0, 200), (200, 100)]:
            with self.subTest(preview=preview, gpu=gpu), self.assertRaises(ValueError):
                benchmark.validate_capture(capture("baseline", preview, gpu))


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.metadata = {"binaries": [{"variant": "baseline"}, {"variant": "revised"}]}

    def fake_capture(self, package, output, fixture, binary, *, capture_editor):
        self.assertFalse(capture_editor)
        record = capture(binary["variant"])
        path = output / record["inputPNG"]
        path.parent.mkdir()
        path.write_bytes(b"saved fixture")
        return record

    def comparison(self, matches=True):
        return subprocess.CompletedProcess([], 0, json.dumps({"interiorMatches": matches,
            "interiorDifferentPixels": 0 if matches else 1, "exteriorDifferentPixels": 1,
            "exteriorDifferences": [{"x": 0, "y": 0}]}), "")

    def test_collects_all_samples_without_editor_recording(self):
        with patch.object(benchmark.smoke, "run_variant", side_effect=self.fake_capture) as run, patch.object(
            benchmark.subprocess, "run", return_value=self.comparison(),
        ) as compare:
            report = benchmark.collect(self.root, self.root / "timings", self.root / "fixture", self.metadata)
        self.assertTrue(report["passed"])
        self.assertEqual(run.call_count, 28)
        self.assertEqual(compare.call_count, 28)
        self.assertEqual(report["results"]["gpu"]["baseline"]["count"], 12)
        self.assertEqual(sum(record["phase"] == "warmup" for record in report["samples"]), 4)
        self.assertTrue((self.root / "timings/input-comparisons/sample-00.json").is_file())
        self.assertEqual(len((self.root / "timings/samples.csv").read_text().splitlines()), 29)

    def test_failed_capture_stops_and_preserves_partial_evidence(self):
        failed = capture("baseline")
        failed.update({"passed": False, "error": "capture denied"})
        with patch.object(benchmark.smoke, "run_variant", return_value=failed) as run:
            report = benchmark.collect(self.root, self.root / "timings", self.root / "fixture", self.metadata)
        self.assertFalse(report["passed"])
        self.assertIsNone(report["results"])
        self.assertEqual(run.call_count, 1)
        self.assertIn("capture denied", report["error"])
        self.assertTrue((self.root / "timings/partial-results.json").is_file())

    def test_input_mismatch_invalidates_all_performance_results(self):
        with patch.object(benchmark.smoke, "run_variant", side_effect=self.fake_capture), patch.object(
            benchmark.subprocess, "run", return_value=self.comparison(False),
        ):
            report = benchmark.collect(self.root, self.root / "timings", self.root / "fixture", self.metadata)
        self.assertFalse(report["passed"])
        self.assertIsNone(report["results"])
        self.assertEqual(len(report["samples"]), 28)
        saved = json.loads((self.root / "timings/input-comparisons/sample-00.json").read_text())
        self.assertEqual(saved["interiorDifferentPixels"], 1)
        self.assertEqual(len(saved["exteriorDifferences"]), 1)

    def test_comparison_failure_preserves_stdout_and_stderr(self):
        result = subprocess.CompletedProcess([], 2, "partial", "decode failed")
        with patch.object(benchmark.subprocess, "run", return_value=result), self.assertRaises(subprocess.CalledProcessError):
            benchmark.compare_inputs(self.root / "fixture", self.root / "a", self.root / "b", self.root / "failure")
        self.assertEqual((self.root / "failure.stderr.log").read_text(), "decode failed")


class CaptureModeTests(unittest.TestCase):
    def test_timing_mode_disables_recording_and_loader_logging_without_changing_smoke_default(self):
        for recording in [False, True]:
            with self.subTest(recording=recording), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / "output"
                output.mkdir()
                executable = root / "cap-gpui"
                executable.write_bytes(b"fake executable")
                binary = {"variant": "baseline", "executable": "cap-gpui",
                          "sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
                child = Mock(pid=42)

                def launch(command, **kwargs):
                    directory = output / "baseline"
                    if "--run" in command:
                        (directory / "fixture-ready.json").write_text(json.dumps({"visible": True, "pixelWidth": 96, "pixelHeight": 64}))
                        (directory / "fixture-hidden.json").write_text(json.dumps({"hiddenUTC": "2026-10-20T00:00:00Z"}))
                        return child
                    self.assertEqual("DYLD_PRINT_LIBRARIES" in kwargs["env"], recording)
                    kwargs["stdout"].write('2026-10-20T00:00:00.200Z INFO CAP_BENCH_DRAW kind="preview" elapsed_ms=200 bounds=same\n'
                                           '2026-10-20T00:00:00.300Z INFO CAP_BENCH_DRAW kind="gpu" elapsed_ms=300 bounds=same\n'
                                           'screenshot editor window closed\n')
                    kwargs["stdout"].flush()
                    (directory / "capture-saved").write_text("ready")
                    capture_path = directory / "profile/screenshots/test/original.png"
                    capture_path.parent.mkdir(parents=True)
                    capture_path.write_bytes(b"image")
                    (directory / "editor-evidence.json").write_text(json.dumps({"containsFixtureColors": True, "ownerPID": 42}))
                    return child

                image = {"fixtureMatches": True, "containsFixtureColors": True, "width": 96, "height": 64}
                with patch.object(benchmark.smoke.subprocess, "Popen", side_effect=launch), patch.object(
                    benchmark.smoke, "inspect_image", return_value=image,
                ), patch.object(benchmark.smoke, "terminate"), contextlib.redirect_stdout(io.StringIO()):
                    kwargs = {} if recording else {"capture_editor": False}
                    record = benchmark.smoke.run_variant(root, output, root / "fixture", copy.deepcopy(binary), **kwargs)
                self.assertTrue(record["passed"], record.get("error"))
                self.assertEqual((output / "baseline/editor-pid").exists(), recording)
                self.assertEqual(record["editorImageRecording"], recording)


if __name__ == "__main__":
    unittest.main(verbosity=2)
