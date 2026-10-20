import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analysis = load("transition_analysis", "analyze.py")
runner = load("transition_runner", "run.py")
BASE = analysis.utc("2026-10-20T00:00:00Z")


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "frames").mkdir()
        self.report = {"completed": True, "readyUTC": BASE - 1,
            "geometry": {"width": 96, "height": 64, "scale": 1, "displayOriginX": 0, "displayOriginY": 0},
            "stream": {"errors": [], "unusableEvents": []}, "frames": [],
            "editorHistory": [{"utc": BASE + 0.16, "pid": 456, "windowNumber": 77,
                               "bounds": {"X": 8, "Y": 8, "Width": 80, "Height": 48}, "alpha": 1}]}
        self.capture = {"passed": True, "editorImageRecording": False, "ownedPID": 456,
            "input": {"fixtureMatches": True, "containsFixtureColors": True}, "inputPNGsha256": "source-hash",
            "diagnosticPreview": {"utc": "2026-10-20T00:00:00.200Z", "bounds": "same"},
            "diagnosticGPU": {"utc": "2026-10-20T00:00:00.350Z", "bounds": "same"},
            "log": "2026-10-20T00:00:00.100Z INFO CAP_BENCH_CAPTURE\nscreenshot editor window opened number=77\n"}
        self.pattern = np.zeros((64, 96, 3), dtype=np.uint8)
        self.pattern[8:56, 8:35] = [237, 40, 39]
        self.pattern[8:56, 35:61] = [15, 75, 235]
        self.pattern[8:56, 61:88] = [0, 209, 66]
        for index in range(60):
            name = f"frames/{index:04d}.png"
            pixels = self.pattern if index >= 9 else np.zeros_like(self.pattern)
            Image.fromarray(pixels).save(self.root / name)
            self.report["frames"].append({"file": name, "utc": BASE + index * 0.02, "freshPixels": index in (0, 9, 18)})

    def test_stable_recording_produces_review_evidence_not_a_flash_free_verdict(self):
        result = analysis.transition(self.root, self.report, self.capture)
        self.assertEqual(result["status"], "evidence-ready-for-review")
        self.assertTrue(result["manualVisualReviewRequired"])
        self.assertFalse(result["physicalDisplayVerified"])
        self.assertGreaterEqual(result["rawPreviewSamples"], 2)
        self.assertGreaterEqual(result["postGPUSamples"], 2)
        self.assertEqual(result["possibleBlankFrames"], [])
        self.assertTrue(all(frame["wholeWindowChangedPixels"] == 0 for frame in result["frames"]))
        self.assertTrue((self.root / "transition-filmstrip.png").exists())

    def test_captured_blank_between_preview_and_gpu_is_flagged(self):
        Image.fromarray(np.zeros_like(self.pattern)).save(self.root / "frames/0015.png")
        result = analysis.transition(self.root, self.report, self.capture)
        self.assertEqual(result["status"], "possible-visual-discontinuity")
        self.assertEqual(result["possibleBlankFrames"], [15])

    def test_small_color_changes_are_reported_for_manual_review(self):
        changed = self.pattern.copy()
        changed[12:20, 12:20, 0] -= 1
        Image.fromarray(changed).save(self.root / "frames/0010.png")
        result = analysis.transition(self.root, self.report, self.capture)
        frame = next(frame for frame in result["frames"] if frame["frame"] == 10)
        self.assertEqual(frame["wholeWindowChangedPixels"], 64)
        self.assertEqual(frame["wholeWindowMaxChannelDifference"], 1)
        self.assertTrue(result["manualVisualReviewRequired"])

    def test_missing_raw_preview_is_inconclusive(self):
        self.report["frames"] = self.report["frames"][18:]
        with self.assertRaisesRegex(analysis.Inconclusive, "missed"):
            analysis.transition(self.root, self.report, self.capture)

    def test_missing_post_gpu_samples_and_coalesced_callbacks_are_inconclusive(self):
        report = copy.deepcopy(self.report)
        report["frames"] = report["frames"][:19]
        with self.assertRaisesRegex(analysis.Inconclusive, "missed"):
            analysis.transition(self.root, report, self.capture)
        capture = copy.deepcopy(self.capture)
        capture["diagnosticGPU"]["utc"] = "2026-10-20T00:00:00.201Z"
        with self.assertRaisesRegex(analysis.Inconclusive, "separation"):
            analysis.transition(self.root, self.report, capture)

    def test_sampling_gap_is_inconclusive_instead_of_a_pass(self):
        self.report["frames"] = [row for index, row in enumerate(self.report["frames"]) if index not in (12, 13, 14)]
        with self.assertRaisesRegex(analysis.Inconclusive, "sampling gap"):
            analysis.transition(self.root, self.report, self.capture)

    def test_unusable_stream_event_during_transition_is_inconclusive(self):
        self.report["stream"]["unusableEvents"] = [{"utc": BASE + 0.3, "status": 2}]
        with self.assertRaisesRegex(analysis.Inconclusive, "unusable"):
            analysis.transition(self.root, self.report, self.capture)

    def test_wrong_owner_moving_window_and_missing_geometry_are_rejected(self):
        for change in [{"pid": 999}, {"windowNumber": 88}, {"alpha": 0.5}, {"utc": BASE + 0.4}]:
            report = copy.deepcopy(self.report)
            report["editorHistory"][0].update(change)
            with self.subTest(change=change), self.assertRaises(analysis.Inconclusive):
                analysis.transition(self.root, report, self.capture)
        report = copy.deepcopy(self.report)
        moving = copy.deepcopy(report["editorHistory"][0])
        moving.update({"utc": BASE + 0.3, "bounds": {"X": 9, "Y": 8, "Width": 80, "Height": 48}})
        report["editorHistory"].append(moving)
        with self.assertRaisesRegex(analysis.Inconclusive, "bounds changed"):
            analysis.transition(self.root, report, self.capture)

    def test_capture_without_pattern_or_matching_draw_bounds_is_rejected(self):
        for change in [{"input": {"fixtureMatches": False}}, {"diagnosticGPU": {"utc": "2026-10-20T00:00:00.350Z", "bounds": "different"}}]:
            with self.subTest(change=change), self.assertRaises(analysis.Inconclusive):
                analysis.transition(self.root, self.report, self.capture | change)

    def test_missing_clock_alignment_and_unsafe_image_paths_are_rejected(self):
        for change in [{"stream": {"errors": ["PTS not aligned"]}}, {"geometry": self.report["geometry"] | {"scale": 2}},
                       {"completed": False}, {"readyUTC": BASE + 0.3}]:
            with self.subTest(change=change), self.assertRaises(analysis.Inconclusive):
                analysis.transition(self.root, self.report | change, self.capture)
        self.report["frames"][0]["file"] = "../outside.png"
        with self.assertRaisesRegex(analysis.Inconclusive, "escapes"):
            analysis.transition(self.root, self.report, self.capture)

    def test_duplicate_timestamps_cannot_inflate_phase_coverage(self):
        self.report["frames"][10]["utc"] = self.report["frames"][9]["utc"]
        with self.assertRaisesRegex(analysis.Inconclusive, "strictly increasing"):
            analysis.transition(self.root, self.report, self.capture)

    def test_native_calibration_requires_blue_green_blue_pixels_at_the_recorded_times(self):
        report = copy.deepcopy(self.report)
        report["phaseEvents"] = [{"name": "blue", "utc": BASE}, {"name": "green", "utc": BASE + 0.3},
                                 {"name": "blue-again", "utc": BASE + 0.65}]
        for index, frame in enumerate(report["frames"]):
            image = np.zeros_like(self.pattern)
            image[:] = [0, 209, 66] if 0.3 <= index * 0.02 < 0.65 else [15, 75, 235]
            Image.fromarray(image).save(self.root / frame["file"])
        result = analysis.self_test(self.root, report)
        self.assertEqual(result["status"], "calibration-passed")
        Image.fromarray(np.zeros_like(self.pattern)).save(self.root / report["frames"][25]["file"])
        with self.assertRaisesRegex(analysis.Inconclusive, "Pixel/time mismatch"):
            analysis.self_test(self.root, report)


