# /opt/bmas/daemon/tests/test_gateway.py
"""Board Gateway tests — the heart of Phase 2 (doc 04 §4).

Heavy unit tests with in-memory fakes: no LLM, no Redis, no network.
Tests cover the full normalize → validate → authorize → commit → emit
pipeline plus remove, set_status, set_meta, envelope fallback, and
salience recompute.
"""
from __future__ import annotations

import asyncio

import pytest
from test_helpers import make_critique_entry, make_proposed_entry, make_solution_entry

from core.entry import envelope_fallback
from core.gateway import BoardGateway, salience_recompute_hook
from core.protocol import (
    EVENT_BOARD_ENTRY,
    EVENT_ENTRY_REJECTED,
    EVENT_ENTRY_REMOVED,
    EVENT_ENTRY_STATUS_CHANGED,
)

# ── Envelope Validation Tests ────────────────────────────────────────


class TestEnvelopeValidation:
    """Test entry normalization and validation."""

    @pytest.mark.asyncio
    async def test_valid_entry_committed(self, gateway, event_emitter, board_store):
        """Valid entry → committed, returns BoardEntry with gateway-assigned fields."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.valuation",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
            round_no=1,
        )
        assert len(entries) == 1
        entry = entries[0]
        assert entry.id.startswith("e-")
        assert entry.author == "expert.valuation"
        assert entry.type == "finding"
        assert entry.status == "open"
        assert entry.round == 1
        assert entry.task_id == "task-1"

    @pytest.mark.asyncio
    async def test_empty_body_rejected(self, gateway, event_emitter):
        """Empty body → rejected, entry_rejected event emitted."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(body="")],
            turn_id="turn-1",
        )
        assert len(entries) == 0
        assert event_emitter.has_event(EVENT_ENTRY_REJECTED)

    @pytest.mark.asyncio
    async def test_whitespace_body_rejected(self, gateway, event_emitter):
        """Whitespace-only body → rejected."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(body="   \n  \t  ")],
            turn_id="turn-1",
        )
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_title_truncated_not_rejected(self, gateway):
        """Title > 200 chars → truncated, not rejected."""
        long_title = "A" * 300
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(title=long_title)],
            turn_id="turn-1",
        )
        assert len(entries) == 1
        assert len(entries[0].title) == 200

    @pytest.mark.asyncio
    async def test_body_exceeds_max_rejected(self, board_store, event_emitter):
        """Body > max_entry_chars → rejected."""
        gw = BoardGateway(board_store, event_emitter, max_body_len=100)
        entries = await gw.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(body="X" * 101)],
            turn_id="turn-1",
        )
        assert len(entries) == 0
        assert event_emitter.has_event(EVENT_ENTRY_REJECTED)

    @pytest.mark.asyncio
    async def test_unknown_type_rejected(self, gateway, event_emitter):
        """Unknown entry type → rejected."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(type="nonexistent_type")],
            turn_id="turn-1",
        )
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_unknown_refs_dropped(self, gateway, event_emitter):
        """Unknown refs → dropped with warning, entry still committed."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(refs=["nonexistent-id"])],
            turn_id="turn-1",
        )
        assert len(entries) == 1
        assert "nonexistent-id" not in entries[0].refs

    @pytest.mark.asyncio
    async def test_valid_refs_preserved(self, gateway, board_store):
        """Valid refs are preserved."""
        # First, create an entry to reference
        entries = await gateway.append(
            task_id="task-1",
            actor="planner",
            capabilities=["plan_writer"],
            proposed=[make_proposed_entry(type="plan")],
            turn_id="turn-1",
        )
        ref_id = entries[0].id

        # Now create an entry that references it
        entries2 = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(refs=[ref_id])],
            turn_id="turn-2",
        )
        assert ref_id in entries2[0].refs

    @pytest.mark.asyncio
    async def test_confidence_clamped(self, gateway):
        """Confidence out of range → clamped to [0, 1]."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[
                make_proposed_entry(confidence=1.5),
                make_proposed_entry(confidence=-0.3),
            ],
            turn_id="turn-1",
        )
        assert entries[0].confidence == 1.0
        assert entries[1].confidence == 0.0

    @pytest.mark.asyncio
    async def test_reserved_fields_stripped(self, gateway):
        """Reserved fields in proposed entry → stripped in normalize."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(
                id="hacked-id",
                status="superseded",
                salience=999.0,
                author="evil-actor",
            )],
            turn_id="turn-1",
        )
        assert len(entries) == 1
        assert entries[0].id != "hacked-id"  # Gateway assigns ID
        assert entries[0].status == "open"    # Gateway forces open
        assert entries[0].author == "expert.x"  # Gateway stamps actor

    @pytest.mark.asyncio
    async def test_type_inferred_from_actor(self, gateway):
        """Missing type → inferred from actor role."""
        entries = await gateway.append(
            task_id="task-1",
            actor="planner",
            capabilities=["plan_writer"],
            proposed=[{"body": "Plan: decompose into 3 subtasks", "title": "Plan"}],
            turn_id="turn-1",
        )
        assert len(entries) == 1
        assert entries[0].type == "plan"


# ── Capability Rejection Tests ───────────────────────────────────────


class TestCapabilityRejection:
    """Test capability-based authorization."""

    @pytest.mark.asyncio
    async def test_critic_cannot_post_solution(self, gateway, event_emitter):
        """Critic posting solution → rejected, entry_rejected emitted."""
        entries = await gateway.append(
            task_id="task-1",
            actor="critic",
            capabilities=["critique_writer"],
            proposed=[make_solution_entry()],
            turn_id="turn-1",
        )
        assert len(entries) == 0
        assert event_emitter.has_event(EVENT_ENTRY_REJECTED)

    @pytest.mark.asyncio
    async def test_finding_writer_cannot_post_plan(self, gateway, event_emitter):
        """Finding writer posting plan → rejected."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(type="plan")],
            turn_id="turn-1",
        )
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_maintenance_can_post_finding(self, gateway, event_emitter):
        """Board maintenance posting finding → committed."""
        entries = await gateway.append(
            task_id="task-1",
            actor="cleaner",
            capabilities=["board_maintenance"],
            proposed=[make_proposed_entry(type="finding")],
            turn_id="turn-1",
        )
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_maintenance_cannot_post_solution(self, gateway, event_emitter):
        """Board maintenance posting solution → rejected."""
        entries = await gateway.append(
            task_id="task-1",
            actor="cleaner",
            capabilities=["board_maintenance"],
            proposed=[make_solution_entry()],
            turn_id="turn-1",
        )
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_decision_writer_can_post_solution(self, gateway):
        """Decision writer posting solution → committed."""
        entries = await gateway.append(
            task_id="task-1",
            actor="decider",
            capabilities=["decision_writer"],
            proposed=[make_solution_entry()],
            turn_id="turn-1",
        )
        assert len(entries) == 1
        assert entries[0].type == "solution"

    @pytest.mark.asyncio
    async def test_empty_capabilities_rejected(self, gateway, event_emitter):
        """No capabilities → rejected."""
        entries = await gateway.append(
            task_id="task-1",
            actor="unknown",
            capabilities=[],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
        )
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_mixed_valid_invalid_entries(self, gateway, event_emitter):
        """Batch with valid + invalid → valid committed, invalid rejected."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[
                make_proposed_entry(body="Valid finding"),
                make_proposed_entry(body=""),  # Invalid: empty body
                make_proposed_entry(type="solution"),  # Invalid: no capability
            ],
            turn_id="turn-1",
        )
        assert len(entries) == 1  # Only the valid one
        assert entries[0].body == "Valid finding"


# ── Envelope Fallback Tests ──────────────────────────────────────────


class TestEnvelopeFallback:
    """Test the free-text envelope wrapping (doc 04 §3)."""

    def test_fallback_for_critic(self):
        """Critic's plain text → type=critique."""
        proposed = envelope_fallback("This finding has issues because X", "critic")
        assert proposed.type == "critique"
        assert proposed.body == "This finding has issues because X"
        assert proposed.title == "This finding has issues because X"

    def test_fallback_for_expert(self):
        """expert.valuation → type=finding."""
        proposed = envelope_fallback("The market analysis shows...", "expert.valuation")
        assert proposed.type == "finding"

    def test_fallback_for_unknown_role(self):
        """Unknown role → type=finding (safe default)."""
        proposed = envelope_fallback("Some output", "universal-3")
        assert proposed.type == "finding"

    def test_fallback_title_truncated(self):
        """Title from first line, truncated to 80 chars."""
        long_line = "A" * 200 + "\nSecond line"
        proposed = envelope_fallback(long_line, "critic")
        assert len(proposed.title) == 80

    def test_fallback_multiline(self):
        """Title is first line only."""
        proposed = envelope_fallback("First line\nSecond line\nThird line", "planner")
        assert proposed.title == "First line"

    def test_fallback_empty_text(self):
        """Empty text → default title."""
        proposed = envelope_fallback("", "planner")
        assert proposed.title == "Untitled response"

    def test_fallback_confidence_none(self):
        """Fallback entries have None confidence (→ default 0.5)."""
        proposed = envelope_fallback("test", "planner")
        assert proposed.confidence is None

    def test_fallback_planner(self):
        """Planner → type=plan."""
        proposed = envelope_fallback("Decompose into subtasks", "planner")
        assert proposed.type == "plan"


