"""The Classic reason table.

Every terminal reason of the native pair maps to one Foundation outcome
class and one benchmark class. Work package 5A registers the table
through the reason registry and the benchmark mapping
``outcome-mapping-classic-2``. The compiled specification records the
reasons a run can end with, so a historical run never resolves a reason
from a newer table.
"""
from __future__ import annotations

from core.run_contracts import OutcomeClass

CLASSIC_REASON_TABLE_VERSION = "classic-reasons/1"
PAPER_ALIGNED_CONTEXT_LIMIT_REASON = "paper_aligned_context_limit_exceeded"

# Reason -> Foundation outcome class. The benchmark mapping rows follow
# the shared reason shape of ``benchmarks.outcome_mappings``.
CLASSIC_REASON_CLASSES: dict[str, OutcomeClass] = {
    "completed": OutcomeClass.SUCCESS,
    PAPER_ALIGNED_CONTEXT_LIMIT_REASON: OutcomeClass.SUBSTANTIVE_FAILURE,
    "no_valid_candidate": OutcomeClass.SUBSTANTIVE_FAILURE,
    "budget_exhausted": OutcomeClass.SUBSTANTIVE_FAILURE,
    "deadline_exceeded": OutcomeClass.SUBSTANTIVE_FAILURE,
    "stalled": OutcomeClass.SUBSTANTIVE_FAILURE,
    "no_available_agents": OutcomeClass.SUBSTANTIVE_FAILURE,
    "cancelled": OutcomeClass.CANCELLATION,
    "integrity_failure": OutcomeClass.INFRASTRUCTURE_FAILURE,
}

_BENCHMARK_CLASS = {
    OutcomeClass.SUCCESS: "success",
    OutcomeClass.SUBSTANTIVE_FAILURE: "substantive_failure",
    OutcomeClass.INFRASTRUCTURE_FAILURE: "infrastructure_failure",
    OutcomeClass.CANCELLATION: "cancelled",
}

CLASSIC_TASK_REASONS: dict[str, dict[str, str]] = {
    reason: {
        "benchmark_class": _BENCHMARK_CLASS[outcome_class],
        "retry_rule": "prohibited" if outcome_class is not OutcomeClass.INFRASTRUCTURE_FAILURE else "predeclared_only",
        "missingness": "observed" if outcome_class is not OutcomeClass.INFRASTRUCTURE_FAILURE else "verified_only",
        "denominator": "unconditional",
    }
    for reason, outcome_class in CLASSIC_REASON_CLASSES.items()
}

# The reasons only the paper-aligned profile can end with.
PAPER_ALIGNED_ONLY_REASONS: tuple[str, ...] = (PAPER_ALIGNED_CONTEXT_LIMIT_REASON,)


def terminal_reasons_for(fidelity_profile_id: str) -> list[str]:
    """The reasons one fidelity profile can end a run with, in table order."""
    return [
        reason for reason in CLASSIC_REASON_CLASSES
        if fidelity_profile_id == "paper_aligned" or reason not in PAPER_ALIGNED_ONLY_REASONS
    ]
