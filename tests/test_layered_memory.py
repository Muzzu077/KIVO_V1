"""Layered memory: personal facts must not leak into task/workspace scopes."""
import json
import tempfile
import unittest
from pathlib import Path

from memory.memory_manager import (
    LayeredMemoryStore,
    LAYER_PROMPT_CHARS,
    workspace_scope,
)


class TestLayeredMemoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.store = LayeredMemoryStore(
            path=base / "layers.json",
            backup_path=base / "layers.json.bak",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_layers_are_isolated(self):
        self.store.remember("workspace", "branch", "main", scope="repo@1234")
        self.store.remember("task", "finding", "the parser fails on empty input", scope="task_1")
        self.store.remember("agent", "goal", "refactor the parser", scope="agent_1")

        self.assertEqual(
            self.store.snapshot("workspace", "repo@1234")["branch"], "main")
        self.assertNotIn("branch", self.store.snapshot("task", "task_1"))
        self.assertNotIn("finding", self.store.snapshot("workspace", "repo@1234"))

    def test_scopes_do_not_share_values(self):
        self.store.remember("workspace", "branch", "main", scope="repo-a")
        self.store.remember("workspace", "branch", "release", scope="repo-b")
        self.assertEqual(self.store.snapshot("workspace", "repo-a")["branch"], "main")
        self.assertEqual(self.store.snapshot("workspace", "repo-b")["branch"], "release")

    def test_survives_a_new_store_instance(self):
        self.store.remember("task", "answer", "42", scope="task_9")
        other = LayeredMemoryStore(
            path=self.store.path, backup_path=self.store.backup_path)
        self.assertEqual(other.snapshot("task", "task_9")["answer"], "42")

    def test_sensitive_values_are_excluded_by_default(self):
        self.store.remember("workspace", "api_key", "sk-secret", scope="repo",
                            sensitive=True)
        self.store.remember("workspace", "branch", "main", scope="repo")

        visible = self.store.snapshot("workspace", "repo")
        self.assertNotIn("api_key", visible)
        self.assertIn("branch", visible)

        prompt = self.store.format_for_prompt("workspace", "repo")
        self.assertNotIn("sk-secret", prompt)

        with_sensitive = self.store.snapshot("workspace", "repo", include_sensitive=True)
        self.assertEqual(with_sensitive["api_key"], "sk-secret")

    def test_prompt_is_bounded(self):
        for i in range(200):
            self.store.remember("task", f"finding_{i}", "x" * 200, scope="task_big")
        prompt = self.store.format_for_prompt("task", "task_big")
        self.assertLessEqual(len(prompt), LAYER_PROMPT_CHARS + 80)
        self.assertIn("TASK MEMORY", prompt)

    def test_forget_and_clear(self):
        self.store.remember("agent", "goal", "ship it", scope="agent_1")
        self.assertTrue(self.store.forget("agent", "goal", "agent_1"))
        self.assertFalse(self.store.forget("agent", "goal", "agent_1"))

        self.store.remember("agent", "note", "keep", scope="agent_1")
        self.store.clear("agent", "agent_1")
        self.assertEqual(self.store.snapshot("agent", "agent_1"), {})

    def test_search_crosses_scopes(self):
        self.store.remember("workspace", "branch", "main", scope="repo-a")
        self.store.remember("workspace", "test_command", "pytest -q", scope="repo-b")
        rows = self.store.search("pytest")
        self.assertTrue(any(r["key"] == "test_command" for r in rows))

    def test_file_is_valid_json(self):
        self.store.remember("task", "k", "v", scope="s")
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertIn("task", data)
        self.assertIn("s", data["task"])


class TestWorkspaceScope(unittest.TestCase):
    def test_same_repo_shares_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "myrepo"
            (root / ".git").mkdir(parents=True)
            nested = root / "src" / "package"
            nested.mkdir(parents=True)
            self.assertEqual(workspace_scope(root), workspace_scope(nested))

    def test_different_checkouts_do_not_share_scope(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ra = Path(a) / "app"
            rb = Path(b) / "app"
            (ra / ".git").mkdir(parents=True)
            (rb / ".git").mkdir(parents=True)
            self.assertNotEqual(workspace_scope(ra), workspace_scope(rb))

    def test_scope_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(workspace_scope(tmp), workspace_scope(tmp))


if __name__ == "__main__":
    unittest.main()