# ── Event Emission Tests ─────────────────────────────────────────────


class TestEventEmission:
    """Test SSE event emission."""

    @pytest.mark.asyncio
    async def test_append_emits_board_entry(self, gateway, event_emitter):
        """append → emits board_entry for each committed entry."""
        await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(), make_proposed_entry(body="Second finding")],
            turn_id="turn-1",
        )
        board_events = event_emitter.events_of_type(EVENT_BOARD_ENTRY)
        assert len(board_events) == 2

    @pytest.mark.asyncio
    async def test_append_rejection_emits_entry_rejected(self, gateway, event_emitter):
        """Rejected entry → emits entry_rejected."""
        await gateway.append(
            task_id="task-1",
            actor="critic",
            capabilities=["critique_writer"],
            proposed=[make_solution_entry()],  # Critic can't post solution
            turn_id="turn-1",
        )
        rejected_events = event_emitter.events_of_type(EVENT_ENTRY_REJECTED)
        assert len(rejected_events) >= 1
        assert "reason" in rejected_events[0]

    @pytest.mark.asyncio
    async def test_remove_emits_entry_removed(self, gateway, event_emitter, board_store):
        """remove → emits entry_removed for each removed entry."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
        )
        entry_id = entries[0].id
        event_emitter.clear()

        removed = await gateway.remove(
            task_id="task-1",
            actor="cleaner",
            capabilities=["board_maintenance"],
            entry_ids=[entry_id],
            reason="cleanup after round 2",
        )
        assert removed == [entry_id]
        assert event_emitter.has_event(EVENT_ENTRY_REMOVED)

    @pytest.mark.asyncio
    async def test_set_status_emits_status_changed(self, gateway, event_emitter):
        """set_status → emits entry_status_changed."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
        )
        event_emitter.clear()

        await gateway.set_status(
            task_id="task-1",
            entry_id=entries[0].id,
            status="superseded",
            actor="decider",
        )
        assert event_emitter.has_event(EVENT_ENTRY_STATUS_CHANGED)
        changed = event_emitter.events_of_type(EVENT_ENTRY_STATUS_CHANGED)
        assert changed[0]["old_status"] == "open"
        assert changed[0]["status"] == "superseded"

    @pytest.mark.asyncio
    async def test_board_entry_payload_shape(self, gateway, event_emitter):
        """board_entry event carries the full entry dict."""
        _entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(title="My Finding")],
            turn_id="turn-1",
        )
        board_events = event_emitter.events_of_type(EVENT_BOARD_ENTRY)
        assert "id" in board_events[0]
        assert "type" in board_events[0]
        assert "author" in board_events[0]
        assert "body" in board_events[0]
        assert board_events[0]["title"] == "My Finding"


