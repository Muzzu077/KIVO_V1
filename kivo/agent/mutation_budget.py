"""Atomic, bounded per-turn mutation-attempt budget.

A conversation turn may make at most one filesystem mutation attempt.  The
budget is claimed *before* mutation preflight so that rejected, expired,
cancelled, failed, and invalid attempts all consume the same allowance.  A new
turn starts with a fresh allowance, and read-only tools are never affected.

The implementation uses a thread lock rather than an asyncio lock because
approval resolution and execution can occur on different threads.
"""
from __future__ import annotations

import collections
import threading

DEFAULT_MAX_TRACKED_TURNS = 2_048


class MutationBudget:
    """Thread-safe single-attempt budget keyed by conversation turn."""

    def __init__(self, max_tracked_turns: int = DEFAULT_MAX_TRACKED_TURNS):
        self._lock = threading.Lock()
        self._claimed: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._max_tracked_turns = max(1, int(max_tracked_turns))

    @staticmethod
    def _clean_turn_id(turn_id: object) -> str:
        if not isinstance(turn_id, str):
            return ""
        return turn_id.strip()

    def claim(self, turn_id: object) -> bool:
        """Atomically claim the one mutation attempt for ``turn_id``.

        Returns ``False`` if the turn already used its budget, the turn
        identifier is missing, or another thread won the race.
        """
        clean = self._clean_turn_id(turn_id)
        if not clean:
            return False
        with self._lock:
            if clean in self._claimed:
                return False
            self._claimed[clean] = None
            while len(self._claimed) > self._max_tracked_turns:
                self._claimed.popitem(last=False)
            return True

    def is_claimed(self, turn_id: object) -> bool:
        clean = self._clean_turn_id(turn_id)
        if not clean:
            return False
        with self._lock:
            return clean in self._claimed

    def try_claim(self, turn_id: object) -> bool:
        """Alias for ``claim`` used by callers that prefer explicit intent."""
        return self.claim(turn_id)

    def remaining(self, turn_id: object) -> int:
        """Return the number of mutation attempts left for a turn (0 or 1)."""
        return 0 if self.is_claimed(turn_id) else 1

    def reset(self, turn_id: object = None) -> None:
        """Test/administrative helper; normal turns never release a budget."""
        with self._lock:
            if turn_id is None:
                self._claimed.clear()
                return
            clean = self._clean_turn_id(turn_id)
            if clean:
                self._claimed.pop(clean, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._claimed)


__all__ = ["DEFAULT_MAX_TRACKED_TURNS", "MutationBudget"]
