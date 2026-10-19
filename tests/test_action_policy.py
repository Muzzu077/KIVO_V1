import unittest
from core.action_policy import (
    lookup,
    check,
    override,
    clear_override,
    trust_session,
    record_execution,
    get_audit_log,
    Risk,
    Control,
)

class TestActionPolicy(unittest.TestCase):
    def test_default_policies(self):
        # web_search should be safe and logged
        risk, ctrl = lookup("web_search", "*")
        self.assertEqual(risk, Risk.SAFE)
        self.assertEqual(ctrl, Control.LOG)

        # code_helper bash/terminal command check
        risk_c, ctrl_c, denial = check("code_helper", "command", {"command": "echo test"})
        self.assertEqual(risk_c, Risk.CRITICAL)
        self.assertEqual(ctrl_c, Control.CONFIRM)

        # computer_settings shutdown check
        risk_s, ctrl_s = lookup("computer_settings", "shutdown")
        self.assertEqual(risk_s, Risk.CRITICAL)
        self.assertEqual(ctrl_s, Control.CONFIRM)

    def test_overrides(self):
        override("weather_report", Control.DENY, action="current", risk=Risk.CRITICAL)
        risk, ctrl = lookup("weather_report", "current")
        self.assertEqual(risk, Risk.CRITICAL)
        self.assertEqual(ctrl, Control.DENY)

        clear_override("weather_report", "current")
        risk_r, ctrl_r = lookup("weather_report", "current")
        self.assertNotEqual(ctrl_r, Control.DENY)

    def test_trust_mode(self):
        trust_session(True)
        # An action that normally requires CONFIRM should be lowered to LOG under trust mode
        risk, ctrl = lookup("file_controller", "delete")
        self.assertEqual(ctrl, Control.LOG)

        trust_session(False)
        risk_norm, ctrl_norm = lookup("file_controller", "delete")
        self.assertEqual(ctrl_norm, Control.CONFIRM)

    def test_audit_log(self):
        record_execution("desktop_context", "summary", {"action": "summary"}, Risk.SAFE, Control.LOG, outcome="ok", result="done")
        history = get_audit_log(limit=5)
        self.assertGreaterEqual(len(history), 1)
        self.assertEqual(history[0].tool, "desktop_context")
        self.assertEqual(history[0].outcome, "ok")

if __name__ == "__main__":
    unittest.main()
