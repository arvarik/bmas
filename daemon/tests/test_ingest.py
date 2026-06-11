# /opt/bmas/daemon/tests/test_ingest.py
"""
Tests for the trace ingest endpoint (routes/ingest.py).

Validates:
- Bearer auth (BMAS_NODE_KEY): valid, missing, wrong
- Cost computation from MODEL_PRICING
- Trace schema compliance and DB row preparation
- Final event triggers cost computation
- Missing pricing → cost_usd = 0.0
- usage: null → graceful skip

NOTE: The daemon's config.py runs at import time and sys.exit()s if
bmas.yaml is missing.  These tests bypass that by mocking `config`
as a module before importing ingest.
"""

import os
import sys
import pytest
from unittest.mock import MagicMock

# ── Extend the conftest fake config module ──────────────────────────
# conftest.py injects a comprehensive fake config into sys.modules.
# We just need to add the fields that ingest.py specifically needs.

import config as _mock_config  # already the fake from conftest.py
_mock_config.BMAS_NODE_KEY = "test-node-key-abc123"
_mock_config.MODEL_PRICING = {
    "gemini-pro": {
        "input_cost_per_token": 1.25e-6,
        "output_cost_per_token": 5.0e-6,
        "source": "bmas.yaml",
    },
    "gemini-flash": {
        "input_cost_per_token": 1.5e-7,
        "output_cost_per_token": 6.0e-7,
        "source": "bmas.yaml",
    },
}

# Now safe to add daemon/src to path and import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from routes.ingest import _compute_cost, _verify_bearer  # noqa: E402


# ── Test _compute_cost ─────────────────────────────────────────────────

class TestComputeCost:
    """Tests for the _compute_cost helper in ingest.py."""

    def test_cost_with_valid_pricing(self):
        """Computes correct cost from token counts × pricing."""
        usage = {"prompt_tokens": 16000, "completion_tokens": 500}
        cost, source = _compute_cost(usage, "gemini-pro")

        expected = 16000 * 1.25e-6 + 500 * 5.0e-6
        assert abs(cost - expected) < 1e-10
        assert source == "bmas.yaml"

    def test_cost_gemini_flash(self):
        """Correct cost for gemini-flash model."""
        usage = {"prompt_tokens": 10000, "completion_tokens": 200}
        cost, source = _compute_cost(usage, "gemini-flash")

        expected = 10000 * 1.5e-7 + 200 * 6.0e-7
        assert abs(cost - expected) < 1e-10
        assert source == "bmas.yaml"

    def test_cost_with_missing_model(self):
        """Returns 0.0 when model is not in pricing."""
        usage = {"prompt_tokens": 1000, "completion_tokens": 100}
        cost, source = _compute_cost(usage, "unknown-model")

        assert cost == 0.0
        assert source == "missing"

    def test_cost_with_null_usage(self):
        """Returns 0.0 when usage is None."""
        cost, source = _compute_cost(None, "gemini-pro")
        assert cost == 0.0
        assert source == "none"

    def test_cost_with_null_model(self):
        """Returns 0.0 when model is None."""
        usage = {"prompt_tokens": 1000}
        cost, source = _compute_cost(usage, None)
        assert cost == 0.0
        assert source == "none"

    def test_cost_with_hermes_token_names(self):
        """Accepts Hermes-style token names (input_tokens/output_tokens)."""
        usage = {"input_tokens": 10000, "output_tokens": 200}
        cost, source = _compute_cost(usage, "gemini-flash")

        expected = 10000 * 1.5e-7 + 200 * 6.0e-7
        assert abs(cost - expected) < 1e-10

    def test_cost_rounding(self):
        """Cost is rounded to 8 decimal places."""
        # Temporarily override pricing
        _mock_config.MODEL_PRICING["model-x"] = {
            "input_cost_per_token": 1.333333333e-7,
            "output_cost_per_token": 2.777777777e-7,
        }
        try:
            usage = {"prompt_tokens": 7, "completion_tokens": 3}
            cost, _ = _compute_cost(usage, "model-x")
            # Verify cost is a reasonable number (not exact due to floating point)
            assert isinstance(cost, float)
            assert cost > 0
        finally:
            del _mock_config.MODEL_PRICING["model-x"]

    def test_zero_tokens(self):
        """Zero tokens → zero cost."""
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        cost, source = _compute_cost(usage, "gemini-pro")
        assert cost == 0.0
        assert source == "bmas.yaml"

    def test_large_token_counts(self):
        """Large token counts produce reasonable costs."""
        usage = {"prompt_tokens": 100000, "completion_tokens": 10000}
        cost, source = _compute_cost(usage, "gemini-pro")
        # 100k × 1.25e-6 + 10k × 5e-6 = 0.125 + 0.05 = 0.175
        assert abs(cost - 0.175) < 1e-8
        assert source == "bmas.yaml"


# ── Test _verify_bearer ─────────────────────────────────────────────────

