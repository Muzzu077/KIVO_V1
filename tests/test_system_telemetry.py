import unittest
from actions.system_monitor import get_system_status, SystemMonitor
from actions.desktop_context_action import desktop_context_handler


class TestSystemTelemetry(unittest.TestCase):
    def test_get_system_status_structure(self):
        status = get_system_status()
        self.assertIsInstance(status, dict)
        self.assertIn("cpu_percent", status)
        self.assertIn("ram_percent", status)
        self.assertIn("uptime", status)
        self.assertIn("summary", status)
        self.assertIsInstance(status["summary"], str)
        self.assertTrue(len(status["summary"]) > 0)

        # Check telemetry fields
        if status.get("cpu_temp_c") is not None:
            self.assertGreater(status["cpu_temp_c"], 0)
        if status.get("laptop_temp_c") is not None:
            self.assertGreater(status["laptop_temp_c"], 0)

    def test_system_monitor_check(self):
        monitor = SystemMonitor()
        alert = monitor.check()
        # Normal system state should not trigger immediate critical alert
        self.assertTrue(alert is None or isinstance(alert, str))

    def test_desktop_context_actions(self):
        res_summary = desktop_context_handler({"action": "summary"})
        self.assertIsInstance(res_summary, str)

        res_windows = desktop_context_handler({"action": "list_windows"})
        self.assertIsInstance(res_windows, str)

        res_terminals = desktop_context_handler({"action": "list_terminals"})
        self.assertIsInstance(res_terminals, str)

        res_status = desktop_context_handler({"action": "monitor_status"})
        self.assertIn("Screen monitoring", res_status)

        res_text = desktop_context_handler({"action": "screen_text"})
        self.assertIsInstance(res_text, str)


if __name__ == "__main__":
    unittest.main()
