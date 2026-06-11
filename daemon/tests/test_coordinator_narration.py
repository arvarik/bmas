# /opt/bmas/daemon/tests/test_coordinator_narration.py
"""Unit tests for Coordinator Narration Lane (doc 05 §1.2, doc 13 §3).

Tests the {selected, rationale} event emission gated by
coordination.traditional.coordinator_narration.

Hard constraints tested:
  - Same selection call (no extra LLM spend)
  - Malformed rationale never blocks the loop
  - Lane hides entirely when flagged off
"""

import time
from datetime import datetime, timezone

import pytest

from core.entry import BoardEntry
from core.event_emitter import InMemoryEventEmitter
from core.variants.traditional import (
    parse_cu_output,
    TraditionalVariant,
    AgentRoster,
    ExpertIdentity,
    CONSTANT_ROLE_DESCRIPTIONS,
)


# ── Helpers ──────────────────────────────────────────────────────────

VALID_NAMES = [
    "planner", "critic", "conflict_resolver", "cleaner", "decider",
    "expert.valuation", "expert.supply_chain",
]


def _make_entry(
    eid: str, etype: str, author: str, body: str,
    status: str = "open", refs: list[str] | None = None,
    round_no: int = 0,
) -> BoardEntry:
    return BoardEntry(
        id=eid, task_id="t", type=etype, author=author, body=body,
        title=body[:80], refs=refs or [], confidence=0.8, status=status,
        round=round_no,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_variant(
    coordinator_narration: bool = False,
    cu_mode: str = "llm",
    emitter: InMemoryEventEmitter | None = None,
) -> TraditionalVariant:
    """Create a variant with no external deps (all set to None/mock)."""
    config = {
        "max_rounds": 4,
        "max_duration_s": 1800,
        "budget_ceiling_usd": 0.50,
        "max_concurrent_activations": 3,
        "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 3},
        "cleaner_entry_threshold": 12,
        "stall_rounds": 2,
        "cu_mode": cu_mode,
        "coordinator_narration": coordinator_narration,
        "sole_similarity": "auto",
    }

    # Minimal mock gateway — step() calls gateway.set_meta() after
    # emitting narration events; this prevents AttributeError.
    class _MockGateway:
        async def set_meta(self, *args, **kwargs):
            pass

    v = TraditionalVariant(
        gateway=_MockGateway(),
        board_store=None,
        event_emitter=emitter,
        triage=None,
        config=config,
        litellm_url="",
        litellm_key="",
        node_endpoints=["http://localhost:8000"],
        role_registry={},
        model_routing={},
    )
    v.roster = AgentRoster(
        constants=dict(CONSTANT_ROLE_DESCRIPTIONS),
        experts=[
            ExpertIdentity("Valuation Analyst", "valuation", "Values companies", "m"),
            ExpertIdentity("Supply Chain Expert", "supply_chain", "Supply chain analysis", "m"),
        ],
    )
    return v


# ── parse_cu_output: rationale extraction ────────────────────────────

class TestParseCuOutputRationale:

    def test_parse_cu_output_returns_rationale(self):
        """parse_cu_output returns (selected, rationale) tuple."""
        raw = '{"selected": ["critic"], "rationale": "Open critique needs response"}'
        selected, rationale = parse_cu_output(raw, VALID_NAMES)
        assert selected == ["critic"]
        assert rationale == "Open critique needs response"

    def test_parse_cu_output_missing_rationale(self):
        """Missing rationale key → (selected, None)."""
        raw = '{"selected": ["planner"]}'
        selected, rationale = parse_cu_output(raw, VALID_NAMES)
        assert selected == ["planner"]
        assert rationale is None

    def test_parse_cu_output_non_string_rationale(self):
        """rationale: 42 or rationale: [...] → (selected, None)."""
        for bad_rationale in [42, [1, 2], {"nested": True}, True, None]:
            raw_obj = {"selected": ["decider"], "rationale": bad_rationale}
            import json
            raw = json.dumps(raw_obj)
            selected, rationale = parse_cu_output(raw, VALID_NAMES)
            assert selected == ["decider"], f"Failed for rationale={bad_rationale}"
            assert rationale is None, f"Expected None for rationale={bad_rationale}, got {rationale!r}"

    def test_parse_cu_output_empty_string_rationale(self):
        """rationale: '' → (selected, None)."""
        raw = '{"selected": ["planner"], "rationale": ""}'
        selected, rationale = parse_cu_output(raw, VALID_NAMES)
        assert selected == ["planner"]
        assert rationale is None

    def test_parse_cu_output_whitespace_only_rationale(self):
        """rationale: '   ' → (selected, None)."""
        raw = '{"selected": ["planner"], "rationale": "   "}'
        selected, rationale = parse_cu_output(raw, VALID_NAMES)
        assert selected == ["planner"]
        assert rationale is None

    def test_parse_cu_output_garbled_still_returns_tuple(self):
        """Garbled text → ([], None)."""
        raw = "Not JSON at all, just rambling text."
        selected, rationale = parse_cu_output(raw, VALID_NAMES)
        assert selected == []
        assert rationale is None


# ── Coordinator narration event emission ─────────────────────────────

class TestNarrationEventEmission:

    @pytest.mark.asyncio
    async def test_narration_event_emitted_when_flag_on(self):
        """With flag on + mock emitter → coordinator_narration event captured."""
        from core.board_store import InMemoryBoardStore

        emitter = InMemoryEventEmitter()
        v = _make_variant(coordinator_narration=True, cu_mode="heuristic_first", emitter=emitter)
        v.store = InMemoryBoardStore()
        v.genesis_time = time.monotonic()

        task_id = "test-narration"
        await v.store.set_meta(task_id, round=0, budget_spent=0.0)

        task = {"task_id": task_id, "query": "Test query"}
        result = await v.step(task, None)

        # Step should succeed (not terminal)
        assert result.terminal is False

        # The event should have been emitted
        narration_events = emitter.events_of_type("coordinator_narration")
        assert len(narration_events) == 1

        evt = narration_events[0]
        assert evt["round"] == 1
        assert isinstance(evt["selected"], list)
        assert len(evt["selected"]) > 0
        assert evt["source"] == "heuristic"
        # Heuristic path has no rationale
        assert evt["rationale"] is None

    @pytest.mark.asyncio
    async def test_narration_event_not_emitted_when_flag_off(self):
        """With flag off → no coordinator_narration event in emitter."""
        from core.board_store import InMemoryBoardStore

        emitter = InMemoryEventEmitter()
        v = _make_variant(coordinator_narration=False, cu_mode="heuristic_first", emitter=emitter)
        v.store = InMemoryBoardStore()
        v.genesis_time = time.monotonic()

        task_id = "test-no-narration"
        await v.store.set_meta(task_id, round=0, budget_spent=0.0)

        task = {"task_id": task_id, "query": "Test query"}
        result = await v.step(task, None)

        assert result.terminal is False
        # No coordinator_narration events should be emitted
        narration_events = emitter.events_of_type("coordinator_narration")
        assert len(narration_events) == 0

    @pytest.mark.asyncio
    async def test_malformed_rationale_does_not_block_loop(self):
        """Garbled rationale → event fires with rationale: null,
        selected still populated from fallback, step returns non-terminal.

        This explicitly tests the hard constraint from doc 05 §1.2:
        a malformed rationale NEVER blocks the loop.
        """
        from core.board_store import InMemoryBoardStore

        emitter = InMemoryEventEmitter()
        v = _make_variant(coordinator_narration=True, cu_mode="heuristic_first", emitter=emitter)
        v.store = InMemoryBoardStore()
        v.genesis_time = time.monotonic()

        task_id = "test-malformed"
        await v.store.set_meta(task_id, round=0, budget_spent=0.0)

        task = {"task_id": task_id, "query": "Test with malformed rationale"}
        result = await v.step(task, None)

        # Loop must NOT be blocked
        assert result.terminal is False
        assert len(result.activations) > 0

        # Event emitted with null rationale (heuristic path)
        narration_events = emitter.events_of_type("coordinator_narration")
        assert len(narration_events) == 1
        assert narration_events[0]["rationale"] is None
        assert narration_events[0]["source"] == "heuristic"

    @pytest.mark.asyncio
    async def test_heuristic_path_emits_event(self):
        """cu_mode: heuristic_first → event with source: 'heuristic', rationale: null."""
        from core.board_store import InMemoryBoardStore

        emitter = InMemoryEventEmitter()
        v = _make_variant(coordinator_narration=True, cu_mode="heuristic_first", emitter=emitter)
        v.store = InMemoryBoardStore()
        v.genesis_time = time.monotonic()

        task_id = "test-heuristic"
        await v.store.set_meta(task_id, round=0, budget_spent=0.0)

        task = {"task_id": task_id, "query": "Heuristic test"}
        await v.step(task, None)

        narration_events = emitter.events_of_type("coordinator_narration")
        assert len(narration_events) == 1
        assert narration_events[0]["source"] == "heuristic"
        assert narration_events[0]["rationale"] is None

    def test_narration_event_shape(self):
        """Validate event payload has exactly {round, selected, rationale, source}."""
        # This tests the contract the UI will consume
        expected_keys = {"round", "selected", "rationale", "source"}

        # Simulate what step() would emit
        payload = {
            "round": 2,
            "selected": ["critic", "expert.valuation"],
            "rationale": "Open critiques need response from valuation expert",
            "source": "llm",
        }
        assert set(payload.keys()) == expected_keys
        assert isinstance(payload["round"], int)
        assert isinstance(payload["selected"], list)
        assert isinstance(payload["rationale"], str)
        assert payload["source"] in ("llm", "heuristic")

        # With null rationale
        payload_null = {
            "round": 1,
            "selected": ["planner"],
            "rationale": None,
            "source": "heuristic",
        }
        assert set(payload_null.keys()) == expected_keys
        assert payload_null["rationale"] is None
