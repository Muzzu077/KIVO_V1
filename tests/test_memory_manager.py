import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.memory_manager import load_memory, save_memory, remember, forget


class TestMemoryManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.test_mem_file = self.temp_path / "long_term.json"
        self.test_bak_file = self.temp_path / "long_term.json.bak"

        # Invalidate in-memory cache before tests
        import memory.memory_manager as mm
        mm._cached_memory = None
        mm._cached_mtime = 0.0

    def tearDown(self):
        import memory.memory_manager as mm
        mm._cached_memory = None
        mm._cached_mtime = 0.0
        self.temp_dir.cleanup()

    def test_save_and_load_memory(self):
        with patch("memory.memory_manager.MEMORY_PATH", self.test_mem_file), \
             patch("memory.memory_manager.MEMORY_BACKUP_PATH", self.test_bak_file):
            remember("favorite_editor", "neovim", category="preferences")
            mem = load_memory()
            entry = mem.get("preferences", {}).get("favorite_editor")
            val = entry.get("value") if isinstance(entry, dict) else entry
            self.assertEqual(val, "neovim")
            self.assertTrue(self.test_mem_file.exists())

    def test_atomic_write_and_backup(self):
        with patch("memory.memory_manager.MEMORY_PATH", self.test_mem_file), \
             patch("memory.memory_manager.MEMORY_BACKUP_PATH", self.test_bak_file):
            remember("key1", "val1", category="preferences")
            self.assertTrue(self.test_mem_file.exists())

            # Second save should create backup of previous state
            remember("key2", "val2", category="preferences")
            self.assertTrue(self.test_bak_file.exists())
            bak_data = json.loads(self.test_bak_file.read_text())
            self.assertIn("key1", bak_data.get("preferences", {}))

    def test_corrupted_file_recovery_from_backup(self):
        import memory.memory_manager as mm
        with patch("memory.memory_manager.MEMORY_PATH", self.test_mem_file), \
             patch("memory.memory_manager.MEMORY_BACKUP_PATH", self.test_bak_file):
            # Create a valid backup
            valid_data = mm._empty_memory()
            valid_data["preferences"]["theme"] = "gruvbox"
            self.test_bak_file.write_text(json.dumps(valid_data))

            # Corrupt the primary file with broken JSON
            self.test_mem_file.write_text("{bad json syntax...")

            # Ensure cache is invalidated
            mm._cached_memory = None
            mm._cached_mtime = 0.0

            mem = load_memory()
            self.assertEqual(mem.get("preferences", {}).get("theme"), "gruvbox")


if __name__ == "__main__":
    unittest.main()
