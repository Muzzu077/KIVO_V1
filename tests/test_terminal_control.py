import unittest
import os
from core.terminal_control import TerminalController, _is_shell_idle

class TestTerminalControl(unittest.TestCase):
    def setUp(self):
        self.adapter = TerminalController()

    def test_current_shell_idle_check(self):
        pid = os.getpid()
        is_idle, cmd = _is_shell_idle(pid)
        self.assertIsInstance(is_idle, bool)

    def test_run_command_safe(self):
        res = self.adapter.run_in_subprocess("echo 'KIVO_TEST'")
        self.assertFalse(res.success)
        self.assertIn("disabled for security", res.error)

    def test_terminal_control_action_dispatch(self):
        from actions.terminal_control_action import terminal_control
        out = terminal_control({"action": "execute_bg", "command": "echo 'DISPATCH_OK'"})
        self.assertIn("disabled for security", out)

        status_out = terminal_control({"action": "status"})
        self.assertIsInstance(status_out, str)

        list_out = terminal_control({"action": "list"})
        self.assertIsInstance(list_out, str)

    def test_type_command_validation(self):
        from actions.terminal_control_action import terminal_control
        out = terminal_control({"action": "type", "command": "ls"})
        self.assertIn("disabled for security", out)

        unknown = terminal_control({"action": "invalid_action_xyz"})
        self.assertIn("Unknown terminal action", unknown)

if __name__ == "__main__":
    unittest.main()
