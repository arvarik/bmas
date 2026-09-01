"""Foundation Stage 0H: writer enablement stays gated.

No new writer enables until its conformance column, the populated
migration and rollback gates, and the security gate all pass, and its
feature flag is on. A planned runtime pair is never a runnable choice
until its complete column passes.
"""
from __future__ import annotations

import pytest

import capability_publication as cap
import conformance_kit as kit
import release_gates as gates
from core.foundation_gates import gate_states
from core.variants import RuntimeKey

REFERENCE = RuntimeKey("reference", "1")
CLASSIC_NATIVE = RuntimeKey("classic", "2")


def test_every_feature_gate_defaults_disabled():
    # The Stage 0A default keeps every planned writer gate disabled.
    assert gate_states() == {name: False for name in gate_states()}


def test_a_writer_stays_disabled_until_every_gate_passes():
    ledger = gates.GateLedger()
    # No gate has passed yet: the writer is blocked on all gates.
    assert not gates.writer_may_enable(
        "activation_ledger", CLASSIC_NATIVE, ledger,
    )
    reason = gates.blocked_reason("activation_ledger", CLASSIC_NATIVE, ledger)
    assert reason is not None
    assert reason.startswith("gates_not_passed:")
    for gate in gates.RELEASE_GATES:
        assert gate in reason


def test_partial_gates_keep_the_writer_blocked():
    ledger = gates.GateLedger()
    ledger.record_pass("conformance", CLASSIC_NATIVE)
    ledger.record_pass("populated_migration", CLASSIC_NATIVE)
    # Two gates still missing.
    missing = ledger.missing_gates(CLASSIC_NATIVE)
    assert set(missing) == {"supported_downgrade", "security"}
    assert not gates.writer_may_enable(
        "activation_ledger", CLASSIC_NATIVE, ledger,
    )


def test_all_gates_passed_but_feature_flag_still_off():
    ledger = gates.GateLedger()
    for gate in gates.RELEASE_GATES:
        ledger.record_pass(gate, CLASSIC_NATIVE)
    assert ledger.all_gates_passed(CLASSIC_NATIVE)
    # Every gate passed, yet the feature flag stays off by default, so
    # the writer still does not enable.
    assert not gates.writer_may_enable(
        "activation_ledger", CLASSIC_NATIVE, ledger,
    )
    assert gates.blocked_reason(
        "activation_ledger", CLASSIC_NATIVE, ledger,
    ) == "feature_gate_disabled"


def test_conformance_gate_records_only_on_a_full_pass():
    ledger = gates.GateLedger()
    directory = cap.CapabilityDirectory()
    # A full pass records the conformance gate.
    passing = kit.run_conformance_suite(
        kit.ConformanceAdapter(directory.get(REFERENCE)),
    )
    ledger.record_conformance(passing)
    assert ledger.gate_passed("conformance", REFERENCE)
    # A failing report records nothing.
    import dataclasses

    reference = directory.get(REFERENCE)
    broken_record = dataclasses.replace(
        reference,
        runtime_key=CLASSIC_NATIVE,
        capabilities={
            **reference.capabilities,
            "nested_receipts": "legacy_unobservable",
        },
    )
    failing = kit.run_conformance_suite(
        kit.ConformanceAdapter(broken_record),
    )
    ledger.record_conformance(failing)
    assert not ledger.gate_passed("conformance", CLASSIC_NATIVE)


def test_unknown_gates_and_writers_fail_closed():
    ledger = gates.GateLedger()
    with pytest.raises(gates.ReleaseGateError):
        ledger.record_pass("invented_gate", CLASSIC_NATIVE)
    with pytest.raises(gates.ReleaseGateError):
        gates.writer_may_enable("invented_writer", CLASSIC_NATIVE, ledger)
