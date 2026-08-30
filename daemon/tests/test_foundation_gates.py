"""Foundation Stage 0A: planned shared-writer gates stay present and disabled."""

from __future__ import annotations

import re

import pytest

from config_schema import BmasConfig, FoundationGatesConfig
from core.foundation_gates import (
    PLANNED_WRITER_GATES,
    UnknownGateError,
    gate_enabled,
    gate_states,
)

EXPECTED_GATES = (
    "runtime_registry",
    "run_context",
    "runtime_unit_of_work",
    "activation_ledger",
    "effect_ledger",
    "budget_reservations",
    "trace_envelope",
    "evidence_index",
    "goal_index",
)


def test_every_planned_writer_has_exactly_one_gate():
    assert PLANNED_WRITER_GATES == EXPECTED_GATES


def test_every_gate_defaults_to_disabled():
    assert gate_states() == {name: False for name in EXPECTED_GATES}


def test_configuration_schema_defaults_keep_every_gate_disabled():
    section = FoundationGatesConfig()
    assert section.model_dump() == {name: False for name in EXPECTED_GATES}
    resolved = BmasConfig.model_fields["foundation_gates"]
    assert resolved.default_factory is FoundationGatesConfig


def test_unknown_gate_fails_closed():
    with pytest.raises(UnknownGateError, match="board_projection"):
        gate_enabled("board_projection")


def test_enabled_gate_reads_from_configuration(monkeypatch):
    import config

    monkeypatch.setattr(
        config, "FOUNDATION_GATES", {"effect_ledger": True}, raising=False
    )
    assert gate_enabled("effect_ledger") is True
    others = {name for name in EXPECTED_GATES if name != "effect_ledger"}
    assert all(gate_enabled(name) is False for name in others)


def test_gate_names_stay_generation_neutral():
    for name in EXPECTED_GATES:
        assert not re.search(r"(^|_)[vV][0-9]+(_|$)", name)
        assert not re.search(r"[0-9]$", name)
