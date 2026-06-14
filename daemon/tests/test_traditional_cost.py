# /opt/bmas/daemon/tests/test_traditional_cost.py
"""Unit tests for control-plane LLM cost capture (doc 06 §3.1).

The CU/AG/SolE calls in the traditional variant are real billable LiteLLM
completions. These tests verify that ``_record_llm_cost`` turns a response
``usage`` block into an accumulated budget, a per-call cost entry, and a
live ``cost`` SSE event — the link that was previously missing (cost/tokens
always showed 0).
"""

import pytest

import config
from core.event_emitter import InMemoryEventEmitter
from core.variants.traditional import TraditionalVariant


def _make_variant(emitter: InMemoryEventEmitter) -> TraditionalVariant:
    cfg = {
        "max_rounds": 4,
        "max_duration_s": 1800,
        "budget_ceiling_usd": 0.50,
        "max_concurrent_activations": 3,
        "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 3},
        "cleaner_entry_threshold": 12,
        "stall_rounds": 2,
        "cu_mode": "llm",
        "coordinator_narration": False,
        "sole_similarity": "auto",
    }
    return TraditionalVariant(
        gateway=None,
        board_store=None,
        event_emitter=emitter,
        triage=None,
        config=cfg,
        litellm_url="http://litellm",
        litellm_key="key",
        node_endpoints=[],
        role_registry={},
        model_routing={"light": "test-light", "medium": "test-medium"},
    )


@pytest.mark.asyncio
async def test_record_llm_cost_accumulates_and_emits(monkeypatch):
    """usage → budget accumulation + cost SSE event with correct math."""
    monkeypatch.setattr(
        config,
        "MODEL_PRICING",
        {"test-light": {
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
            "source": "test",
        }},
        raising=False,
    )
    emitter = InMemoryEventEmitter()
    v = _make_variant(emitter)

    await v._record_llm_cost(
        "t1",
        {"prompt_tokens": 100, "completion_tokens": 50, "model": "test-light"},
        "test-light",
        "control_plane:cu",
    )

    expected = 100 * 1e-6 + 50 * 2e-6
    assert v.budget_spent == pytest.approx(expected)

    events = emitter.events_of_type("cost")
    assert len(events) == 1
    assert events[0]["input_tokens"] == 100
    assert events[0]["output_tokens"] == 50
    assert events[0]["cost_usd"] == pytest.approx(expected)
    assert events[0]["model"] == "test-light"


@pytest.mark.asyncio
async def test_record_llm_cost_skips_missing_or_empty_usage(monkeypatch):
    """No usage / zero tokens / no task_id → no budget change, no event."""
    monkeypatch.setattr(config, "MODEL_PRICING", {}, raising=False)
    emitter = InMemoryEventEmitter()
    v = _make_variant(emitter)

    await v._record_llm_cost("t1", None, "test-light", "x")
    await v._record_llm_cost("t1", {"prompt_tokens": 0, "completion_tokens": 0}, "test-light", "x")
    await v._record_llm_cost(None, {"prompt_tokens": 9, "completion_tokens": 9}, "test-light", "x")

    assert v.budget_spent == 0.0
    assert emitter.events_of_type("cost") == []


@pytest.mark.asyncio
async def test_record_llm_cost_zero_when_pricing_missing(monkeypatch):
    """Tokens still recorded/emitted with cost 0 when no pricing for model."""
    monkeypatch.setattr(config, "MODEL_PRICING", {}, raising=False)
    emitter = InMemoryEventEmitter()
    v = _make_variant(emitter)

    await v._record_llm_cost(
        "t1",
        {"prompt_tokens": 10, "completion_tokens": 5},
        "unknown-model",
        "control_plane:ag",
    )

    assert v.budget_spent == 0.0
    events = emitter.events_of_type("cost")
    assert len(events) == 1
    assert events[0]["input_tokens"] == 10
    assert events[0]["output_tokens"] == 5
    assert events[0]["cost_usd"] == 0.0
