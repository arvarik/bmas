# /opt/bmas/daemon/tests/test_cu_parser.py
"""Unit tests for CU output parser and deterministic fallback (doc 05 §1.1).

No LLM calls — tests the parser and fallback table against canned inputs.
"""

from datetime import datetime, timezone

from core.entry import BoardEntry
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


def _make_variant_with_roster() -> TraditionalVariant:
    config = {
        "max_rounds": 4, "max_duration_s": 1800, "budget_ceiling_usd": 0.50,
        "max_concurrent_activations": 3,
        "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 3},
        "cleaner_entry_threshold": 12, "stall_rounds": 2,
        "cu_mode": "llm", "sole_similarity": "auto",
    }
    v = TraditionalVariant(
        gateway=None, board_store=None, event_emitter=None, triage=None,
        config=config, litellm_url="", litellm_key="",
        node_endpoints=[], role_registry={}, model_routing={},
    )
    v.roster = AgentRoster(
        constants=dict(CONSTANT_ROLE_DESCRIPTIONS),
        experts=[
            ExpertIdentity("Valuation Analyst", "valuation", "Values companies", "m"),
            ExpertIdentity("Supply Chain Expert", "supply_chain", "Supply chain analysis", "m"),
        ],
    )
    return v


# ── parse_cu_output() ────────────────────────────────────────────────

class TestParseCuOutput:

    def test_valid_json(self):
        raw = '{"selected": ["critic", "expert.valuation"], "rationale": "Open critique"}'
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == ["critic", "expert.valuation"]

    def test_json_in_markdown_code_block(self):
        raw = '```json\n{"selected": ["planner"], "rationale": "Round 1"}\n```'
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == ["planner"]

    def test_garbled_output_returns_empty(self):
        raw = "I think we should ask the critic to review..."
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == []

    def test_unknown_names_dropped(self):
        raw = '{"selected": ["critic", "expert.unknown", "decider"], "rationale": "test"}'
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == ["critic", "decider"]

    def test_empty_selection_returns_empty(self):
        raw = '{"selected": [], "rationale": "nothing to do"}'
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == []

    def test_missing_selected_key_returns_empty(self):
        raw = '{"agents": ["critic"], "rationale": "test"}'
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == []

    def test_non_list_selected_returns_empty(self):
        raw = '{"selected": "critic", "rationale": "test"}'
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == []

    def test_non_string_items_dropped(self):
        raw = '{"selected": ["critic", 42, null, "decider"], "rationale": "test"}'
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == ["critic", "decider"]

    def test_non_dict_root_returns_empty(self):
        raw = '["critic", "decider"]'
        result = parse_cu_output(raw, VALID_NAMES)
        assert result == []


# ── Deterministic Fallback Table ─────────────────────────────────────

class TestDeterministicFallback:

    def test_round_1_returns_planner_plus_experts(self):
        v = _make_variant_with_roster()
        result = v._deterministic_fallback({}, current_round=1)
        assert "planner" in result
        assert "expert.valuation" in result
        assert "expert.supply_chain" in result

    def test_open_critiques_returns_critiqued_authors(self):
        v = _make_variant_with_roster()
        finding = _make_entry("e-1", "finding", "expert.valuation", "X", round_no=1)
        critique = _make_entry("e-2", "critique", "critic", "Wrong!", refs=["e-1"], round_no=2)
        snapshot = {"e-1": finding, "e-2": critique}
        result = v._deterministic_fallback(snapshot, current_round=3)
        assert "expert.valuation" in result

    def test_critiques_with_rebuttals_not_selected(self):
        """Addressed critiques → don't re-select the author."""
        v = _make_variant_with_roster()
        finding = _make_entry("e-1", "finding", "expert.valuation", "X", round_no=1)
        critique = _make_entry("e-2", "critique", "critic", "Wrong!", refs=["e-1"], round_no=2)
        rebuttal = _make_entry("e-3", "rebuttal", "expert.valuation", "No!", refs=["e-2"], round_no=2)
        snapshot = {"e-1": finding, "e-2": critique, "e-3": rebuttal}
        result = v._deterministic_fallback(snapshot, current_round=3)
        # The critique is addressed, so we should fall through to conflicts/cleaner/decider
        assert "expert.valuation" not in result or "decider" in result

    def test_open_conflicts_returns_conflict_resolver(self):
        v = _make_variant_with_roster()
        conflict = _make_entry("e-1", "conflict", "critic", "Contradiction!", round_no=2)
        snapshot = {"e-1": conflict}
        result = v._deterministic_fallback(snapshot, current_round=3)
        assert result == ["conflict_resolver"]

    def test_high_entry_count_returns_cleaner(self):
        v = _make_variant_with_roster()
        v.cleaner_threshold = 3  # Low threshold for test
        snapshot = {
            f"e-{i}": _make_entry(f"e-{i}", "finding", f"x{i}", f"Body {i}", round_no=2)
            for i in range(5)
        }
        result = v._deterministic_fallback(snapshot, current_round=3)
        assert result == ["cleaner"]

    def test_default_fallback_is_decider(self):
        v = _make_variant_with_roster()
        # No special conditions — just some findings, no critiques/conflicts
        finding = _make_entry("e-1", "finding", "expert.valuation", "X", round_no=2)
        snapshot = {"e-1": finding}
        result = v._deterministic_fallback(snapshot, current_round=3)
        assert result == ["decider"]

    def test_no_roster_returns_planner(self):
        v = _make_variant_with_roster()
        v.roster = None
        result = v._deterministic_fallback({}, current_round=1)
        assert result == ["planner"]

    def test_clamping_to_max_concurrent(self):
        """step() should clamp CU selection to max_concurrent."""
        v = _make_variant_with_roster()
        v.max_concurrent = 2
        result = v._deterministic_fallback({}, current_round=1)
        # Round 1 would normally return planner + 2 experts = 3
        # But _deterministic_fallback doesn't clamp; step() does
        assert len(result) == 3  # Not clamped here
        # The clamping happens in step() itself
