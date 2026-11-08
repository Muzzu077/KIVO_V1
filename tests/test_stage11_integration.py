"""Stage 11.0/11.1 integration checks: allowlist, prompt, and disclosure boundaries."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from core.action_loader import ActionRecord
from kivo.agent import create_default_tool_registry
from kivo.agent.approval_gate import ApprovalGate
from kivo.agent.tools.system_info import handle_get_system_overview
from kivo.conversation.prompt_manager import get_system_prompt


class FakeActionRegistry:
    def __init__(self):
        self.records = {
            "web_search": ActionRecord(
                name="web_search",
                description="legacy",
                valid=True,
                handler=lambda parameters=None, **kwargs: "ok",
                parameters={
                    "type": "OBJECT",
                    "properties": {"query": {"type": "STRING"}},
                    "required": ["query"],
                },
            ),
            "desktop_context": ActionRecord(
                name="desktop_context",
                description="legacy",
                valid=True,
                handler=lambda parameters=None, **kwargs: "ok",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "action": {"type": "STRING"},
                        "text": {"type": "STRING"},
                    },
                    "required": ["action"],
                },
            ),
            "file_controller": ActionRecord(
                name="file_controller",
                description="legacy mutation",
                valid=True,
                handler=lambda parameters=None, **kwargs: "ok",
                parameters={
                    "type": "OBJECT",
                    "properties": {"action": {"type": "STRING"}},
                    "required": ["action"],
                },
            ),
            "browser_control": ActionRecord(
                name="browser_control",
                description="legacy browser",
                valid=True,
                handler=lambda parameters=None, **kwargs: "ok",
                parameters={
                    "type": "OBJECT",
                    "properties": {"action": {"type": "STRING"}},
                    "required": ["action"],
                },
            ),
            "computer_control": ActionRecord(
                name="computer_control",
                description="legacy input",
                valid=True,
                handler=lambda parameters=None, **kwargs: "ok",
                parameters={
                    "type": "OBJECT",
                    "properties": {"action": {"type": "STRING"}},
                    "required": ["action"],
                },
            ),
        }

    def get_record(self, name):
        return self.records.get(name)

    def run(self, name, parameters, ctx=None, *, preauthorized=False):
        return "ok"


class Stage11RegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = create_default_tool_registry(
            approval_gate=ApprovalGate(timeout_seconds=1.0),
            action_registry=FakeActionRegistry(),
        )
        self.names = set(self.registry.list_tool_names())

    def test_only_stage_11_read_only_legacy_tools_are_exposed(self):
        self.assertIn("web_search", self.names)
        self.assertIn("list_open_windows", self.names)
        self.assertIn("get_system_overview", self.names)
        self.assertIn("get_current_time", self.names)
        self.assertIn("run_readonly_command", self.names)
        self.assertIn("list_directory", self.names)
        self.assertIn("get_file_metadata", self.names)
        self.assertIn("search_filenames", self.names)
        self.assertIn("get_disk_usage", self.names)
        self.assertIn("read_text_file", self.names)

        for excluded in (
            "desktop_context",
            "file_controller",
            "browser_control",
            "computer_control",
            "file_processor",
            "code_helper",
            "terminal_control",
            "write_file",
            "create_file",
            "delete_file",
        ):
            self.assertNotIn(excluded, self.names)

    def test_web_search_discloses_external_processing(self):
        tool = self.registry.get_tool("web_search")
        lowered = tool.description.lower()
        self.assertIn("external", lowered)
        self.assertIn("provider", lowered)
        self.assertIn("untrusted", lowered)

    def test_desktop_tool_has_no_interaction_capabilities(self):
        tool = self.registry.get_tool("list_open_windows")
        schema_text = json.dumps(tool.parameters).lower()
        description = tool.description.lower()
        for forbidden in ("type_text", "press_key", "click", "ocr", "screenshot", "screen_text"):
            self.assertNotIn(forbidden, schema_text)
            self.assertNotIn(forbidden, description)


class Stage11PromptAndTelemetryTest(unittest.TestCase):
    def test_prompt_mentions_new_tools_and_untrusted_boundary(self):
        prompt = get_system_prompt().lower()
        self.assertIn("web_search", prompt)
        self.assertIn("list_open_windows", prompt)
        self.assertIn("untrusted", prompt)
        self.assertIn("search provider", prompt)
        self.assertIn("list_directory", prompt)
        self.assertIn("read_text_file", prompt)
        self.assertIn("approved workspace", prompt)

    def test_system_overview_does_not_expose_environment(self):
        with patch.dict(os.environ, {"KIVO_TEST_SECRET": "super-secret-value"}):
            overview = handle_get_system_overview({})
        serialized = json.dumps(overview).lower()
        self.assertNotIn("kivo_test_secret", serialized)
        self.assertNotIn("super-secret-value", serialized)
        self.assertNotIn('"env"', serialized)
        self.assertNotIn("environment", serialized)


if __name__ == "__main__":
    unittest.main()
