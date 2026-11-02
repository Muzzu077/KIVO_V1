"""Drive session lifecycle and cancellation."""
import threading
import time
import unittest

from core.drive import DriveSession


class TestDriveSession(unittest.TestCase):
    def setUp(self):
        self.drive = DriveSession()
        self.events: list[dict] = []
        self.drive.add_listener(lambda s: self.events.append(s))

    def test_start_update_stop(self):
        s = self.drive.start("Build the thing")
        self.assertTrue(s["active"])
        self.assertEqual(s["goal"], "Build the thing")
        self.assertEqual(len(self.events), 1)

        s = self.drive.update("Step 1: compile")
        self.assertTrue(s["active"])
        self.assertEqual(s["action"], "Step 1: compile")
        self.assertEqual(len(self.events), 2)

        s = self.drive.stop("done")
        self.assertFalse(s["active"])
        self.assertEqual(len(self.events), 3)

    def test_stop_when_not_active_is_silent(self):
        self.drive.stop()
        self.assertEqual(len(self.events), 0)

    def test_start_is_idempotent(self):
        self.drive.start("A")
        s = self.drive.start("B")
        self.assertEqual(s["goal"], "A")

    def test_update_without_start_is_ignored(self):
        s = self.drive.update("invisible")
        self.assertFalse(s["active"])


class TestOrchestratorCancellation(unittest.TestCase):
    def test_cancel_stops_running_plan(self):
        from core.task_orchestrator import TaskOrchestrator, TaskPlan, TaskStep, StepStatus

        class SlowRegistry:
            def run(self, name, params, ctx):
                time.sleep(0.5)
                return "ok"

        orch = TaskOrchestrator()
        orch.bind_registry(SlowRegistry())

        plan = orch.create_plan("Slow job", [
            {"tool": "a", "description": "step 1"},
            {"tool": "b", "description": "step 2"},
            {"tool": "c", "description": "step 3"},
        ])

        result = [None]

        def run():
            result[0] = orch.execute_plan(plan)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.2)
        plan.cancel_event.set()
        t.join(timeout=5)

        self.assertTrue(result[0].is_finished)
        self.assertFalse(result[0].success)
        self.assertIn("cancelled", result[0].summary.lower())
        skipped = sum(1 for s in result[0].steps if s.status == StepStatus.SKIPPED)
        self.assertGreater(skipped, 0)


if __name__ == "__main__":
    unittest.main()
