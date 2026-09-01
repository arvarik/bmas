"""Foundation Stage 0H: the cross-runtime conformance suite.

The shared case set runs against the deterministic reference adapter
and the Classic v1, PatchBoard v1, and Stigmergic v1 compatibility
adapters. Each v1 pair asserts its declared matrix value instead of a
v2 guarantee, every pair keeps the generic interface fallback, and no
v1 execution creates a v2 authority record. The planned v2 records
exist for interface development but are never runnable choices.
"""
from __future__ import annotations

import pytest

import capability_publication as cap
import conformance_kit as kit
from core.variants import RuntimeKey

REFERENCE = RuntimeKey("reference", "1")
CLASSIC_LEGACY = RuntimeKey("classic", "1")
PATCHBOARD_LEGACY = RuntimeKey("patchboard", "1")
STIGMERGIC_LEGACY = RuntimeKey("stigmergic", "1")
CLASSIC_NATIVE = RuntimeKey("classic", "2")
PATCHBOARD_NATIVE = RuntimeKey("patchboard", "2")

STAGE0_QUALIFIED = (REFERENCE, CLASSIC_LEGACY, PATCHBOARD_LEGACY, STIGMERGIC_LEGACY)
LEGACY_PAIRS = (CLASSIC_LEGACY, PATCHBOARD_LEGACY, STIGMERGIC_LEGACY)


@pytest.fixture()
def directory():
    return cap.CapabilityDirectory()


# ── Capability publication ───────────────────────────────────────────


def test_one_capability_record_per_runtime_pair(directory):
    keys = set(directory.records)
    assert keys == {
        REFERENCE, CLASSIC_LEGACY, PATCHBOARD_LEGACY, STIGMERGIC_LEGACY,
        CLASSIC_NATIVE, PATCHBOARD_NATIVE,
    }
    for record in directory.records.values():
        # Every record declares a value for every conformance capability.
        assert set(record.capabilities) == set(cap.CONFORMANCE_CAPABILITIES)


def test_only_qualified_pairs_are_runnable_choices(directory):
    assert set(directory.runnable_choices()) == set(STAGE0_QUALIFIED)
    # The planned native records exist for interface development.
    assert set(directory.planned_pairs()) == {CLASSIC_NATIVE, PATCHBOARD_NATIVE}
    for planned in directory.planned_pairs():
        assert not directory.get(planned).is_runnable_choice()


def test_legacy_records_mark_unsupported_native_capabilities(directory):
    for key in LEGACY_PAIRS:
        record = directory.get(key)
        unsupported = record.unsupported_native_capabilities()
        # The matrix declares these unsupported native capabilities.
        assert "activation_dispatch_outbox" in unsupported
        assert "signed_activation_acknowledgement" in unsupported
        assert "nested_receipts" in unsupported
        assert record.capabilities["agent_protocol"] == "legacy"
        assert record.capabilities["budget_reservation"] == "advisory_legacy"
        assert record.capabilities["nested_receipts"] == "legacy_unobservable"
        # Every pair keeps the generic fallback.
        assert record.ui_fallback
        assert "trace" in record.ui_fallback_panels


def test_patchboard_and_stigmergic_lack_a_typed_evidence_index(directory):
    for key in (PATCHBOARD_LEGACY, STIGMERGIC_LEGACY):
        record = directory.get(key)
        assert record.capabilities["typed_evidence_index"] == "unavailable"
    # Classic legacy has a legacy source adapter for evidence.
    assert directory.get(CLASSIC_LEGACY).capabilities[
        "typed_evidence_index"
    ] == "legacy"


def test_reference_adapter_is_native_contract(directory):
    record = directory.get(REFERENCE)
    assert record.availability == "qualified"
    assert record.unsupported_native_capabilities() == []
    assert record.agent_protocol_version == "2"


