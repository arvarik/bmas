"""Foundation Stage 0H: published runtime capabilities and negotiation.

The capability API publishes one record per runtime pair. Each record
states the exact supported versions, the availability state, and every
conformance capability, and it marks each unsupported native
capability explicitly instead of implying a native guarantee.

Stage 0 marks the runtime pairs that need a not-yet-built native
implementation as ``planned``. A planned pair is available for
interface development but never a runnable production choice. A pair
becomes ``qualified`` only after its conformance column passes.

The client selects an adapter by the exact runtime pair. An unknown
pair uses the generic user-interface fallback, which shows the common
trace, assets, costs, and final result projections.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from core.variants import (
    RUNTIME_AVAILABILITY_STATES,
    RuntimeKey,
)

# The values a legacy compatibility column declares instead of a
# native guarantee. Every value is explicit, so no export or view
# can imply a capability the runtime does not have.
COMPATIBILITY_VALUES = (
    "native",
    "compatibility_adapter",
    "compatibility_projection",
    "compatibility_record",
    "legacy",
    "legacy_unavailable",
    "legacy_unobservable",
    "advisory_legacy",
    "recorded_only",
    "unavailable",
)

# The conformance capabilities the shared suite exercises for every
# pair. Each capability maps to one declared matrix value per pair.
CONFORMANCE_CAPABILITIES = (
    "shared_submission",
    "immutable_assets",
    "immutable_policy_set",
    "task_fence_validation",
    "durable_activation_ledger",
    "activation_dispatch_outbox",
    "agent_protocol",
    "signed_activation_acknowledgement",
    "nested_receipts",
    "trusted_envelope_creator",
    "cancellation_signal",
    "budget_reservation",
    "common_event_envelope",
    "typed_evidence_index",
    "applied_seed_evidence",
    "deterministic_analysis_replay",
    "benchmark_scoring",
    "foundation_reference_scoring",
    "ui_adapter",
    "generic_ui_fallback",
    "recovery_reader_retained",
)

# The generic user-interface fallback capability set. An unknown
# adapter renders these common projections.
GENERIC_UI_FALLBACK_PANELS = ("trace", "assets", "costs", "final_result")


class CapabilityPublicationError(ValueError):
    """One capability publication rule failed closed."""


@dataclass(frozen=True)
class RuntimeCapabilityRecord:
    """The published capability record of one runtime pair."""

    runtime_key: RuntimeKey
    canonical_label: str
    historical_label: str | None
    availability: str
    schema_versions: dict[str, str]
    capabilities: dict[str, str]
    agent_protocol_version: str
    agent_receipt_version: str | None
    effect_schema_version: str | None
    supports_seed_state: bool
    supports_assets: bool
    supports_cancellation: bool
    supports_recovery: bool
    supports_evidence: bool
    supports_budget: bool
    nested_effect_receipts: bool
    provider_qualification: str
    benchmark_qualification: str
    ui_adapter: str
    ui_fallback: bool = True
    ui_fallback_panels: tuple[str, ...] = GENERIC_UI_FALLBACK_PANELS

    def __post_init__(self) -> None:
        if self.availability not in RUNTIME_AVAILABILITY_STATES:
            raise CapabilityPublicationError(
                f"Unknown availability state: {self.availability!r}"
            )
        missing = [
            capability
            for capability in CONFORMANCE_CAPABILITIES
            if capability not in self.capabilities
        ]
        if missing:
            raise CapabilityPublicationError(
                f"The record for {self.runtime_key} misses capability "
                f"values: {missing}"
            )
        for capability, value in self.capabilities.items():
            if value not in COMPATIBILITY_VALUES:
                raise CapabilityPublicationError(
                    f"{self.runtime_key} declares an unknown value "
                    f"{value!r} for {capability}"
                )
        if not self.ui_fallback:
            raise CapabilityPublicationError(
                "Every runtime pair keeps the generic UI fallback"
            )

    def unsupported_native_capabilities(self) -> list[str]:
        """List each capability this pair does not support natively."""
        return sorted(
            capability
            for capability, value in self.capabilities.items()
            if value != "native"
        )

    def is_runnable_choice(self) -> bool:
        """Report whether production admission can run this pair.

        Production admission accepts only a qualified pair. A planned
        pair is available for interface development, never a runnable
        choice.
        """
        return self.availability == "qualified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_key": self.runtime_key.to_dict(),
            "canonical_label": self.canonical_label,
            "historical_label": self.historical_label,
            "availability": self.availability,
            "schema_versions": dict(self.schema_versions),
            "capabilities": dict(self.capabilities),
            "agent_protocol_version": self.agent_protocol_version,
            "agent_receipt_version": self.agent_receipt_version,
            "effect_schema_version": self.effect_schema_version,
            "supports_seed_state": self.supports_seed_state,
            "supports_assets": self.supports_assets,
            "supports_cancellation": self.supports_cancellation,
            "supports_recovery": self.supports_recovery,
            "supports_evidence": self.supports_evidence,
            "supports_budget": self.supports_budget,
            "nested_effect_receipts": self.nested_effect_receipts,
            "provider_qualification": self.provider_qualification,
            "benchmark_qualification": self.benchmark_qualification,
            "ui_adapter": self.ui_adapter,
            "ui_fallback": self.ui_fallback,
            "ui_fallback_panels": list(self.ui_fallback_panels),
            "unsupported_native_capabilities": (
                self.unsupported_native_capabilities()
            ),
            "runnable_choice": self.is_runnable_choice(),
        }


_NATIVE_SCHEMA_VERSIONS = {
    "runtime_spec_schema_version": "1",
    "runtime_state_schema_version": "1",
    "checkpoint_schema_version": "1",
    "activation_schema_version": "1",
    "activation_dispatch_schema_version": "1",
    "activation_acknowledgement_schema_version": "1",
    "digest_profile_version": "1",
    "runtime_outcome_schema_version": "1",
    "post_terminal_invalidation_schema_version": "1",
    "agent_protocol_version": "2",
    "agent_receipt_schema_version": "1",
    "effect_schema_version": "1",
    "trace_schema_version": "1",
    "evidence_schema_version": "1",
    "asset_manifest_schema_version": "1",
    "policy_set_schema_version": "1",
    "capability_document_version": "1",
    "database_schema_version": "18",
}


def _legacy_capabilities() -> dict[str, str]:
    """The declared compatibility values for a legacy runtime column."""
    return {
        "shared_submission": "compatibility_adapter",
        "immutable_assets": "compatibility_adapter",
        "immutable_policy_set": "compatibility_record",
        "task_fence_validation": "compatibility_adapter",
        # The host admits a legacy task into one Foundation run and
        # dispatches through signed grants on the runtime's behalf when
        # the endpoint qualifies. The runtime itself stays legacy: it
        # authors no native authority record. These values name that
        # host-side compatibility adapter.
        "durable_activation_ledger": "compatibility_adapter",
        "activation_dispatch_outbox": "compatibility_adapter",
        "agent_protocol": "compatibility_adapter",
        "signed_activation_acknowledgement": "compatibility_adapter",
        "nested_receipts": "compatibility_adapter",
        "trusted_envelope_creator": "compatibility_adapter",
        "cancellation_signal": "legacy",
        "budget_reservation": "advisory_legacy",
        "common_event_envelope": "compatibility_projection",
        "typed_evidence_index": "legacy",
        "applied_seed_evidence": "recorded_only",
        "deterministic_analysis_replay": "compatibility_projection",
        "benchmark_scoring": "compatibility_adapter",
        "foundation_reference_scoring": "compatibility_projection",
        "ui_adapter": "legacy",
        "generic_ui_fallback": "native",
        "recovery_reader_retained": "native",
    }


def _native_capabilities() -> dict[str, str]:
    """The declared native values for a fully qualified native column."""
    return {capability: "native" for capability in CONFORMANCE_CAPABILITIES}


# The published capability records for the Foundation runtime pairs.
# The deterministic reference adapter and the three legacy compatibility
# adapters qualify in Stage 0. The native native runtimes stay planned
# until their conformance columns pass.
def build_records() -> dict[RuntimeKey, RuntimeCapabilityRecord]:
    """Build the published capability record for every runtime pair."""
    records: dict[RuntimeKey, RuntimeCapabilityRecord] = {}

    def add(record: RuntimeCapabilityRecord) -> None:
        records[record.runtime_key] = record

    # The deterministic reference adapter: native native, qualified.
    add(RuntimeCapabilityRecord(
        runtime_key=RuntimeKey("reference", "1"),
        canonical_label="Deterministic reference",
        historical_label=None,
        availability="qualified",
        schema_versions=dict(_NATIVE_SCHEMA_VERSIONS),
        capabilities=_native_capabilities(),
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
        ui_adapter="reference",
    ))

    # The Classic legacy compatibility adapter: qualified in Stage 0.
    classic_caps = _legacy_capabilities()
    add(RuntimeCapabilityRecord(
        runtime_key=RuntimeKey("classic", "1"),
        canonical_label="Classic v1",
        historical_label="Classic v1",
        availability="qualified",
        schema_versions=_legacy_schema_versions(),
        capabilities=classic_caps,
        agent_protocol_version="1",
        agent_receipt_version=None,
        effect_schema_version=None,
        supports_seed_state=True,
        supports_assets=True,
        supports_cancellation=True,
        supports_recovery=True,
        supports_evidence=True,
        supports_budget=True,
        nested_effect_receipts=False,
        provider_qualification="compatibility",
        benchmark_qualification="compatibility",
        ui_adapter="classic_legacy",
    ))

    # The PatchBoard legacy compatibility adapter: qualified in Stage 0.
    # It has no typed evidence index.
    patchboard_caps = _legacy_capabilities()
    patchboard_caps["typed_evidence_index"] = "unavailable"
    add(RuntimeCapabilityRecord(
        runtime_key=RuntimeKey("patchboard", "1"),
        canonical_label="Parallel synthesis",
        historical_label="Patchboard v1",
        availability="qualified",
        schema_versions=_legacy_schema_versions(),
        capabilities=patchboard_caps,
        agent_protocol_version="1",
        agent_receipt_version=None,
        effect_schema_version=None,
        supports_seed_state=True,
        supports_assets=True,
        supports_cancellation=True,
        supports_recovery=True,
        supports_evidence=False,
        supports_budget=True,
        nested_effect_receipts=False,
        provider_qualification="compatibility",
        benchmark_qualification="compatibility",
        ui_adapter="parallel_synthesis",
    ))

    # The Stigmergic legacy compatibility adapter: qualified in Stage 0.
    # It also has no typed evidence index.
    stigmergic_caps = _legacy_capabilities()
    stigmergic_caps["typed_evidence_index"] = "unavailable"
    add(RuntimeCapabilityRecord(
        runtime_key=RuntimeKey("stigmergic", "1"),
        canonical_label="Ordered revision",
        historical_label="Stigmergic v1",
        availability="qualified",
        schema_versions=_legacy_schema_versions(),
        capabilities=stigmergic_caps,
        agent_protocol_version="1",
        agent_receipt_version=None,
        effect_schema_version=None,
        supports_seed_state=True,
        supports_assets=True,
        supports_cancellation=True,
        supports_recovery=True,
        supports_evidence=False,
        supports_budget=True,
        nested_effect_receipts=False,
        provider_qualification="compatibility",
        benchmark_qualification="compatibility",
        ui_adapter="stigmergic_legacy",
    ))

    # The native Classic and PatchBoard records stay planned until their
    # conformance columns pass. Available for interface development.
    add(RuntimeCapabilityRecord(
        runtime_key=RuntimeKey("classic", "2"),
        canonical_label="Classic v2",
        historical_label="Classic v1",
        availability="planned",
        schema_versions=dict(_NATIVE_SCHEMA_VERSIONS),
        capabilities=_native_capabilities(),
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
        provider_qualification="planned",
        benchmark_qualification="planned",
        ui_adapter="classic_native",
    ))
    add(RuntimeCapabilityRecord(
        runtime_key=RuntimeKey("patchboard", "2"),
        canonical_label="PatchBoard v2",
        historical_label=None,
        availability="planned",
        schema_versions=dict(_NATIVE_SCHEMA_VERSIONS),
        capabilities=_native_capabilities(),
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
        provider_qualification="planned",
        benchmark_qualification="planned",
        ui_adapter="state_lab",
    ))
    return records


def _legacy_schema_versions() -> dict[str, str]:
    versions = dict(_NATIVE_SCHEMA_VERSIONS)
    versions["agent_protocol_version"] = "1"
    versions["agent_receipt_schema_version"] = "0"
    versions["effect_schema_version"] = "0"
    versions["activation_dispatch_schema_version"] = "0"
    versions["activation_acknowledgement_schema_version"] = "0"
    return versions


@dataclass(frozen=True)
class CapabilityDirectory:
    """The published directory of every runtime capability record."""

    records: dict[RuntimeKey, RuntimeCapabilityRecord] = field(
        default_factory=build_records,
    )

    def get(self, key: RuntimeKey) -> RuntimeCapabilityRecord:
        record = self.records.get(key)
        if record is None:
            raise CapabilityPublicationError(f"Unknown runtime pair: {key}")
        return record

    def runnable_choices(self) -> list[RuntimeKey]:
        """List every pair production admission can run."""
        return sorted(
            key
            for key, record in self.records.items()
            if record.is_runnable_choice()
        )

    def planned_pairs(self) -> list[RuntimeKey]:
        """List every planned pair, available for interface development."""
        return sorted(
            key
            for key, record in self.records.items()
            if record.availability == "planned"
        )

    def select_ui_adapter(self, key: RuntimeKey) -> str:
        """Select the UI adapter for one pair or the generic fallback.

        The client never renders a legacy state with a native adapter.
        An unknown pair uses the generic trace and artifact view.
        """
        record = self.records.get(key)
        if record is None:
            return "generic_fallback"
        return record.ui_adapter

    def qualify(self, key: RuntimeKey) -> RuntimeCapabilityRecord:
        """Promote one planned pair to qualified after its column passes."""
        record = self.get(key)
        if record.availability == "qualified":
            return record
        if record.availability != "planned":
            raise CapabilityPublicationError(
                f"Only a planned pair qualifies; {key} is "
                f"{record.availability}"
            )
        promoted = dataclasses.replace(record, availability="qualified")
        self.records[key] = promoted
        return promoted
