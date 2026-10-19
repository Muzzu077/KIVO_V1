import unittest
from core.task_orchestrator import TaskOrchestrator, TaskStep, TaskPlan, StepStatus

class MockRegistry:
    def __init__(self):
        self.calls = []

    def run(self, tool_name: str, params: dict, *args, **kwargs):
        self.calls.append((tool_name, params))
        if tool_name == "fail_tool":
            raise RuntimeError("Tool simulated failure")
        return f"OK: {tool_name}"

class TestTaskOrchestrator(unittest.TestCase):
    def setUp(self):
        self.orchestrator = TaskOrchestrator()
        self.mock_registry = MockRegistry()
        self.orchestrator.bind_registry(self.mock_registry)

    def test_successful_plan_execution(self):
        plan = TaskPlan(
            task_id="test_plan_1",
            goal="Test successful multi-step execution",
            steps=[
                TaskStep(
                    id=1,
                    description="Step 1",
                    tool="step_one_tool",
                    action="*",
                    parameters={"val": 1}
                ),
                TaskStep(
                    id=2,
                    description="Step 2",
                    tool="step_two_tool",
                    action="*",
                    parameters={"val": 2}
                )
            ]
        )
        report = self.orchestrator.execute_plan(plan)
        self.assertEqual(len(report.steps), 2)
        self.assertEqual(report.steps[0].status, StepStatus.COMPLETED)
        self.assertEqual(report.steps[1].status, StepStatus.COMPLETED)
        self.assertEqual(len(self.mock_registry.calls), 2)

    def test_failed_step_abort(self):
        plan = TaskPlan(
            task_id="test_plan_2",
            goal="Test failure abort",
            steps=[
                TaskStep(
                    id=1,
                    description="Step 1",
                    tool="fail_tool",
                    action="*",
                    parameters={}
                ),
                TaskStep(
                    id=2,
                    description="Step 2",
                    tool="step_two_tool",
                    action="*",
                    parameters={}
                )
            ]
        )
        report = self.orchestrator.execute_plan(plan)
        self.assertEqual(report.steps[0].status, StepStatus.FAILED)
        self.assertEqual(report.steps[1].status, StepStatus.SKIPPED)

if __name__ == "__main__":
    unittest.main()
