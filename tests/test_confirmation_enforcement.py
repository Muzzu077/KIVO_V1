"""Confirmation must be enforced by the registry, not by each handler.

The old path set `parameters["_policy_requires_confirm"] = True` and called the
handler anyway. Only computer_settings read the flag, so file deletion, shell
execution and message sending all ran with no human involved. These tests pin
the new behaviour: a CONFIRM action does not execute until resolve(True).
"""
import unittest
from unittest.mock import patch

from core.action_loader import ActionRegistry, ActionRecord
from core import confirm
from core.action_policy import Control, Risk


def _record(name: str, handler) -> ActionRecord:
    return ActionRecord(name=name, file=f"{name}.py", handler=handler, valid=True)


class TestRegistryConfirmation(unittest.TestCase):
    def setUp(self):
        self.registry = ActionRegistry.__new__(ActionRegistry)
        self.registry._actions = {}
        self.registry._logger = lambda _msg: None
        self.ran = []
        confirm.bind(
            show=lambda title, detail: None,
            hide=lambda: None,
            log=lambda _msg: None,
        )
        # Drop any leftover pending banner between tests.
        confirm.resolve(False)

    def tearDown(self):
        confirm.resolve(False)

    def _register(self, name, handler):
        self.registry._actions[name] = _record(name, handler)

    def test_confirm_action_is_parked(self):
        def delete(parameters=None):
            self.ran.append(parameters)
            return "deleted"

        self._register("file_controller", delete)
        with patch("core.action_policy.check",
                   return_value=(Risk.HIGH, Control.CONFIRM, None)), \
             patch("core.action_policy.record_execution"):
            result = self.registry.run("file_controller",
                                       {"action": "delete", "path": "/tmp/x"})

        self.assertIn("CONFIRMATION_PENDING", result)
        self.assertEqual(self.ran, [], "handler ran before confirmation")

        confirm.resolve(True)
        # resolve() runs the callable on a worker thread
        import time
        for _ in range(50):
            if self.ran:
                break
            time.sleep(0.01)
        self.assertEqual(len(self.ran), 1)
        self.assertTrue(self.ran[0].get("_policy_confirmed"))

    def test_cancel_does_not_execute(self):
        def delete(parameters=None):
            self.ran.append(parameters)
            return "deleted"

        self._register("file_controller", delete)
        with patch("core.action_policy.check",
                   return_value=(Risk.HIGH, Control.CONFIRM, None)), \
             patch("core.action_policy.record_execution"):
            self.registry.run("file_controller", {"action": "delete"})

        confirm.resolve(False)
        import time
        time.sleep(0.05)
        self.assertEqual(self.ran, [])

    def test_safe_action_runs_immediately(self):
        def search(parameters=None):
            self.ran.append(parameters)
            return "ok"

        self._register("web_search", search)
        with patch("core.action_policy.check",
                   return_value=(Risk.SAFE, Control.LOG, None)), \
             patch("core.action_policy.record_execution"):
            result = self.registry.run("web_search", {"query": "x"})

        self.assertEqual(result, "ok")
        self.assertEqual(len(self.ran), 1)

    def test_denied_action_never_runs(self):
        def search(parameters=None):
            self.ran.append(parameters)
            return "ok"

        self._register("web_search", search)
        with patch("core.action_policy.check",
                   return_value=(Risk.CRITICAL, Control.DENY, "Denied by policy")):
            result = self.registry.run("web_search", {"query": "x"})

        self.assertEqual(result, "Denied by policy")
        self.assertEqual(self.ran, [])


if __name__ == "__main__":
    unittest.main()
