"""The Classic reason table and the terminal outcome of a native run.

Every terminal reason of the native pair maps to one Foundation outcome
class and one benchmark class. The reason registry publishes the table
under the reason table version, the benchmark mapping
``outcome-mapping-classic-2`` carries the same table, and the compiled
specification records the reasons a run can end with, so a historical
run never resolves a reason from a newer table.

The terminal outcome writer commits the one ``terminal_outcome`` journal
record of a native run through the unit of work. The journal derives the
run state from the common class, and the journal refuses a second
outcome for the same run.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import database as db
import runtime_journal as journal
from core.digest_profile import plain_json
from core.run_contracts import (
    OutcomeClass,
    ReasonBinding,
    ReasonRegistry,
    RuntimeOutcome,
)
from core.variants import RuntimeKey

if TYPE_CHECKING:
    from core.run_context import RunContext

CLASSIC_REASON_TABLE_VERSION = "classic-reasons/1"
PAPER_ALIGNED_CONTEXT_LIMIT_REASON = "paper_aligned_context_limit_exceeded"
NATIVE_RUNTIME_KEY = RuntimeKey("classic", "2")
TERMINAL_OUTCOME_PRODUCER = "classic-runtime"

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

# The benchmark rules per outcome class. A substantive failure counts
# zero success inside the unconditional denominator. A cancellation
# leaves missing work inside the same denominator. An integrity failure
# excludes only under a predeclared exclusion category, which the study
# admission checks against the excludable rows of this table.
_BENCHMARK_RULES = {
    OutcomeClass.SUCCESS: {
        "retry_rule": "prohibited", "missingness": "observed", "denominator": "unconditional",
    },
    OutcomeClass.SUBSTANTIVE_FAILURE: {
        "retry_rule": "prohibited", "missingness": "observed", "denominator": "unconditional",
    },
    OutcomeClass.CANCELLATION: {
        "retry_rule": "allowed", "missingness": "missing_work", "denominator": "unconditional",
    },
    OutcomeClass.INFRASTRUCTURE_FAILURE: {
        "retry_rule": "allowed", "missingness": "excludable", "denominator": "excludable",
    },
}

CLASSIC_TASK_REASONS: dict[str, dict[str, str]] = {
    reason: {
        "benchmark_class": _BENCHMARK_CLASS[outcome_class],
        **_BENCHMARK_RULES[outcome_class],
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


# ── The reason registry ───────────────────────────────────────────────


def classic_reason_bindings() -> dict[str, ReasonBinding]:
    """The trusted binding of every Classic reason.

    An integrity failure is the one retryable Classic reason, because
    a verified infrastructure fault leaves the task itself intact. The
    registry adds the shared pre-execution reasons on registration.
    """
    return {
        reason: ReasonBinding(
            outcome_class,
            retryable=outcome_class is OutcomeClass.INFRASTRUCTURE_FAILURE,
        )
        for reason, outcome_class in CLASSIC_REASON_CLASSES.items()
    }


def register_classic_reasons(
    registry: ReasonRegistry, runtime_key: RuntimeKey = NATIVE_RUNTIME_KEY,
) -> ReasonRegistry:
    """Publish the Classic reason table for one runtime pair."""
    registry.register_mapping(
        runtime_key, classic_reason_bindings(), mapping_version=CLASSIC_REASON_TABLE_VERSION,
    )
    return registry


_registry: ReasonRegistry | None = None


def classic_reason_registry() -> ReasonRegistry:
    """The process-wide registry that holds the native pair's reasons."""
    global _registry
    if _registry is None:
        _registry = register_classic_reasons(ReasonRegistry())
    return _registry


def classic_benchmark_reasons() -> dict[str, dict[str, str]]:
    """The benchmark reason table of the native pair.

    The table holds every Classic reason. It keeps the shared task
    reasons the daemon task layer still derives from the legacy
    lifecycle, so an attempt of the native pair resolves under one
    mapping until the qualification work package binds the attempt
    to the terminal outcome record. A Classic row wins over a shared
    row with the same name.
    """
    from benchmarks.outcome_mappings import SHARED_TASK_REASONS

    return {**SHARED_TASK_REASONS, **CLASSIC_TASK_REASONS}


# ── Reason derivation ─────────────────────────────────────────────────

# The engine's forced-stop reasons and the Classic reason each one
# names when the run ends without an answer.
_LIMIT_REASONS = {
    "budget": "budget_exhausted",
    "duration": "deadline_exceeded",
    "stalled": "stalled",
    "no_available_agents": "no_available_agents",
}

# The runtime phases and the shared pre-execution reason of a failure
# inside each one.
PHASE_FAILURE_REASONS = {
    "start": "initialization_failure",
    "genesis": "genesis_failure",
}


