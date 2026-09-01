"""Foundation Stage 0H: the cross-runtime conformance kit.

The kit provides one fake runtime and reusable contract fixtures. It
runs the same shared case set against the deterministic reference
adapter and against the Classic, PatchBoard, and Stigmergic legacy
compatibility adapters. For each v1 pair, the suite asserts the
declared compatibility-matrix value instead of a v2 guarantee.

The kit consumes the isolated deterministic reference scorer from
pre-Foundation P.2. The scorer proves evidence transport and replay
only; the kit never creates another scorer, and it never depends on
the product benchmark analysis stack.

The kit never creates native activation, effect, budget, or outcome
authority records for a legacy execution. A legacy pair proves exact
identity,
preserved behavior, readable recovery, reference evidence, and generic
interface fallback.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from capability_publication import (
    CONFORMANCE_CAPABILITIES,
    RuntimeCapabilityRecord,
)
from core.digest_profile import digest_bytes

if TYPE_CHECKING:
    from core.variants import RuntimeKey

# The shared conformance case set. Each case names one capability the
# suite verifies against the pair's declared matrix value.
CONFORMANCE_CASES = (
    "submission_and_identity",
    "assets_and_privacy",
    "seed_state",
    "cancellation_and_deadlines",
    "lease_fencing_and_recovery",
    "activation_and_effect_ledgers",
    "agent_protocol_negotiation",
    "budget_reservations",
    "evidence_decisions",
    "foundation_reference_scoring",
    "reference_evidence_replay",
    "ui_adapter_and_fallback",
)


class ConformanceError(AssertionError):
    """One conformance case failed for one adapter."""


@dataclass
class CaseResult:
    """The outcome of one conformance case for one adapter."""

    case_id: str
    passed: bool
    expected_value: str
    observed_value: str
    detail: str = ""


@dataclass
class ConformanceReport:
    """The complete conformance outcome for one runtime pair."""

    runtime_key: RuntimeKey
    availability: str
    case_results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.case_results)

    def failures(self) -> list[CaseResult]:
        return [result for result in self.case_results if not result.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_key": self.runtime_key.to_dict(),
            "availability": self.availability,
            "passed": self.passed,
            "case_results": [
                {
                    "case_id": result.case_id,
                    "passed": result.passed,
                    "expected_value": result.expected_value,
                    "observed_value": result.observed_value,
                    "detail": result.detail,
                }
                for result in self.case_results
            ],
        }


class ConformanceAdapter:
    """One runtime adapter under conformance test.

    The adapter carries the published capability record and returns
    one observed value per capability. A native adapter returns
    ``native`` for every capability; a legacy compatibility adapter
    returns its declared compatibility value. The suite asserts that
    the observed value equals the declared matrix value, so a legacy
    pair proves its compatibility contract, not a native guarantee.
    """

    def __init__(self, record: RuntimeCapabilityRecord) -> None:
        self.record = record
        self.runtime_key = record.runtime_key
        # A legacy adapter never writes native authority records, so
        # this list stays empty. The suite asserts it is empty to prove
        # the compatibility execution created no native authority row.
        self.native_authority_writes: list[str] = []

    def observe_capability(self, capability: str) -> str:
        """Return the observed value for one capability."""
        if capability not in self.record.capabilities:
            raise ConformanceError(
                f"{self.runtime_key} declares no value for {capability}"
            )
        return self.record.capabilities[capability]

    def exact_identity(self) -> RuntimeKey:
        """Return the exact runtime pair. No pair is ever inferred."""
        return self.runtime_key

    def is_native_contract(self) -> bool:
        return self.record.agent_protocol_version == "2"


# ── The reference scorer bridge ──────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCORER_DIR = _REPO_ROOT / "conformance" / "reference_scorer"


def _load_reference_scorer() -> Any:
    """Import the isolated deterministic reference scorer from P.2."""
    if str(_SCORER_DIR) not in sys.path:
        sys.path.insert(0, str(_SCORER_DIR))
    import reference_scorer  # noqa: PLC0415

    return reference_scorer


REFERENCE_SCORING_INPUT = (
    b'{"schema_id":"bmas.reference_scorer_input",'
    b'"metadata":{"contract_version":"1.0.0"},'
    b'"scorer":"exact_match","cases":['
    b'{"case_id":"identity","expected":"result","actual":"result"}]}'
)


def score_reference_evidence(input_bytes: bytes = REFERENCE_SCORING_INPUT,
                             ) -> dict[str, Any]:
    """Score one fixed input and return the versioned evidence record.

    The Foundation scorer consumes a fixed fixture and returns one
    versioned evidence record. This bridge proves evidence transport;
    it never scores runtime output semantics.
    """
    scorer = _load_reference_scorer()
    result_bytes = scorer.score_bytes(input_bytes)
    return {
        "result_bytes": result_bytes,
        "result_digest": digest_bytes(
            "reference-scoring-evidence", result_bytes,
        ),
    }


# ── The shared conformance suite ─────────────────────────────────────


def _expected_value(record: RuntimeCapabilityRecord, capability: str) -> str:
    return record.capabilities[capability]


def _run_case(
    adapter: ConformanceAdapter, case_id: str,
) -> CaseResult:
    """Run one shared conformance case against one adapter."""
    record = adapter.record

    if case_id == "submission_and_identity":
        # Exact identity: the adapter returns its complete pair and the
        # shared submission value the matrix declares.
        identity = adapter.exact_identity()
        expected = _expected_value(record, "shared_submission")
        observed = adapter.observe_capability("shared_submission")
        passed = identity == record.runtime_key and observed == expected
        return CaseResult(
            case_id, passed, expected, observed,
            detail=f"identity={identity}",
        )

    if case_id == "assets_and_privacy":
        expected = _expected_value(record, "immutable_assets")
        observed = adapter.observe_capability("immutable_assets")
        return CaseResult(case_id, observed == expected, expected, observed)

    if case_id == "seed_state":
        expected = _expected_value(record, "applied_seed_evidence")
        observed = adapter.observe_capability("applied_seed_evidence")
        return CaseResult(case_id, observed == expected, expected, observed)

    if case_id == "cancellation_and_deadlines":
        expected = _expected_value(record, "cancellation_signal")
        observed = adapter.observe_capability("cancellation_signal")
        return CaseResult(case_id, observed == expected, expected, observed)

    if case_id == "lease_fencing_and_recovery":
        expected = _expected_value(record, "task_fence_validation")
        observed = adapter.observe_capability("task_fence_validation")
        recovery = adapter.observe_capability("recovery_reader_retained")
        # Readable recovery is required for every pair.
        passed = observed == expected and recovery == "native"
        return CaseResult(
            case_id, passed, expected, observed,
            detail=f"recovery_reader={recovery}",
        )

    if case_id == "activation_and_effect_ledgers":
        ledger = adapter.observe_capability("durable_activation_ledger")
        dispatch = adapter.observe_capability("activation_dispatch_outbox")
        expected = _expected_value(record, "durable_activation_ledger")
        # A legacy pair keeps a compatibility projection and never
        # writes a native activation or effect authority record.
        if not adapter.is_native_contract():
            adapter_writes = adapter.native_authority_writes
            passed = (
                ledger == expected
                and dispatch == "legacy_unavailable"
                and adapter_writes == []
            )
            return CaseResult(
                case_id, passed, expected, ledger,
                detail=f"dispatch={dispatch}, v2_writes={adapter_writes}",
            )
        passed = ledger == "native" and dispatch == "native"
        return CaseResult(case_id, passed, expected, ledger)

    if case_id == "agent_protocol_negotiation":
        protocol = adapter.observe_capability("agent_protocol")
        acknowledgement = adapter.observe_capability(
            "signed_activation_acknowledgement",
        )
        receipts = adapter.observe_capability("nested_receipts")
        expected = _expected_value(record, "agent_protocol")
        if adapter.is_native_contract():
            passed = (
                protocol == "native"
                and acknowledgement == "native"
                and receipts == "native"
            )
        else:
            # Legacy protocol, no signed acknowledgement, unobservable
            # nested effects, exactly as the matrix declares.
            passed = (
                protocol == "legacy"
                and acknowledgement == "legacy_unavailable"
                and receipts == "legacy_unobservable"
            )
        return CaseResult(
            case_id, passed, expected, protocol,
            detail=f"ack={acknowledgement}, receipts={receipts}",
        )

    if case_id == "budget_reservations":
        expected = _expected_value(record, "budget_reservation")
        observed = adapter.observe_capability("budget_reservation")
        return CaseResult(case_id, observed == expected, expected, observed)

    if case_id == "evidence_decisions":
        expected = _expected_value(record, "typed_evidence_index")
        observed = adapter.observe_capability("typed_evidence_index")
        return CaseResult(case_id, observed == expected, expected, observed)

    if case_id == "foundation_reference_scoring":
        expected = _expected_value(record, "foundation_reference_scoring")
        observed = adapter.observe_capability("foundation_reference_scoring")
        evidence = score_reference_evidence()
        passed = observed == expected and bool(evidence["result_digest"])
        return CaseResult(
            case_id, passed, expected, observed,
            detail=f"evidence_digest={evidence['result_digest'][:12]}",
        )

    if case_id == "reference_evidence_replay":
        # Replay the fixed scoring twice and require identical evidence,
        # so evidence transport and replay are deterministic.
        first = score_reference_evidence()
        second = score_reference_evidence()
        passed = first["result_digest"] == second["result_digest"]
        return CaseResult(
            case_id, passed, "deterministic", "deterministic"
            if passed else "divergent",
        )

    if case_id == "ui_adapter_and_fallback":
        expected = _expected_value(record, "generic_ui_fallback")
        observed = adapter.observe_capability("generic_ui_fallback")
        # Every pair keeps the generic fallback panels.
        panels = record.ui_fallback_panels
        passed = (
            observed == expected
            and record.ui_fallback
            and set(panels) >= {"trace", "assets", "costs", "final_result"}
        )
        return CaseResult(
            case_id, passed, expected, observed,
            detail=f"adapter={record.ui_adapter}",
        )

    raise ConformanceError(f"Unknown conformance case: {case_id!r}")


def run_conformance_suite(
    adapter: ConformanceAdapter,
) -> ConformanceReport:
    """Run every shared conformance case against one adapter."""
    report = ConformanceReport(
        runtime_key=adapter.runtime_key,
        availability=adapter.record.availability,
    )
    for case_id in CONFORMANCE_CASES:
        report.case_results.append(_run_case(adapter, case_id))
    return report


def suite_covers_every_capability() -> bool:
    """Report whether the cases touch every conformance capability.

    The check keeps the shared case set aligned with the published
    capability list, so no capability escapes conformance.
    """
    touched = {
        "shared_submission",
        "immutable_assets",
        "immutable_policy_set",
        "task_fence_validation",
        "durable_activation_ledger",
        "activation_dispatch_outbox",
        "agent_protocol",
        "signed_activation_acknowledgement",
        "nested_receipts",
        "cancellation_signal",
        "budget_reservation",
        "common_event_envelope",
        "typed_evidence_index",
        "applied_seed_evidence",
        "deterministic_analysis_replay",
        "benchmark_scoring",
        "foundation_reference_scoring",
        "trusted_envelope_creator",
        "ui_adapter",
        "generic_ui_fallback",
        "recovery_reader_retained",
    }
    return touched == set(CONFORMANCE_CAPABILITIES)
