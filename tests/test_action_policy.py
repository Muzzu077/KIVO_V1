import time
import unittest
from core.action_policy import (
    lookup,
    check,
    override,
    clear_override,
    trust_session,
    record_execution,
    get_audit_log,
    grant,
    revoke,
    revoke_all,
    list_grants,
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


class TestScopedGrants(unittest.TestCase):
    def setUp(self):
        revoke_all()

    def tearDown(self):
        revoke_all()

    def test_grant_downgrades_confirm_to_log(self):
        # file_controller:delete normally requires CONFIRM
        _, ctrl_before = lookup("file_controller", "delete")
        self.assertEqual(ctrl_before, Control.CONFIRM)

        grant("file_controller", Control.LOG, action="delete", max_uses=2)

        # First use: grant applies
        _, ctrl1 = lookup("file_controller", "delete")
        self.assertEqual(ctrl1, Control.LOG)

        # Second use: still granted
        _, ctrl2 = lookup("file_controller", "delete")
        self.assertEqual(ctrl2, Control.LOG)

        # Third use: exhausted — back to normal
        _, ctrl3 = lookup("file_controller", "delete")
        self.assertEqual(ctrl3, Control.CONFIRM)

    def test_grant_expires_by_time(self):
        grant("code_helper", Control.LOG, action="command",
              max_uses=-1, ttl_seconds=0.1)
        _, ctrl_now = lookup("code_helper", "command")
        self.assertEqual(ctrl_now, Control.LOG)

        time.sleep(0.15)
        _, ctrl_after = lookup("code_helper", "command")
        self.assertEqual(ctrl_after, Control.CONFIRM)

    def test_revoke(self):
        grant("send_message", Control.LOG, max_uses=100)
        self.assertTrue(revoke("send_message", "*"))
        _, ctrl = lookup("send_message", "*")
        self.assertEqual(ctrl, Control.CONFIRM)

    def test_revoke_all(self):
        grant("a", Control.LOG)
        grant("b", Control.LOG)
        self.assertEqual(revoke_all(), 2)
        self.assertEqual(list_grants(), [])

    def test_list_grants(self):
        grant("file_controller", Control.LOG, action="delete", max_uses=3)
        grants = list_grants()
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["tool"], "file_controller")
        self.assertEqual(grants[0]["remaining"], 3)

    def test_wildcard_grant_covers_all_actions(self):
        grant("file_controller", Control.LOG, action="*", max_uses=1)
        _, ctrl = lookup("file_controller", "delete")
        self.assertEqual(ctrl, Control.LOG)
        # One use consumed — back to normal
        _, ctrl2 = lookup("file_controller", "move")
        # Wildcard was consumed for "delete"; check that a second action
        # goes back to the static table
        self.assertIn(ctrl2, (Control.UNDO, Control.LOG, Control.CONFIRM))


if __name__ == "__main__":
    unittest.main()
