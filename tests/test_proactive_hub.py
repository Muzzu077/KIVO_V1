import unittest
import time
from unittest.mock import MagicMock

from core.proactive_hub import ProactiveHub, ProactiveEvent, ProactiveEventType
from core.screen_observer import ScreenObservation


class TestProactiveHub(unittest.TestCase):
    def setUp(self):
        self.hub = ProactiveHub()

    def test_emit_and_pop_event(self):
        ev = ProactiveEvent(
            event_type=ProactiveEventType.TERMINAL_FAILURE,
            source="Terminal",
            title="pytest failed",
            details="AssertionError: 1 != 2",
        )
        accepted = self.hub.emit_event(ev, cooldown_seconds=60.0)
        self.assertTrue(accepted)
        self.assertTrue(self.hub.has_pending_events())

        popped = self.hub.pop_next_event()
        self.assertIsNotNone(popped)
        self.assertEqual(popped.title, "pytest failed")
        self.assertFalse(self.hub.has_pending_events())

    def test_cooldown_deduplication(self):
        ev1 = ProactiveEvent(
            event_type=ProactiveEventType.TERMINAL_FAILURE,
            source="Terminal",
            title="Command failed",
            details="error code 1",
        )
        ev2 = ProactiveEvent(
            event_type=ProactiveEventType.TERMINAL_FAILURE,
            source="Terminal",
            title="Command failed",
            details="error code 1",
        )
        accepted1 = self.hub.emit_event(ev1, cooldown_seconds=60.0)
        accepted2 = self.hub.emit_event(ev2, cooldown_seconds=60.0)

        self.assertTrue(accepted1)
        self.assertFalse(accepted2)  # Should be suppressed by cooldown

    def test_listener_callback(self):
        mock_listener = MagicMock()
        self.hub.add_listener(mock_listener)

        ev = ProactiveEvent(
            event_type=ProactiveEventType.SYSTEM_ALERT,
            source="System",
            title="High RAM usage",
            details="Memory > 95%",
        )
        self.hub.emit_event(ev)
        mock_listener.assert_called_once_with(ev)

    def test_screen_error_detection(self):
        traceback_text = (
            "Traceback (most recent call last):\n"
            "  File 'server.py', line 45, in <module>\n"
            "    ZeroDivisionError: division by zero"
        )
        obs = ScreenObservation(
            active_title="server.py - Neovim",
            active_app="alacritty",
            ocr_text_preview=traceback_text,
        )

        self.hub._on_screen_observation(obs)
        self.assertTrue(self.hub.has_pending_events())
        ev = self.hub.pop_next_event()
        self.assertEqual(ev.event_type, ProactiveEventType.SCREEN_ERROR)
        self.assertIn("ZeroDivisionError: division by zero", ev.details)

    def test_build_event_prompt(self):
        ev = ProactiveEvent(
            event_type=ProactiveEventType.SCREEN_ERROR,
            source="Terminal",
            title="Error in build.rs",
            details="error[E0425]: cannot find value `x` in this scope",
            suggested_action="Offer to inspect the compiler error.",
        )
        prompt = self.hub.build_event_prompt(ev)
        self.assertIn("[PROACTIVE_TRIGGER: SCREEN_ERROR]", prompt)
        self.assertIn("error[E0425]", prompt)
        self.assertIn("Rules for speaking:", prompt)


if __name__ == "__main__":
    unittest.main()