def test_ui_adapter_selection_and_fallback(directory):
    # A known pair selects its adapter; the client never renders a legacy
    # state with a native adapter.
    assert directory.select_ui_adapter(CLASSIC_LEGACY) == "classic_legacy"
    assert directory.select_ui_adapter(CLASSIC_NATIVE) == "classic_native"
    # An unknown pair uses the generic fallback.
    assert directory.select_ui_adapter(
        RuntimeKey("unknown", "9"),
    ) == "generic_fallback"


def test_bad_records_fail_closed():
    with pytest.raises(cap.CapabilityPublicationError):
        cap.RuntimeCapabilityRecord(
            runtime_key=RuntimeKey("broken", "1"),
            canonical_label="Broken",
            historical_label=None,
            availability="invented_state",
            schema_versions={},
            capabilities={
                capability: "native"
                for capability in cap.CONFORMANCE_CAPABILITIES
            },
            agent_protocol_version="2",
            agent_receipt_version="1",
            effect_schema_version="1",
            supports_seed_state=True,
            supports_assets=True,
            supports_cancellation=True,
            supports_recovery=True,
            supports_evidence=True,
            supports_budget=True,
            nested_effect_receipts=True,
            provider_qualification="qualified",
            benchmark_qualification="qualified",
            ui_adapter="broken",
        )


# ── The shared conformance suite ─────────────────────────────────────


def test_the_suite_covers_every_conformance_capability():
    assert kit.suite_covers_every_capability()


@pytest.mark.parametrize("key", STAGE0_QUALIFIED)
def test_each_stage0_adapter_passes_the_shared_suite(directory, key):
    adapter = kit.ConformanceAdapter(directory.get(key))
    report = kit.run_conformance_suite(adapter)
    assert report.passed, report.failures()
    assert len(report.case_results) == len(kit.CONFORMANCE_CASES)


@pytest.mark.parametrize("key", LEGACY_PAIRS)
def test_legacy_execution_creates_no_native_authority_record(directory, key):
    adapter = kit.ConformanceAdapter(directory.get(key))
    report = kit.run_conformance_suite(adapter)
    # The activation and effect ledger case proves the legacy adapter wrote
    # no native authority record.
    ledger_case = next(
        result for result in report.case_results
        if result.case_id == "activation_and_effect_ledgers"
    )
    assert ledger_case.passed
    assert adapter.native_authority_writes == []


@pytest.mark.parametrize("key", LEGACY_PAIRS)
def test_legacy_pairs_prove_compatibility_not_native(directory, key):
    adapter = kit.ConformanceAdapter(directory.get(key))
    report = kit.run_conformance_suite(adapter)
    protocol_case = next(
        result for result in report.case_results
        if result.case_id == "agent_protocol_negotiation"
    )
    assert protocol_case.expected_value == "legacy"
    assert protocol_case.observed_value == "legacy"
    assert protocol_case.passed


def test_a_native_adapter_that_regresses_a_capability_fails(directory):
    # If a native adapter dropped its signed acknowledgement, the
    # protocol case fails, so the column cannot pass.
    record = directory.get(REFERENCE)
    broken = cap.RuntimeCapabilityRecord(
        **{
            **_record_fields(record),
            "capabilities": {
                **record.capabilities,
                "signed_activation_acknowledgement": "legacy_unavailable",
            },
        },
    )
    adapter = kit.ConformanceAdapter(broken)
    report = kit.run_conformance_suite(adapter)
    assert not report.passed
    assert any(
        result.case_id == "agent_protocol_negotiation"
        for result in report.failures()
    )


def test_reference_scoring_evidence_replays_deterministically():
    first = kit.score_reference_evidence()
    second = kit.score_reference_evidence()
    assert first["result_digest"] == second["result_digest"]
    assert first["result_bytes"] == second["result_bytes"]
    # The scorer returns a versioned result document.
    assert b'"contract_version":"1.0.0"' in first["result_bytes"]


def _record_fields(record):
    from capability_publication import _record_fields as fields

    return fields(record)
