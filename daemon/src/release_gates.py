"""Foundation Stage 0H: writer enablement and the release gate.

A new shared writer never enables until every required gate passes:
its conformance matrix column, the populated-migration and rollback
gates, and the security gate. A planned runtime pair never becomes a
runnable production choice until its complete conformance column
passes.

This module records which gates passed and answers whether one writer
may enable. It never enables a writer on its own; it reports the gate
state, and the deployment configuration still holds the feature flag
disabled by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.foundation_gates import PLANNED_WRITER_GATES, gate_enabled

if TYPE_CHECKING:
    from conformance_kit import ConformanceReport
    from core.variants import RuntimeKey

# The release gates every writer clears before it enables.
RELEASE_GATES = (
    "conformance",
    "populated_migration",
    "supported_downgrade",
    "security",
)


class ReleaseGateError(ValueError):
    """One release-gate rule failed closed."""


@dataclass
class GateLedger:
    """The recorded pass state of every release gate.

    Each gate records the runtime columns that passed it. A writer
    scoped to one runtime pair enables only when every gate recorded a
    pass for that pair.
    """

    passed: dict[str, set] = field(
        default_factory=lambda: {gate: set() for gate in RELEASE_GATES},
    )

    def record_pass(self, gate: str, runtime_key: RuntimeKey) -> None:
        if gate not in RELEASE_GATES:
            raise ReleaseGateError(f"Unknown release gate: {gate!r}")
        self.passed[gate].add(runtime_key)

    def record_conformance(self, report: ConformanceReport) -> None:
        """Record one conformance report, passing only on full success."""
        if report.passed:
            self.record_pass("conformance", report.runtime_key)

    def gate_passed(self, gate: str, runtime_key: RuntimeKey) -> bool:
        if gate not in RELEASE_GATES:
            raise ReleaseGateError(f"Unknown release gate: {gate!r}")
        return runtime_key in self.passed[gate]

    def missing_gates(self, runtime_key: RuntimeKey) -> list[str]:
        """List every release gate this pair has not yet passed."""
        return [
            gate
            for gate in RELEASE_GATES
            if runtime_key not in self.passed[gate]
        ]

    def all_gates_passed(self, runtime_key: RuntimeKey) -> bool:
        return not self.missing_gates(runtime_key)


def writer_may_enable(
    writer: str,
    runtime_key: RuntimeKey,
    ledger: GateLedger,
) -> bool:
    """Report whether one writer may enable for one runtime pair.

    A writer enables only when its feature gate is on and every release
    gate recorded a pass for the pair. With any gate missing, the
    writer stays disabled and this returns ``False``.
    """
    if writer not in PLANNED_WRITER_GATES:
        raise ReleaseGateError(f"Unknown planned writer: {writer!r}")
    if not ledger.all_gates_passed(runtime_key):
        return False
    return gate_enabled(writer)


def blocked_reason(
    writer: str,
    runtime_key: RuntimeKey,
    ledger: GateLedger,
) -> str | None:
    """Return why a writer stays blocked, or ``None`` when it may enable."""
    if writer not in PLANNED_WRITER_GATES:
        raise ReleaseGateError(f"Unknown planned writer: {writer!r}")
    missing = ledger.missing_gates(runtime_key)
    if missing:
        return f"gates_not_passed:{','.join(missing)}"
    if not gate_enabled(writer):
        return "feature_gate_disabled"
    return None
