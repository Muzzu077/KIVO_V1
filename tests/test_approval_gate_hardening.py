"""Adversarial tests for approval expiration, cancellation, races, and limits."""
from __future__ import annotations

import asyncio
import threading
import unittest

from kivo.agent.approval_gate import ApprovalGate


class ApprovalGateHardeningTest(unittest.IsolatedAsyncioTestCase):
    async def _start_request(
        self,
        gate: ApprovalGate,
        *,
        turn_id: str = "turn-1",
        tool_call_id: str = "call-1",
        expected_pending: int = 1,
    ):
        task = asyncio.create_task(
            gate.request_approval(
                tool_call_id=tool_call_id,
                tool_name="test_tool",
                command="test_action",
                arguments=[],
                working_directory="",
                reason="testing",
                turn_id=turn_id,
            )
        )
        for _ in range(100):
            pending = gate.get_pending_requests()
            # Wait until this request is actually registered. Checking only
            # "any pending" would race with an earlier still-open request.
            if len(pending) >= expected_pending:
                return task, pending[-1]
            await asyncio.sleep(0.01)
        self.fail("approval request never became pending")

    async def test_resolution_from_non_event_loop_thread(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task, pending = await self._start_request(gate)

        thread = threading.Thread(
            target=lambda: gate.resolve(
                pending["token"], approved=True, turn_id=pending["turn_id"]
            ),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())

        approved, status = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(approved)
        self.assertEqual(status, "approved")

    async def test_double_resolution_is_rejected(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task, pending = await self._start_request(gate)

        first, _ = gate.resolve(
            pending["token"], approved=True, turn_id=pending["turn_id"]
        )
        second, message = gate.resolve(
            pending["token"], approved=True, turn_id=pending["turn_id"]
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIn("reused", message.lower())

        approved, _ = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(approved)

    async def test_expired_request_cannot_be_resolved(self):
        gate = ApprovalGate(timeout_seconds=0.05)
        task, pending = await self._start_request(gate)

        approved, status = await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(approved)
        self.assertEqual(status, "expired")

        resolved, _ = gate.resolve(
            pending["token"], approved=True, turn_id=pending["turn_id"]
        )
        self.assertFalse(resolved)
        self.assertEqual(gate.get_pending_requests(), [])

    async def test_cancel_all_cancels_waiter_and_cleans_pending(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task, _ = await self._start_request(gate)

        gate.cancel_all()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(gate.get_pending_requests(), [])

    async def test_pending_requests_are_bounded(self):
        gate = ApprovalGate(timeout_seconds=2.0, max_pending=2)
        tasks = []
        for index in range(2):
            task, _ = await self._start_request(
                gate,
                turn_id=f"turn-{index}",
                tool_call_id=f"call-{index}",
                expected_pending=index + 1,
            )
            tasks.append(task)

        approved, status = await gate.request_approval(
            tool_call_id="call-overflow",
            tool_name="test_tool",
            command="test_action",
            arguments=[],
            working_directory="",
            reason="overflow",
            turn_id="turn-overflow",
        )
        self.assertFalse(approved)
        self.assertEqual(status, "too_many_pending")

        gate.cancel_all()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_consumed_token_storage_is_bounded(self):
        gate = ApprovalGate(
            timeout_seconds=2.0,
            max_consumed_tokens=3,
        )
        for index in range(8):
            task, pending = await self._start_request(
                gate, turn_id=f"turn-{index}", tool_call_id=f"call-{index}"
            )
            gate.resolve(
                pending["token"], approved=True, turn_id=pending["turn_id"]
            )
            accepted, _ = await asyncio.wait_for(task, timeout=2.0)
            self.assertTrue(accepted)

        self.assertLessEqual(len(gate._consumed_tokens), 3)

    async def test_resolution_is_bound_to_turn_and_tool_call(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task, pending = await self._start_request(
            gate, turn_id="turn-a", tool_call_id="call-a"
        )

        wrong_turn, _ = gate.resolve(
            pending["token"], approved=True, turn_id="turn-b"
        )
        self.assertFalse(wrong_turn)
        self.assertEqual(len(gate.get_pending_requests()), 1)

        wrong_call, _ = gate.resolve(
            pending["token"], approved=True, tool_call_id="call-b"
        )
        self.assertFalse(wrong_call)
        self.assertEqual(len(gate.get_pending_requests()), 1)

        correct, _ = gate.resolve(
            pending["token"],
            approved=True,
            turn_id="turn-a",
            tool_call_id="call-a",
        )
        self.assertTrue(correct)
        approved, _ = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(approved)

    async def test_cancel_all_can_filter_by_turn(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task_a, pending_a = await self._start_request(
            gate, turn_id="turn-a", tool_call_id="call-a"
        )
        task_b, _ = await self._start_request(
            gate, turn_id="turn-b", tool_call_id="call-b"
        )

        gate.cancel_all(turn_id="turn-a")
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task_a, timeout=2.0)

        self.assertEqual(len(gate.get_pending_requests()), 1)
        self.assertTrue(
            gate.resolve(pending_a["token"], approved=True, turn_id="turn-a")[0] is False
        )

        pending_b = gate.get_pending_requests()[0]
        gate.resolve(pending_b["token"], approved=True, turn_id="turn-b")
        approved_b, _ = await asyncio.wait_for(task_b, timeout=2.0)
        self.assertTrue(approved_b)


if __name__ == "__main__":
    unittest.main()
