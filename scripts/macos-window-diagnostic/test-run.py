import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("diagnostic", Path(__file__).with_name("run.py"))
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def observed(matched, **state):
    return {"matched": matched, "state": state}


class DiagnosticTests(unittest.TestCase):
    def test_unavailable_window_list_cannot_prove_visibility(self):
        self.assertFalse(diagnostic.hidden({"hidden": True, "visibleWindowCount": 0}))
        self.assertFalse(diagnostic.visible({"hidden": False, "visibleWindowCount": 1}))
        self.assertFalse(diagnostic.hidden({"hidden": True, "visibleWindowCount": 1, "windowListAvailable": True}))
        self.assertTrue(diagnostic.hidden({"hidden": True, "visibleWindowCount": 0, "windowListAvailable": True}))

    def run_case(self, observations):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "case"
            child = Mock()
            child.poll.return_value = None
            with patch.object(diagnostic, "observe", side_effect=observations), \
                    patch.object(diagnostic, "command") as command, \
                    patch.object(diagnostic.subprocess, "Popen", return_value=child), \
                    patch.object(diagnostic.os, "kill") as kill:
                result = diagnostic.run_case(Path("probe"), directory, "raw", "prohibited", "accessory")
                self.assertEqual(json.loads((directory / "result.json").read_text()), result)
                kill.assert_called_once_with(123, diagnostic.signal.SIGTERM)
                return result, [call.args[1] for call in command.call_args_list]

    def initial(self):
        return [observed(True, **{"self": {"pid": 123}}),
                observed(True, **{"self": {"windowListAvailable": True, "visibleWindowCount": 0}})]

    def test_remote_method_requires_observed_hiding_and_reopening(self):
        result, commands = self.run_case([*self.initial(), observed(True, hideReturned=False), observed(True)])
        self.assertTrue(result["diagnosticComplete"])
        self.assertTrue(result["remoteMethodSupported"])
        self.assertFalse(result["remoteHide"]["state"]["hideReturned"])
        self.assertEqual(commands, ["remote-hide", "open-editor"])

    def test_self_hide_and_explicit_unhide_do_not_validate_remote_method(self):
        result, commands = self.run_case([*self.initial(), observed(False), observed(True), observed(False), observed(True)])
        self.assertTrue(result["diagnosticComplete"])
        self.assertFalse(result["remoteMethodSupported"])
        self.assertTrue(result["reopenExplicitly"]["matched"])
        self.assertEqual(commands, ["remote-hide", "self-hide", "open-editor", "explicit-unhide"])

    def test_failed_hiding_is_a_completed_diagnostic_without_reopen(self):
        result, commands = self.run_case([*self.initial(), observed(False), observed(False)])
        self.assertTrue(result["diagnosticComplete"])
        self.assertFalse(result["remoteMethodSupported"])
        self.assertNotIn("reopenByActivation", result)
        self.assertEqual(commands, ["remote-hide", "self-hide"])

    def test_crashed_process_is_incomplete_and_preserves_error(self):
        result, _ = self.run_case([*self.initial(), RuntimeError("process exited")])
        self.assertFalse(result["diagnosticComplete"])
        self.assertEqual(result["error"], "process exited")

    def test_transient_hidden_state_does_not_satisfy_stability(self):
        child = Mock()
        child.poll.return_value = None
        clock = iter([0, 0, 0, 0.1, 0.1, 0.2, 0.2, 0.3, 0.3, 0.4])
        with patch.object(diagnostic.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(diagnostic.time, "sleep"), \
                patch.object(diagnostic, "read_state", side_effect=[{"ok": True}, {"ok": False}, {"ok": True}, {"ok": True}]):
            result = diagnostic.observe(Path("unused"), lambda state: state["ok"], [child], timeout=0.35, stable=0.25)
        self.assertFalse(result["matched"])


if __name__ == "__main__":
    unittest.main()
