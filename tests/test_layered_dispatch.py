"""Talk vs Agent dispatch: routing policy and layered memory."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.brain import get_brain
from memory.memory_manager import (
    LayeredMemoryStore,
    format_layered_memory_for_prompt,
    workspace_scope,
)


class TestAutomationBlocking(unittest.TestCase):
    """CANCEL and DICTATION must never reach a desktop automation tool."""

    def setUp(self):
        self.router = get_brain().router

    def _allows(self, text: str) -> bool:
        return self.router.allows_automation(self.router.classify(text))

    def test_cancel_blocks_automation(self):
        for text in ("stop", "cancel everything", "never mind"):
            with self.subTest(text=text):
                decision = self.router.classify(text)
                self.assertEqual(decision.intent.value, "cancel")
                self.assertFalse(self.router.allows_automation(decision))

    def test_dictation_blocks_desktop_automation(self):
        decision = self.router.classify("dictate this into the email field")
        self.assertEqual(decision.intent.value, "dictation")
        self.assertFalse(self.router.allows_automation(decision))

    def test_ordinary_request_is_not_blocked(self):
        self.assertTrue(self._allows("what is the weather in Istanbul?"))

    def test_no_transcript_means_no_block(self):
        self.assertTrue(self._allows(""))


class TestMemoryScopeSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.store = LayeredMemoryStore(
            path=base / "layers.json", backup_path=base / "layers.json.bak")

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_scope_uses_current_repo(self):
        self.assertEqual(workspace_scope(), workspace_scope(os.getcwd()))
        self.assertIn("@", workspace_scope())

    def test_task_scope_is_isolated_from_other_tasks(self):
        self.store.remember("task", "finding", "one test fails", scope="job-1")
        self.assertEqual(len(self.store.recall("task", "job-1")), 1)
        self.assertEqual(self.store.recall("task", "job-2"), [])

    def test_task_scope_does_not_fall_into_personal_memory(self):
        self.store.remember("task", "goal", "finish the job", scope="session")
        rows = self.store.recall("task", "session")
        self.assertEqual(rows[0]["value"], "finish the job")
        # The layered store has no "personal" layer; task work can never
        # silently land in the personal memory bucket.
        self.assertEqual(self.store.recall("personal", "session"), [])


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
