"""Feature gates for the planned shared Foundation writers.

Each gate names one planned durable writer from the Foundation plan.
Every gate stays disabled by default, and the default deployment keeps
every gate disabled. No current runtime consults a gate for its
behavior, so disabling every gate preserves existing behavior.

A future writer must check its gate through ``gate_enabled`` before it
performs any durable write. Gate names are generation-neutral: they
carry no version token and no numeric version suffix.
"""
from __future__ import annotations

import config

PLANNED_WRITER_GATES: tuple[str, ...] = (
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


class UnknownGateError(ValueError):
    """The requested feature gate is not a planned Foundation writer."""


def gate_enabled(name: str) -> bool:
    """Report whether one planned writer gate is enabled.

    Raises UnknownGateError for a name outside the planned writer set,
    so a typo can never read as a disabled gate.
    """
    if name not in PLANNED_WRITER_GATES:
        raise UnknownGateError(
            f"Unknown Foundation writer gate: {name!r}. "
            f"The planned gates are: {', '.join(PLANNED_WRITER_GATES)}."
        )
    configured = getattr(config, "FOUNDATION_GATES", {})
    return bool(configured.get(name, False))


class WriterDisabledError(RuntimeError):
    """A v2 writer ran while its foundation gate stayed disabled."""


def require_writer_gates(*names: str) -> None:
    """Refuse a v2 write while any named gate stays disabled."""
    disabled = [name for name in names if not gate_enabled(name)]
    if disabled:
        raise WriterDisabledError(
            "The foundation gates stay disabled for this writer: "
            + ", ".join(sorted(disabled))
        )


def gate_states() -> dict[str, bool]:
    """Return the current state of every planned writer gate."""
    return {name: gate_enabled(name) for name in PLANNED_WRITER_GATES}