# ── Salience Recompute Tests ─────────────────────────────────────────


class TestSalienceRecompute:
    """Test the salience recompute hook integration."""

    @pytest.mark.asyncio
    async def test_salience_computed_after_append(self, board_store, event_emitter):
        """After append, salience is recomputed for all entries."""
        gw = BoardGateway(
            board_store, event_emitter,
            recompute_hooks=[salience_recompute_hook],
        )
        await board_store.set_meta("task-1", round=1)

        entries = await gw.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(confidence=0.8)],
            turn_id="turn-1",
            round_no=1,
        )
        entry = await board_store.get_entry("task-1", entries[0].id)
        assert entry.salience > 0  # Should have been recomputed

    @pytest.mark.asyncio
    async def test_refs_in_boost_salience(self, board_store, event_emitter):
        """Entry cited by others has higher salience."""
        gw = BoardGateway(
            board_store, event_emitter,
            recompute_hooks=[salience_recompute_hook],
        )
        await board_store.set_meta("task-1", round=1)

        # Create first entry
        entries1 = await gw.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry(confidence=0.5)],
            turn_id="turn-1",
            round_no=1,
        )
        ref_id = entries1[0].id
        salience_before = entries1[0].salience

        # Create entries that reference the first
        await gw.append(
            task_id="task-1",
            actor="expert.y",
            capabilities=["finding_writer"],
            proposed=[
                make_proposed_entry(refs=[ref_id], body="Response 1"),
                make_proposed_entry(refs=[ref_id], body="Response 2"),
            ],
            turn_id="turn-2",
            round_no=1,
        )

        # Salience should have increased
        entry = await board_store.get_entry("task-1", ref_id)
        assert entry.salience > salience_before


