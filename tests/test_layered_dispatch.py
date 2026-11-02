"""Talk vs Agent dispatch: the two cases where routing is enforcement."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.brain import get_brain
from main import JarvisLive
from memory.memory_manager import LayeredMemoryStore, format_layered_memory_for_prompt


def _live(text: str) -> JarvisLive:
    live = JarvisLive.__new__(JarvisLive)
    live._last_user_text = text
    live._brain = get_brain()
    live._workspace_scope = "Mark-LV@test"
    live._active_agent_scope = ""
    live._active_task_scope = ""
    return live


class TestToolBlocking(unittest.TestCase):
    def test_cancel_blocks_every_tool(self):
        live = _live("stop")
        for tool in ("computer_control", "web_search", "save_memory", "orchestrate_task"):
            with self.subTest(tool=tool):
                self.assertIn("asked to stop", live._tool_block_reason(tool))

    def test_dictation_blocks_desktop_automation(self):
        live = _live("dictate this into the email field")
        self.assertIn("dictation", live._tool_block_reason("computer_control"))
        self.assertIn("dictation", live._tool_block_reason("browser_control"))
        self.assertIn("dictation", live._tool_block_reason("orchestrate_task"))
        # Memory and vision are not desktop automation.
        self.assertEqual(live._tool_block_reason("save_memory"), "")
        self.assertEqual(live._tool_block_reason("screen_process"), "")

    def test_ordinary_request_is_not_blocked(self):
        live = _live("what is the weather in Istanbul?")
        self.assertEqual(live._tool_block_reason("computer_control"), "")

    def test_no_transcript_means_no_block(self):
        live = _live("")
        self.assertEqual(live._tool_block_reason("computer_control"), "")


class TestMemoryScopeSelection(unittest.TestCase):
    def test_workspace_layer_uses_current_repo(self):
        live = _live("")
        live._workspace_scope = "Mark-LV@test"
        self.assertEqual(live._scope_for_layer("workspace"), "Mark-LV@test")

    def test_task_layer_uses_active_agent_workspace(self):
        live = _live("")
        live._active_agent_scope = "fix-parser-1"
        self.assertEqual(live._scope_for_layer("task"), "fix-parser-1")
        self.assertEqual(live._scope_for_layer("agent"), "fix-parser-1")

    def test_task_layer_without_workspace_stays_out_of_personal(self):
        live = _live("")
        self.assertEqual(live._scope_for_layer("task"), "session")
        self.assertEqual(live._scope_for_layer("personal"), "")


class TestLayeredPrompt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.store = LayeredMemoryStore(
            path=base / "layers.json", backup_path=base / "layers.json.bak")
        self.patch = patch(
            "memory.memory_manager._default_layer_store", self.store)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_blocks_are_labelled_and_ordered(self):
        self.store.remember("workspace", "branch", "feature/memory", scope="repo")
        self.store.remember("task", "finding", "one test fails", scope="job")
        self.store.remember("agent", "goal", "finish the memory layer", scope="job")

        block = format_layered_memory_for_prompt(
            workspace="repo", task="job", agent="job")
        self.assertIn("WORKSPACE MEMORY", block)
        self.assertIn("TASK MEMORY", block)
        self.assertIn("AGENT MEMORY", block)
        self.assertLess(block.index("WORKSPACE MEMORY"), block.index("TASK MEMORY"))
        self.assertLess(block.index("TASK MEMORY"), block.index("AGENT MEMORY"))

    def test_empty_scopes_produce_no_empty_headers(self):
        block = format_layered_memory_for_prompt(workspace="", task="", agent="")
        self.assertNotIn("TASK MEMORY", block)
        self.assertNotIn("AGENT MEMORY", block)


if __name__ == "__main__":
    unittest.main()
