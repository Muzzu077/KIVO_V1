"""Agent workspaces: a job gets its own directory and memory scope."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.agent_workspace import (
    close_workspace,
    create_workspace,
    list_workspaces,
    load_workspace,
    record_finding,
    workspace_memory_block,
)
from core.task_orchestrator import TaskOrchestrator
from memory.memory_manager import LayeredMemoryStore


class TestAgentWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = LayeredMemoryStore(
            path=self.base / "layers.json",
            backup_path=self.base / "layers.json.bak",
        )
        self.env = patch.dict(os.environ, {"KIVO_AGENTS_DIR": str(self.base)})
        self.env.start()
        self.store_patch = patch(
            "memory.memory_manager._default_layer_store", self.store)
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_create_load_list(self):
        ws = create_workspace("Fix the parser", base_dir=self.base)
        self.assertTrue(ws.root.is_dir())
        self.assertTrue((ws.root / "workspace.json").exists())

        loaded = load_workspace(ws.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.goal, "Fix the parser")

        listed = list_workspaces(base_dir=self.base)
        self.assertEqual([w.id for w in listed], [ws.id])

    def test_task_and_agent_memory_are_scoped(self):
        ws = create_workspace("Fix the parser", base_dir=self.base)
        record_finding(ws, "error", "SyntaxError on line 4")
        record_finding(ws, "convention", "uses pytest", durable=True)

        task = self.store.snapshot("task", ws.scope)
        agent = self.store.snapshot("agent", ws.scope)
        self.assertEqual(task["error"], "SyntaxError on line 4")
        self.assertNotIn("error", agent)
        self.assertEqual(agent["convention"], "uses pytest")

    def test_prompt_block_contains_goal_and_findings(self):
        ws = create_workspace("Fix the parser", base_dir=self.base)
        record_finding(ws, "error", "SyntaxError on line 4")
        block = workspace_memory_block(ws)
        self.assertIn("Fix the parser", block)
        self.assertIn("SyntaxError on line 4", block)
        self.assertIn("TASK MEMORY", block)

    def test_close_marks_status(self):
        ws = create_workspace("Ship it", base_dir=self.base)
        close_workspace(ws)
        self.assertEqual(load_workspace(ws.id).status, "closed")
        self.assertEqual(self.store.snapshot("agent", ws.scope)["status"], "closed")

    def test_sensitive_finding_not_in_prompt(self):
        ws = create_workspace("Handle credentials", base_dir=self.base)
        record_finding(ws, "token", "secret-value", sensitive=True)
        record_finding(ws, "note", "visible")
        block = workspace_memory_block(ws)
        self.assertNotIn("secret-value", block)
        self.assertIn("visible", block)


class TestOrchestratorWorkspaceIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = LayeredMemoryStore(
            path=self.base / "layers.json",
            backup_path=self.base / "layers.json.bak",
        )
        self.env = patch.dict(os.environ, {"KIVO_AGENTS_DIR": str(self.base)})
        self.env.start()
        self.store_patch = patch(
            "memory.memory_manager._default_layer_store", self.store)
        self.store_patch.start()

        class FakeRegistry:
            def run(self, name, parameters, ctx):
                return f"ran {name}"

        self.orchestrator = TaskOrchestrator.get_instance()
        self.orchestrator.bind_registry(FakeRegistry())
        self.orchestrator._active_tasks.clear()

    def tearDown(self):
        self.store_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_plan_records_findings_in_its_workspace(self):
        from actions.task_orchestrator_action import task_orchestrator_handler

        report = task_orchestrator_handler({
            "goal": "Check the build",
            "steps": [
                {"tool": "computer_settings", "description": "check version"},
                {"tool": "system_monitor", "description": "check memory"},
            ],
        })
        self.assertIn("Workspace:", report)
        workspaces = list_workspaces(base_dir=self.base)
        self.assertEqual(len(workspaces), 1)
        task = self.store.snapshot("task", workspaces[0].scope)
        self.assertIn("goal", task)
        self.assertIn("result", task)
        self.assertIn("step_1", task)


if __name__ == "__main__":
    unittest.main()