# ── Remove Tests ─────────────────────────────────────────────────────


class TestRemove:
    """Test the remove path (Cleaner)."""

    @pytest.mark.asyncio
    async def test_remove_marks_as_removed(self, gateway, board_store):
        """Remove flips status to 'removed'."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
        )
        entry_id = entries[0].id

        await gateway.remove(
            task_id="task-1",
            actor="cleaner",
            capabilities=["board_maintenance"],
            entry_ids=[entry_id],
            reason="cleanup",
        )

        entry = await board_store.get_entry("task-1", entry_id)
        assert entry.status == "removed"

    @pytest.mark.asyncio
    async def test_remove_unauthorized(self, gateway, board_store, event_emitter):
        """Finding writer cannot remove entries."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
        )
        event_emitter.clear()

        removed = await gateway.remove(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            entry_ids=[entries[0].id],
            reason="unauthorized attempt",
        )
        assert removed == []
        entry = await board_store.get_entry("task-1", entries[0].id)
        assert entry.status == "open"

    @pytest.mark.asyncio
    async def test_remove_nonexistent_ignored(self, gateway):
        """Removing a nonexistent entry is a no-op."""
        removed = await gateway.remove(
            task_id="task-1",
            actor="cleaner",
            capabilities=["board_maintenance"],
            entry_ids=["nonexistent"],
            reason="cleanup",
        )
        assert removed == []

    @pytest.mark.asyncio
    async def test_remove_event_logged(self, gateway, board_store, event_emitter):
        """Remove creates an event in the log."""
        entries = await gateway.append(
            task_id="task-1",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
        )

        await gateway.remove(
            task_id="task-1",
            actor="cleaner",
            capabilities=["board_maintenance"],
            entry_ids=[entries[0].id],
            reason="cleanup",
        )

        events = await board_store.get_events("task-1")
        removal_events = [e for e in events if e["event_type"] == "entry_removed"]
        assert len(removal_events) == 1


# ── Concurrency Tests ────────────────────────────────────────────────


