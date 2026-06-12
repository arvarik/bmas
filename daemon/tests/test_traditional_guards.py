# /opt/bmas/daemon/tests/test_traditional_guards.py
"""Unit tests for TraditionalVariant deterministic guards (doc 05 §5).

No LLM, no Redis — pure logic tests against in-memory board state.
"""

import time
from datetime import UTC, datetime

import pytest

from core.entry import BoardEntry
from core.variants.traditional import TraditionalVariant, _entries_hash

# ── Helpers ──────────────────────────────────────────────────────────

def _make_entry(
    eid: str, etype: str, author: str, body: str,
    status: str = "open", refs: list[str] | None = None,
    round_no: int = 0, confidence: float = 0.8,
    salience: float = 0.5,
) -> BoardEntry:
    """Create a BoardEntry for testing."""
    return BoardEntry(
        id=eid,
        task_id="test-task",
        type=etype,
        author=author,
        body=body,
        title=body[:80],
        refs=refs or [],
        confidence=confidence,
        status=status,
        salience=salience,
        round=round_no,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


def _make_variant(**overrides) -> TraditionalVariant:
    """Create a variant with no external deps (all set to None/mock)."""
    config = {
        "max_rounds": overrides.pop("max_rounds", 4),
        "max_duration_s": overrides.pop("max_duration_s", 1800),
        "budget_ceiling_usd": overrides.pop("budget_ceiling_usd", 0.50),
        "max_concurrent_activations": overrides.pop("max_concurrent_activations", 3),
        "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 3},
        "cleaner_entry_threshold": overrides.pop("cleaner_entry_threshold", 12),
        "stall_rounds": overrides.pop("stall_rounds", 2),
        "cu_mode": "llm",
        "sole_similarity": "auto",
    }
    return TraditionalVariant(
        gateway=None,
        board_store=None,
        event_emitter=None,
        triage=None,
        config=config,
        litellm_url="",
        litellm_key="",
        node_endpoints=[],
        role_registry={},
        model_routing={},
    )


# ── accepted_solution() ─────────────────────────────────────────────

class TestAcceptedSolution:

    def test_empty_board_returns_none(self):
        v = _make_variant()
        assert v._accepted_solution({}) is None

    def test_solution_without_critique_is_accepted(self):
        v = _make_variant()
        sol = _make_entry("e-1", "solution", "decider", "The answer is 42", round_no=2)
        snapshot = {"e-1": sol}
        result = v._accepted_solution(snapshot)
        assert result is not None
        assert result.id == "e-1"

    def test_solution_with_critique_is_contested(self):
        v = _make_variant()
        sol = _make_entry("e-1", "solution", "decider", "Answer", round_no=2)
        crit = _make_entry("e-2", "critique", "critic", "Wrong!", refs=["e-1"], round_no=2)
        snapshot = {"e-1": sol, "e-2": crit}
        assert v._accepted_solution(snapshot, current_round=2) is None

    def test_solution_with_removed_critique_is_accepted(self):
        v = _make_variant()
        sol = _make_entry("e-1", "solution", "decider", "Answer", round_no=2)
        crit = _make_entry("e-2", "critique", "critic", "Wrong!",
                          refs=["e-1"], round_no=2, status="removed")
        snapshot = {"e-1": sol, "e-2": crit}
        result = v._accepted_solution(snapshot, current_round=3)
        assert result is not None

    def test_removed_solution_ignored(self):
        v = _make_variant()
        sol = _make_entry("e-1", "solution", "decider", "Answer",
                         status="removed", round_no=2)
        snapshot = {"e-1": sol}
        assert v._accepted_solution(snapshot) is None

    def test_multiple_solutions_latest_uncontested_wins(self):
        v = _make_variant()
        sol1 = _make_entry("e-1", "solution", "decider", "Old answer", round_no=2)
        crit = _make_entry("e-2", "critique", "critic", "Wrong!", refs=["e-1"], round_no=2)
        sol2 = _make_entry("e-3", "solution", "decider", "Better answer", round_no=3)
        snapshot = {"e-1": sol1, "e-2": crit, "e-3": sol2}
        result = v._accepted_solution(snapshot, current_round=3)
        assert result is not None
        assert result.id == "e-3"


# ── _is_stalled() ───────────────────────────────────────────────────

class TestStallDetection:

    def test_no_entries_last_round_increments_counter(self):
        v = _make_variant(stall_rounds=2)
        snapshot = {}  # empty board
        assert v._is_stalled(snapshot, current_round=2) is False
        assert v._stall_counter == 1

    def test_stall_threshold_reached(self):
        v = _make_variant(stall_rounds=2)
        snapshot = {}
        v._is_stalled(snapshot, current_round=2)  # counter=1
        assert v._is_stalled(snapshot, current_round=3) is True  # counter=2
        assert v._stall_counter == 2

    def test_new_entries_reset_counter(self):
        v = _make_variant(stall_rounds=2)
        snapshot = {}
        v._is_stalled(snapshot, current_round=2)  # counter=1

        # Now add an entry in the current round
        entry = _make_entry("e-1", "finding", "expert.x", "New content", round_no=2)
        snapshot_with = {"e-1": entry}
        result = v._is_stalled(snapshot_with, current_round=3)
        assert result is False
        assert v._stall_counter == 0

    def test_near_duplicate_bodies_count_as_stall(self):
        v = _make_variant(stall_rounds=2)
        e1 = _make_entry("e-1", "finding", "expert.a", "Same content", round_no=1)
        snapshot1 = {"e-1": e1}
        v._is_stalled(snapshot1, current_round=2)
        # First call: hash stored, counter=0 (first time seeing this hash)

        # Same body hash in the next round → counter increments to 1
        e2 = _make_entry("e-2", "finding", "expert.a", "Same content", round_no=2)
        snapshot2 = {"e-1": e1, "e-2": e2}
        assert v._is_stalled(snapshot2, current_round=3) is False
        assert v._stall_counter == 1

        # Third round with same hash → counter=2, meets threshold
        e3 = _make_entry("e-3", "finding", "expert.a", "Same content", round_no=3)
        snapshot3 = {"e-1": e1, "e-2": e2, "e-3": e3}
        assert v._is_stalled(snapshot3, current_round=4) is True
        assert v._stall_counter == 2


# ── is_terminal() ────────────────────────────────────────────────────

class TestIsTerminal:

    def test_empty_board_not_terminal(self):
        v = _make_variant()
        terminal, reason = v.is_terminal({})
        assert terminal is False
        assert reason is None

    def test_accepted_solution_is_terminal(self):
        v = _make_variant()
        sol = _make_entry("e-1", "solution", "decider", "Answer", round_no=2)
        terminal, reason = v.is_terminal({"e-1": sol})
        assert terminal is True
        assert reason == "solution"


# ── Guard ordering (solution checked before rounds) ──────────────────

class TestGuardOrdering:

    @pytest.mark.asyncio
    async def test_solution_found_before_max_rounds(self):
        """If a solution exists, step() returns terminal even at round 1."""
        from core.board_store import InMemoryBoardStore
        v = _make_variant(max_rounds=4)
        v.store = InMemoryBoardStore()

        task_id = "test-task"
        # Write a solution via the store
        sol = _make_entry("e-1", "solution", "decider", "42", round_no=1)
        await v.store.upsert_entry(task_id, sol)
        await v.store.set_meta(task_id, round=0, budget_spent=0.0)

        # genesis_time must be set
        v.genesis_time = time.monotonic()

        task = {"task_id": task_id, "query": "What is the answer?"}
        result = await v.step(task, None)
        assert result.terminal is True
        assert result.reason == "solution"

    @pytest.mark.asyncio
    async def test_budget_guard(self):
        """step() returns terminal when budget is exceeded."""
        from core.board_store import InMemoryBoardStore
        v = _make_variant(budget_ceiling_usd=0.10)
        v.store = InMemoryBoardStore()

        task_id = "test-task"
        await v.store.set_meta(task_id, round=0, budget_spent=0.15)
        v.genesis_time = time.monotonic()

        task = {"task_id": task_id, "query": "Test"}
        result = await v.step(task, None)
        assert result.terminal is True
        assert result.reason == "budget"

    @pytest.mark.asyncio
    async def test_max_rounds_guard(self):
        """step() returns terminal when max_rounds is exceeded."""
        from core.board_store import InMemoryBoardStore
        v = _make_variant(max_rounds=2)
        v.store = InMemoryBoardStore()

        task_id = "test-task"
        await v.store.set_meta(task_id, round=2, budget_spent=0.0)
        v.genesis_time = time.monotonic()

        task = {"task_id": task_id, "query": "Test"}
        result = await v.step(task, None)
        assert result.terminal is True
        assert result.reason == "max_rounds"

    @pytest.mark.asyncio
    async def test_duration_guard(self):
        """step() returns terminal when max_duration_s is exceeded."""
        from core.board_store import InMemoryBoardStore
        v = _make_variant(max_duration_s=1)
        v.store = InMemoryBoardStore()

        task_id = "test-task"
        await v.store.set_meta(task_id, round=0, budget_spent=0.0)
        v.genesis_time = time.monotonic() - 100  # 100s ago

        task = {"task_id": task_id, "query": "Test"}
        result = await v.step(task, None)
        assert result.terminal is True
        assert result.reason == "duration"


# ── _entries_hash() ──────────────────────────────────────────────────

class TestEntriesHash:

    def test_same_bodies_same_hash(self):
        e1 = _make_entry("e-1", "finding", "a", "hello world")
        e2 = _make_entry("e-2", "finding", "b", "hello world")
        h1 = _entries_hash([e1])
        h2 = _entries_hash([e2])
        assert h1 == h2

    def test_different_bodies_different_hash(self):
        e1 = _make_entry("e-1", "finding", "a", "hello")
        e2 = _make_entry("e-2", "finding", "b", "world")
        h1 = _entries_hash([e1])
        h2 = _entries_hash([e2])
        assert h1 != h2