def reason_for_result(result: dict[str, Any]) -> str:
    """The Classic reason of one finished coordination result.

    A run that produced an answer completed. The verification status
    and the answer source travel in the outcome detail, so the later
    candidate lifecycle can refine the success rule without changing
    this record. A run without an answer names the limit that stopped
    it, or ``no_valid_candidate`` when no limit did.
    """
    answer = str(result.get("answer") or "").strip()
    if answer:
        return "completed"
    terminated_by = str(result.get("terminated_by") or "")
    return _LIMIT_REASONS.get(terminated_by, "no_valid_candidate")


def reason_for_exception(exc: BaseException, *, phase: str) -> str | None:
    """The Classic reason of one failed run, or None for a recoverable stop.

    A lease or fence loss is not terminal: another host resumes the
    run. A configuration block is not terminal either: the operator
    resumes the task after the repair. Every other failure ends the
    run with one registered reason.
    """
    from core.gateway import LeaseLostError
    from core.variants import VariantConfigurationError
    from core.variants.classic.projection import (
        ClassicFenceError,
        ClassicIntegrityError,
        RunCancelledError,
        RunDeadlineError,
    )

    if isinstance(exc, (LeaseLostError, ClassicFenceError, db.LeaseFenceError, journal.JournalFenceError)):
        return None
    if isinstance(exc, VariantConfigurationError):
        return None
    if isinstance(exc, (asyncio.CancelledError, RunCancelledError)):
        return "cancelled"
    if isinstance(exc, RuntimeError) and "abort" in str(exc).lower():
        return "cancelled"
    if isinstance(exc, RunDeadlineError):
        return "deadline_exceeded"
    if isinstance(exc, (ClassicIntegrityError, journal.JournalIntegrityError, journal.SnapshotVerificationError)):
        return "integrity_failure"
    return PHASE_FAILURE_REASONS.get(phase, "no_valid_candidate")


# ── The terminal outcome writer ───────────────────────────────────────


async def terminal_outcome_record(run_id: str) -> journal.JournalRecord | None:
    """The one terminal outcome record of a run, when it exists."""
    for record in await journal.read_journal(run_id=run_id):
        if record.operation_type == "terminal_outcome":
            return record
    return None


async def commit_terminal_outcome(
    *,
    context: RunContext,
    reason_code: str,
    tenant_id: str = "tenant-default",
    final_references: tuple[str, ...] = (),
    resource_references: tuple[str, ...] = (),
    detail: dict[str, Any] | None = None,
    registry: ReasonRegistry | None = None,
) -> journal.JournalRecord:
    """Commit the one terminal outcome of a native run.

    The registry supplies the trusted common class from the reason
    code. The outcome seals the run's journal at its current chain
    head, so the record names the final cursor of the coordination it
    ends. A run that already holds its outcome returns that record and
    writes nothing.
    """
    registry = registry or classic_reason_registry()
    binding = registry.binding_for(context.runtime_key, reason_code)
    existing = await terminal_outcome_record(context.run_id)
    if existing is not None:
        return existing
    run_row = await db.get_run(context.run_id)
    if run_row is None:
        raise journal.JournalError(f"Unknown run: {context.run_id}")
    outcome = RuntimeOutcome(
        outcome_id=f"outcome-{context.run_id}",
        run_id=context.run_id,
        tenant_id=tenant_id,
        runtime_key=context.runtime_key,
        common_class=binding.common_class,
        reason_code=reason_code,
        mapping_version=registry.mapping_version_for(context.runtime_key),
        final_references=tuple(final_references),
        resource_references=tuple(resource_references),
        terminal_journal_cursor=int(run_row["journal_cursor"]),
    )
    payload = {
        "outcome_id": outcome.outcome_id,
        "common_class": outcome.common_class.value,
        "reason_code": outcome.reason_code,
        "mapping_version": outcome.mapping_version,
        "outcome_digest": outcome.digest(),
        "final_references": list(outcome.final_references),
        "resource_references": list(outcome.resource_references),
        "terminal_journal_cursor": outcome.terminal_journal_cursor,
        "tenant_id": outcome.tenant_id,
        "runtime_key": outcome.runtime_key.to_dict(),
        "policy_set_digest": context.policy_set_digest,
        "specification_digest": context.effective_spec_digest,
        "retryable": binding.retryable,
        "terminal_detail": plain_json(detail or {}),
    }
    operation = journal.JournalOperation(
        operation_type="terminal_outcome",
        task_id=context.task_id,
        run_id=context.run_id,
        runtime_id=context.runtime_key.runtime_id,
        runtime_contract_version=context.runtime_key.runtime_contract_version,
        payload=payload,
        idempotency_token=f"terminal-{context.run_id}",
        producer=TERMINAL_OUTCOME_PRODUCER,
        authority_type="runtime",
        tenant_id=tenant_id,
        task_fence=context.task_fence,
    )
    return await journal.commit_operation(operation)
