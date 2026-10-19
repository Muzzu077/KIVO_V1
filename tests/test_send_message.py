import unittest
from unittest.mock import patch, MagicMock

from actions.send_message import (
    TOOL,
    send_message,
    _is_phone_number,
    _resolve_platform,
)


class TestSendMessage(unittest.TestCase):
    def test_phone_number_detection(self):
        self.assertTrue(_is_phone_number("+1234567890"))
        self.assertTrue(_is_phone_number("1234567890"))
        self.assertTrue(_is_phone_number("+90 (555) 123-4567"))
        self.assertFalse(_is_phone_number("John Doe"))
        self.assertFalse(_is_phone_number("Mom"))

    def test_platform_resolution(self):
        self.assertIsNotNone(_resolve_platform("whatsapp"))
        self.assertIsNotNone(_resolve_platform("whatsapp_web"))
        self.assertIsNotNone(_resolve_platform("telegram"))
        self.assertIsNotNone(_resolve_platform("discord"))
        self.assertIsNotNone(_resolve_platform("slack"))
        self.assertIsNotNone(_resolve_platform("active_window"))

    @patch("actions.send_message._send_whatsapp")
    def test_send_message_whatsapp(self, mock_wa):
        mock_wa.return_value = "Message sent to Alex via WhatsApp."
        res = send_message({
            "platform": "whatsapp",
            "receiver": "Alex",
            "message_text": "Hey Alex, see you at 5!"
        })
        mock_wa.assert_called_once_with("Alex", "Hey Alex, see you at 5!")
        self.assertIn("sent", res.lower())

    @patch("actions.send_message._send_active_window")
    def test_send_message_active_window(self, mock_active):
        mock_active.return_value = "Typed and sent message into active chat window: 'Meeting in 10'"
        res = send_message({
            "platform": "active_window",
            "message_text": "Meeting in 10"
        })
        mock_active.assert_called_once_with("", "Meeting in 10")
        self.assertIn("active chat window", res)

    def test_validation_empty_message(self):
        res = send_message({
            "platform": "whatsapp",
            "receiver": "Alex",
            "message_text": ""
        })
        self.assertIn("specify the message content", res)

    def test_tool_declaration(self):
        self.assertEqual(TOOL["name"], "send_message")
        self.assertIn("receiver", TOOL["parameters"]["properties"])
        self.assertIn("message_text", TOOL["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