class TestConcurrency:
    """Test per-task lock serialization (doc 04 §6)."""

    @pytest.mark.asyncio
    async def test_concurrent_appends_serialized(self, gateway, board_store):
        """Two concurrent appends → both succeed, seqs are unique."""
        async def do_append(body: str):
            return await gateway.append(
                task_id="task-1",
                actor="expert.x",
                capabilities=["finding_writer"],
                proposed=[make_proposed_entry(body=body)],
                turn_id="turn-1",
            )

        results = await asyncio.gather(
            do_append("First concurrent"),
            do_append("Second concurrent"),
        )

        all_entries = []
        for r in results:
            all_entries.extend(r)

        assert len(all_entries) == 2
        ids = [e.id for e in all_entries]
        assert len(set(ids)) == 2  # All unique IDs

    @pytest.mark.asyncio
    async def test_different_tasks_independent(self, gateway, board_store):
        """Different tasks don't block each other."""
        _entries_a = await gateway.append(
            task_id="task-a",
            actor="expert.x",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
        )
        _entries_b = await gateway.append(
            task_id="task-b",
            actor="expert.y",
            capabilities=["finding_writer"],
            proposed=[make_proposed_entry()],
            turn_id="turn-1",
        )

        snap_a = await board_store.get_snapshot("task-a")
        snap_b = await board_store.get_snapshot("task-b")
        assert len(snap_a) == 1
        assert len(snap_b) == 1


# ── Set Meta Tests ───────────────────────────────────────────────────


class TestSetMeta:
    """Test board metadata updates."""

    @pytest.mark.asyncio
    async def test_set_meta(self, gateway, board_store):
        """set_meta updates board metadata."""
        await gateway.set_meta("task-1", phase="executing", round=2)
        meta = await board_store.get_meta("task-1")
        assert meta["phase"] == "executing"
        assert meta["round"] == 2

    @pytest.mark.asyncio
    async def test_set_meta_partial_update(self, gateway, board_store):
        """set_meta merges, doesn't replace."""
        await gateway.set_meta("task-1", phase="executing", round=1)
        await gateway.set_meta("task-1", round=2)
        meta = await board_store.get_meta("task-1")
        assert meta["phase"] == "executing"
        assert meta["round"] == 2


# ── Replay Determinism Test ──────────────────────────────────────────


class TestReplayDeterminism:
    """Test that folding events produces the same snapshot as live ops."""

    @pytest.mark.asyncio
    async def test_full_replay_matches_live(self, gateway_no_hooks, board_store):
        """Full replay: fold all events → matches live snapshot."""
        gw = gateway_no_hooks

        # Build a board through various operations
        _entries = await gw.append(
            task_id="task-1",
            actor="planner",
            capabilities=["plan_writer"],
            proposed=[{"type": "plan", "body": "Step 1: research\nStep 2: analyze", "title": "Plan"}],
            turn_id="turn-1",
            round_no=1,
        )

        for i in range(3):
            await gw.append(
                task_id="task-1",
                actor=f"expert.domain-{i}",
                capabilities=["finding_writer"],
                proposed=[make_proposed_entry(body=f"Finding {i}")],
                turn_id=f"turn-{i+2}",
                round_no=1,
            )

        await gw.append(
            task_id="task-1",
            actor="critic",
            capabilities=["critique_writer"],
            proposed=[make_critique_entry()],
            turn_id="turn-5",
            round_no=2,
        )

        # Remove one entry
        snap_before = await board_store.get_snapshot("task-1")
        finding_ids = [e.id for e in snap_before.values() if e.type == "finding"]
        if finding_ids:
            await gw.remove(
                task_id="task-1",
                actor="cleaner",
                capabilities=["board_maintenance"],
                entry_ids=[finding_ids[0]],
                reason="cleanup",
            )

        # Get live snapshot
        live_snapshot = await board_store.get_snapshot("task-1")

        # Get all events and fold them
        from core.board_store import fold_events_to_snapshot
        events = await board_store.get_events("task-1")
        replayed_snapshot = fold_events_to_snapshot(events)

        # Compare: same entry ids, same statuses
        assert set(live_snapshot.keys()) == set(replayed_snapshot.keys())
        for entry_id in live_snapshot:
            assert live_snapshot[entry_id].status == replayed_snapshot[entry_id].status
            assert live_snapshot[entry_id].type == replayed_snapshot[entry_id].type
            assert live_snapshot[entry_id].body == replayed_snapshot[entry_id].body