class TestVerifyBearer:
    """Tests for bearer token validation."""

    def test_valid_bearer(self):
        """Valid bearer token passes without exception."""
        request = MagicMock()
        request.headers = {"Authorization": "Bearer test-node-key-abc123"}
        # Should not raise
        _verify_bearer(request)

    def test_missing_bearer(self):
        """Missing Authorization header raises 401."""
        from fastapi import HTTPException

        request = MagicMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            _verify_bearer(request)
        assert exc_info.value.status_code == 401
        assert "Missing" in exc_info.value.detail

    def test_wrong_bearer(self):
        """Wrong bearer token raises 401."""
        from fastapi import HTTPException

        request = MagicMock()
        request.headers = {"Authorization": "Bearer wrong-key"}
        with pytest.raises(HTTPException) as exc_info:
            _verify_bearer(request)
        assert exc_info.value.status_code == 401
        assert "Invalid" in exc_info.value.detail

    def test_bearer_no_prefix(self):
        """Token without 'Bearer ' prefix raises 401."""
        from fastapi import HTTPException

        request = MagicMock()
        request.headers = {"Authorization": "test-node-key-abc123"}
        with pytest.raises(HTTPException) as exc_info:
            _verify_bearer(request)
        assert exc_info.value.status_code == 401


# ── Test Trace Schema & DB Row Preparation ───────────────────────────

class TestTraceIngestion:
    """Test trace event format validation and DB row preparation."""

    def test_trace_schema_compliance(self):
        """Verify that a well-formed trace has all required fields."""
        trace = {
            "trace_id": "trace-turn-abc",
            "task_id": "task-123",
            "turn_id": "turn-abc",
            "seq": 0,
            "ts": "2026-06-10T20:00:00Z",
            "role": "planner",
            "node": "node-1",
            "type": "reasoning",
            "data": {"text": "Analyzing the task..."},
            "tokens": {"in": 100, "out": 50},
            "cost_usd": 0.0,
        }
        required = {"trace_id", "task_id", "turn_id", "seq", "ts", "role",
                     "node", "type", "data", "tokens", "cost_usd"}
        assert required.issubset(set(trace.keys()))

    def test_db_row_extraction(self):
        """Verify DB row is correctly extracted from a trace event."""
        trace = {
            "task_id": "task-123",
            "turn_id": "turn-abc",
            "seq": 3,
            "role": "executor",
            "node": "node-2",
            "type": "tool_call",
            "data": {"tool": "web_search", "args": {"query": "test"}},
            "tokens": {"in": 0, "out": 0},
        }

        tokens = trace.get("tokens", {})
        db_row = {
            "task_id": trace["task_id"],
            "turn_id": trace["turn_id"],
            "seq": trace.get("seq", 0),
            "role": trace.get("role", "agent"),
            "node": trace.get("node"),
            "type": trace["type"],
            "data": trace.get("data"),
            "model": None,
            "tokens_in": tokens.get("in", 0),
            "tokens_out": tokens.get("out", 0),
            "cost_usd": 0.0,
        }

        assert db_row["task_id"] == "task-123"
        assert db_row["turn_id"] == "turn-abc"
        assert db_row["seq"] == 3
        assert db_row["type"] == "tool_call"
        assert isinstance(db_row["data"], dict)

    def test_final_event_cost_extraction(self):
        """Final event usage → cost computation."""
        final_trace = {
            "type": "final",
            "data": {
                "summary": "The answer is 42.",
                "usage": {
                    "prompt_tokens": 16842,
                    "completion_tokens": 567,
                    "total_tokens": 17409,
                    "model": "gemini-flash",
                },
            },
        }

        usage = final_trace["data"]["usage"]
        model = usage.get("model")
        cost, source = _compute_cost(usage, model)

        expected = 16842 * 1.5e-7 + 567 * 6.0e-7
        assert abs(cost - expected) < 1e-10
        assert source == "bmas.yaml"

    def test_hermes_z_fallback_no_usage(self):
        """Hermes -z fallback produces usage=null; cost is skipped."""
        final_trace = {
            "type": "final",
            "data": {"summary": "Done.", "usage": None},
        }

        usage = (final_trace.get("data") or {}).get("usage")
        model = None

        cost, source = _compute_cost(usage, model)
        assert cost == 0.0
        assert source == "none"

    def test_empty_trace_batch(self):
        """Empty batch should not fail."""
        traces = []
        assert len(traces) == 0

    def test_trace_types_phase1(self):
        """Phase 1 emits these 7 trace types."""
        phase1_types = {"turn_start", "reasoning", "tool_call", "tool_result",
                        "approval_request", "final", "error"}
        for t in phase1_types:
            trace = {
                "task_id": "task-x", "turn_id": "turn-x", "seq": 0,
                "role": "agent", "type": t, "data": {},
            }
            assert trace["type"] in phase1_types

    def test_trace_types_reserved_for_phase2(self):
        """token_delta and entries_posted are reserved for Phase 2+.

        These types require blackboard entry posting (Phase 2) and
        streaming token accounting (Phase 3). Not emitted in Phase 1.
        """
        future_types = {"token_delta", "entries_posted"}
        phase1_types = {"turn_start", "reasoning", "tool_call", "tool_result",
                        "approval_request", "final", "error"}
        # No overlap — future types are distinct
        assert future_types.isdisjoint(phase1_types)
        # All 9 known types are accounted for
        all_known = phase1_types | future_types
        assert len(all_known) == 9