class RunnerTests(unittest.TestCase):
    def prepare_smoke(self, root):
        (root / "package.json").write_text(json.dumps({"repository": "dovidmoishe/Cap", "buildRun": 39426190700,
                                                      "buildCommit": "test-build", "platform": "test-macOS"}))
        smoke = Mock()
        smoke.load_package.return_value = {"binaries": [{"variant": "revised", "sha256": "test-executable-hash"}], "revised_snapshot": {"test": True}}
        return smoke

    def test_wrong_recorder_owner_and_missing_ready_do_not_launch_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ready.json").write_text(json.dumps({"pid": 999, "patternVisible": True, "geometry": {"backgroundLevel": -1}}))
            with self.assertRaises(ValueError):
                runner.wait_ready(root, Mock(pid=123))
            (root / "ready.json").unlink()
            child = Mock()
            child.poll.return_value = 2
            with self.assertRaises(RuntimeError):
                runner.wait_ready(root, child)

    def test_failed_cap_capture_preserves_failure_and_terminates_owned_recorder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            smoke = self.prepare_smoke(root)
            smoke.run_variant.return_value = {"passed": False, "error": "capture failure"}
            child = Mock()
            with patch.object(runner, "load_smoke", return_value=smoke), patch.object(runner, "wait_ready", return_value={"pid": 123}), \
                    patch.object(runner.subprocess, "Popen", return_value=child):
                report = runner.collect(root, root, root / "fixture", root / "recorder")
            self.assertEqual(report["status"], "inconclusive")
            self.assertIn("capture failure", report["error"])
            self.assertEqual(json.loads((root / "transition.json").read_text())["status"], "inconclusive")
            smoke.terminate.assert_called_once_with(child)
            child.wait.assert_not_called()

    def test_success_collects_only_revised_visual_evidence_and_preserves_package_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            smoke = self.prepare_smoke(root)
            smoke.run_variant.return_value = {"passed": True}
            child = Mock(returncode=0)

            def analyze(command, **kwargs):
                self.assertIn("--capture", command)
                self.assertEqual(command[-1], str(root / "capture/revised"))
                (root / "recording/analysis.json").write_text(json.dumps({"status": "evidence-ready-for-review", "manualVisualReviewRequired": True}))
                return runner.subprocess.CompletedProcess(command, 0, "evidence ready", "")

            with patch.object(runner, "load_smoke", return_value=smoke), patch.object(runner, "wait_ready", return_value={"pid": 123}), \
                    patch.object(runner.subprocess, "Popen", return_value=child), patch.object(runner.subprocess, "run", side_effect=analyze):
                report = runner.collect(root, root, root / "fixture", root / "recorder")
            self.assertEqual(report["status"], "evidence-ready-for-review")
            self.assertTrue(report["analysis"]["manualVisualReviewRequired"])
            self.assertEqual(report["package"]["buildRun"], 39426190700)
            self.assertEqual(report["revisedExecutableSHA256"], "test-executable-hash")
            self.assertEqual(smoke.run_variant.call_args.kwargs, {"capture_editor": False, "desktop": True})
            self.assertEqual(smoke.run_variant.call_args.args[3]["variant"], "revised")
            child.wait.assert_called_once_with(timeout=30)
            smoke.terminate.assert_called_once_with(child)


if __name__ == "__main__":
    unittest.main(verbosity=2)
