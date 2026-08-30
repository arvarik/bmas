"""Foundation Stage 0B: stable task, run, admission, and outcome contracts.

This module defines the version taxonomy and routing contracts:

- ``VersionSet`` keeps every schema and protocol version as a separate
  metadata field. No component infers one version from another.
- A task is the durable user objective and ownership container. A run
  is one admitted execution under one immutable runtime pair.
- ``RuntimeAdmission`` is the partial admission record. This stage only
  compiles and validates the routing fields. It persists nothing and it
  enqueues nothing; the later admission transaction stage adds both.
- ``RuntimeOutcome`` is the only terminal run record, and only the host
  creates it through the outcome ledger.
- ``PostTerminalInvalidation`` records a material fact that changes
  after an outcome commits. It changes derived projections only. It
  never rewrites a terminal outcome.

The ledgers here are in-memory reference authorities. The storage
authority stage binds the same contracts to the durable journal.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

from core.variants import (
    RuntimeKey,
    require_admissible_runtime,
    require_runtime,
)

_VERSION_TOKEN = re.compile(r"(^|[._-])[vV][0-9]+([._-]|$)")

RUN_CONTRACT_SCHEMA_VERSION = "1"


class RunContractError(ValueError):
    """A run, admission, outcome, or invalidation contract was violated."""


class VersionFieldError(RunContractError):
    """One named version field is missing or malformed."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field_name = field_name


class MissingReaderError(RunContractError):
    """A required reader identifier has no available reader."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        super().__init__(
            f"Required readers are unavailable: {sorted(missing)}. "
            "Admission rejects a missing reader without fallback."
        )
        self.missing = tuple(sorted(missing))


class TerminalRunError(RunContractError):
    """A terminal run cannot reopen, change state, or change its pair."""


class InvalidRunTransitionError(RunContractError):
    """The requested run state transition is not registered."""


class AdmissionBindingError(RunContractError):
    """A run admission binding rule was violated."""


class UnregisteredReasonError(RunContractError):
    """The terminal reason code is not in the runtime reason mapping."""


class OutcomeConflictError(RunContractError):
    """A run already holds its one immutable terminal outcome."""


class InvalidationConflictError(RunContractError):
    """The idempotency key was reused with a different request."""


class InvalidationRejectedError(RunContractError):
    """The invalidation request failed a validation gate before mutation."""


class InvalidationExecutionError(RunContractError):
    """The invalidation service never executes a model or a tool."""


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 digest of one canonical JSON encoding."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _require_exact_string(field_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise VersionFieldError(field_name, "must be a string")
    if not value or value != value.strip():
        raise VersionFieldError(
            field_name, "must be one exact non-empty string",
        )
    return value


@dataclass(frozen=True)
class VersionSet:
    """Hold every schema and protocol version as a separate field.

    The runtime pair itself lives in ``RuntimeKey``. Every other
    contract version is an independent string field, and the database
    schema version is an integer. No reader infers one field from
    another field.
    """

    runtime_spec_schema_version: str
    runtime_state_schema_version: str
    checkpoint_schema_version: str
    activation_schema_version: str
    activation_dispatch_schema_version: str
    activation_acknowledgement_schema_version: str
    digest_profile_version: str
    runtime_outcome_schema_version: str
    post_terminal_invalidation_schema_version: str
    agent_protocol_version: str
    agent_receipt_schema_version: str
    effect_schema_version: str
    trace_schema_version: str
    evidence_schema_version: str
    asset_manifest_schema_version: str
    policy_set_schema_version: str
    capability_document_version: str
    database_schema_version: int

    def __post_init__(self) -> None:
        for spec in fields(self):
            value = getattr(self, spec.name)
            if spec.name == "database_schema_version":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise VersionFieldError(spec.name, "must be an integer")
                if value < 1:
                    raise VersionFieldError(spec.name, "must be positive")
                continue
            _require_exact_string(spec.name, value)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON shape of this version set."""
        return {spec.name: getattr(self, spec.name) for spec in fields(self)}


class RunState(StrEnum):
    """The registered run states. Three states are terminal."""

    ADMITTED = "admitted"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
)

# The registered transitions. A transition into a terminal state exists
# only through the outcome ledger, so this map holds no terminal target.
_RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.ADMITTED: frozenset({RunState.QUEUED}),
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.CANCELLING}),
    RunState.RUNNING: frozenset(
        {RunState.PAUSED, RunState.NEEDS_ATTENTION, RunState.CANCELLING}
    ),
    RunState.PAUSED: frozenset({RunState.RUNNING, RunState.CANCELLING}),
    RunState.NEEDS_ATTENTION: frozenset(
        {RunState.RUNNING, RunState.CANCELLING}
    ),
    RunState.CANCELLING: frozenset(),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


def validate_run_transition(current: RunState, target: RunState) -> None:
    """Validate one run state transition or fail closed."""
    if current in TERMINAL_RUN_STATES:
        raise TerminalRunError(
            f"A {current.value} run is terminal and never reopens"
        )
    if target in TERMINAL_RUN_STATES:
        raise InvalidRunTransitionError(
            "Only the host outcome ledger can enter a terminal state"
        )
    if target not in _RUN_TRANSITIONS[current]:
        raise InvalidRunTransitionError(
            f"No registered transition from {current.value} to {target.value}"
        )


class RunLineageReason(StrEnum):
    """The typed reasons that link a successor run to its parent."""

    RESTART = "restart"
    ACTIVATION_RETRY = "activation_retry"
    RERUN = "rerun"
    REROUTE = "reroute"
    BENCHMARK_REPETITION = "benchmark_repetition"


@dataclass
class RunRecord:
    """One admitted execution under one immutable runtime pair.

    The runtime pair never changes inside an existing run. A reroute
    creates a successor run instead.
    """

    run_id: str
    task_id: str
    tenant_id: str
    runtime_key: RuntimeKey
    state: RunState = RunState.ADMITTED
    attempt: int = 0
    parent_run_id: str | None = None
    lineage_reason: RunLineageReason | None = None
    rerouted_from_run_id: str | None = None
    repetition_slot: int | None = None
    invalidation_id: str | None = None


@dataclass(frozen=True)
class RuntimeAdmission:
    """The partial routing identity of one candidate run.

    This record binds the exact runtime pair and the complete version
    set before queue admission. The later stages add the immutable
    asset manifest, the policy set, the qualifications, the initial
    reservation, and the admission transaction.
    """

    admission_id: str
    task_id: str
    run_id: str
    runtime_key: RuntimeKey
    version_set: VersionSet
    specification_digest: str
    capability_document_digest: str
    prompt_profile_digest: str
    role_profile_digest: str
    seed_policy: str
    requested_seed: int | None
    required_reader_ids: tuple[str, ...]
    interface_adapter_id: str
    metadata_schema_version: str = RUN_CONTRACT_SCHEMA_VERSION

    def digest(self) -> str:
        """Return the canonical digest of this admission."""
        record = {
            "admission_id": self.admission_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "runtime_key": self.runtime_key.to_dict(),
            "version_set": self.version_set.to_dict(),
            "specification_digest": self.specification_digest,
            "capability_document_digest": self.capability_document_digest,
            "prompt_profile_digest": self.prompt_profile_digest,
            "role_profile_digest": self.role_profile_digest,
            "seed_policy": self.seed_policy,
            "requested_seed": self.requested_seed,
            "required_reader_ids": list(self.required_reader_ids),
            "interface_adapter_id": self.interface_adapter_id,
            "metadata_schema_version": self.metadata_schema_version,
        }
        return canonical_digest(record)


SEED_POLICIES = ("recorded", "applied", "none")


def compile_run_admission(
    *,
    admission_id: str,
    task_id: str,
    run_id: str,
    runtime_key: RuntimeKey,
    version_set: VersionSet,
    specification_digest: str,
    capability_document_digest: str,
    prompt_profile_digest: str,
    role_profile_digest: str,
    seed_policy: str,
    requested_seed: int | None,
    required_reader_ids: tuple[str, ...],
    interface_adapter_id: str,
    available_reader_ids: frozenset[str],
    require_qualified: bool = True,
) -> RuntimeAdmission:
    """Compile and validate one partial run admission.

    The compiler resolves the exact runtime pair through the registry.
    Production admission accepts only a qualified pair. Every required
    reader must be available; a missing reader fails the compilation
    without fallback. A failed compilation raises and leaves the task
    open, because nothing was persisted or enqueued.
    """
    if require_qualified:
        require_admissible_runtime(runtime_key)
    else:
        require_runtime(runtime_key)

    for name, value in (
        ("admission_id", admission_id),
        ("task_id", task_id),
        ("run_id", run_id),
        ("specification_digest", specification_digest),
        ("capability_document_digest", capability_document_digest),
        ("prompt_profile_digest", prompt_profile_digest),
        ("role_profile_digest", role_profile_digest),
        ("interface_adapter_id", interface_adapter_id),
    ):
        _require_exact_string(name, value)

    if seed_policy not in SEED_POLICIES:
        raise VersionFieldError(
            "seed_policy", f"must be one of {SEED_POLICIES}",
        )
    if requested_seed is not None and not isinstance(requested_seed, int):
        raise VersionFieldError("requested_seed", "must be an integer or None")

    missing = tuple(
        reader for reader in required_reader_ids
        if reader not in available_reader_ids
    )
    if missing:
        raise MissingReaderError(missing)

    return RuntimeAdmission(
        admission_id=admission_id,
        task_id=task_id,
        run_id=run_id,
        runtime_key=runtime_key,
        version_set=version_set,
        specification_digest=specification_digest,
        capability_document_digest=capability_document_digest,
        prompt_profile_digest=prompt_profile_digest,
        role_profile_digest=role_profile_digest,
        seed_policy=seed_policy,
        requested_seed=requested_seed,
        required_reader_ids=tuple(required_reader_ids),
        interface_adapter_id=interface_adapter_id,
    )


class RunLedger:
    """In-memory authority for task and run identity.

    A task can own several runs. Each run binds one immutable admission
    and one immutable runtime pair. The ledger enforces the operation
    identities: restart, activation retry, rerun, reroute, and
    benchmark repetition.
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._admissions: dict[str, RuntimeAdmission] = {}

    def create_run(
        self,
        *,
        task_id: str,
        tenant_id: str,
        runtime_key: RuntimeKey,
        parent_run_id: str | None = None,
        lineage_reason: RunLineageReason | None = None,
        rerouted_from_run_id: str | None = None,
        repetition_slot: int | None = None,
        invalidation_id: str | None = None,
    ) -> RunRecord:
        """Admit one new run for a task."""
        if (parent_run_id is None) != (lineage_reason is None):
            raise RunContractError(
                "A successor run stores its parent and a typed lineage reason"
            )
        if parent_run_id is not None and parent_run_id not in self._runs:
            raise RunContractError(f"Unknown parent run: {parent_run_id}")
        run = RunRecord(
            run_id=f"run-{uuid.uuid4()}",
            task_id=task_id,
            tenant_id=tenant_id,
            runtime_key=runtime_key,
            parent_run_id=parent_run_id,
            lineage_reason=lineage_reason,
            rerouted_from_run_id=rerouted_from_run_id,
            repetition_slot=repetition_slot,
            invalidation_id=invalidation_id,
        )
        self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> RunRecord:
        run = self._runs.get(run_id)
        if run is None:
            raise RunContractError(f"Unknown run: {run_id}")
        return run

    def runs_for_task(self, task_id: str) -> list[RunRecord]:
        return [run for run in self._runs.values() if run.task_id == task_id]

    def bind_admission(
        self, run_id: str, admission: RuntimeAdmission,
    ) -> None:
        """Bind one immutable admission to one run exactly once."""
        run = self.get(run_id)
        if run_id in self._admissions:
            raise AdmissionBindingError(
                f"Run {run_id} already binds its immutable admission"
            )
        if admission.run_id != run_id or admission.task_id != run.task_id:
            raise AdmissionBindingError(
                "The admission identifiers do not match the run"
            )
        if admission.runtime_key != run.runtime_key:
            raise AdmissionBindingError(
                "The admission runtime pair does not match the run"
            )
        self._admissions[run_id] = admission

    def admission_for(self, run_id: str) -> RuntimeAdmission:
        admission = self._admissions.get(run_id)
        if admission is None:
            raise RunContractError(f"Run {run_id} binds no admission")
        return admission

    def transition(self, run_id: str, target: RunState) -> RunRecord:
        """Apply one validated non-terminal state transition."""
        run = self.get(run_id)
        validate_run_transition(run.state, target)
        run.state = target
        return run

    def restart(self, run_id: str) -> RunRecord:
        """Resume the same run after process loss.

        The run identifier, the admission, and the runtime pair stay
        unchanged.
        """
        run = self.get(run_id)
        if run.state in TERMINAL_RUN_STATES:
            raise TerminalRunError(
                f"A {run.state.value} run is terminal and never restarts"
            )
        run.state = RunState.RUNNING
        return run

    def retry_activation(self, run_id: str) -> RunRecord:
        """Create a new attempt inside the same run."""
        run = self.get(run_id)
        if run.state in TERMINAL_RUN_STATES:
            raise TerminalRunError(
                f"A {run.state.value} run is terminal and never retries"
            )
        run.attempt += 1
        return run

    def rerun(
        self, run_id: str, *, invalidation_id: str | None = None,
    ) -> RunRecord:
        """Create a new run under the same task after a terminal run."""
        run = self.get(run_id)
        if run.state not in TERMINAL_RUN_STATES:
            raise RunContractError(
                "A rerun requires a terminal predecessor; an active run "
                "reroutes instead"
            )
        return self.create_run(
            task_id=run.task_id,
            tenant_id=run.tenant_id,
            runtime_key=run.runtime_key,
            parent_run_id=run.run_id,
            lineage_reason=RunLineageReason.RERUN,
            invalidation_id=invalidation_id,
        )

    def reroute(
        self, run_id: str, *, runtime_key: RuntimeKey,
    ) -> RunRecord:
        """Create a successor run for one active run.

        The predecessor keeps its immutable runtime pair and moves to
        cancelling. The successor records the reroute lineage.
        """
        run = self.get(run_id)
        if run.state in TERMINAL_RUN_STATES:
            raise TerminalRunError(
                f"A {run.state.value} run is terminal; use a rerun"
            )
        successor = self.create_run(
            task_id=run.task_id,
            tenant_id=run.tenant_id,
            runtime_key=runtime_key,
            parent_run_id=run.run_id,
            lineage_reason=RunLineageReason.REROUTE,
            rerouted_from_run_id=run.run_id,
        )
        if run.state is not RunState.CANCELLING:
            validate_run_transition(run.state, RunState.CANCELLING)
            run.state = RunState.CANCELLING
        return successor

    def benchmark_repetition(
        self,
        *,
        task_id: str,
        tenant_id: str,
        runtime_key: RuntimeKey,
        parent_run_id: str,
        repetition_slot: int,
    ) -> RunRecord:
        """Create one run for one planned benchmark repetition slot."""
        if repetition_slot < 1:
            raise RunContractError("A repetition slot starts at one")
        return self.create_run(
            task_id=task_id,
            tenant_id=tenant_id,
            runtime_key=runtime_key,
            parent_run_id=parent_run_id,
            lineage_reason=RunLineageReason.BENCHMARK_REPETITION,
            repetition_slot=repetition_slot,
        )


class OutcomeClass(StrEnum):
    """The common terminal classes shared by every runtime mapping."""

    SUCCESS = "success"
    SUBSTANTIVE_FAILURE = "substantive_failure"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    CANCELLATION = "cancellation"


_TERMINAL_STATE_FOR_CLASS = {
    OutcomeClass.SUCCESS: RunState.COMPLETED,
    OutcomeClass.SUBSTANTIVE_FAILURE: RunState.FAILED,
    OutcomeClass.INFRASTRUCTURE_FAILURE: RunState.FAILED,
    OutcomeClass.CANCELLATION: RunState.CANCELLED,
}


@dataclass(frozen=True)
class ReasonBinding:
    """Bind one registered reason code to its trusted meaning."""

    common_class: OutcomeClass
    retryable: bool


# The shared pre-execution reasons. Every runtime mapping registers
# these codes so a failure before execution always has one trusted
# terminal identity.
SHARED_PRE_EXECUTION_REASONS: dict[str, ReasonBinding] = {
    "admission_failure": ReasonBinding(
        OutcomeClass.INFRASTRUCTURE_FAILURE, retryable=False,
    ),
    "blueprint_failure": ReasonBinding(
        OutcomeClass.INFRASTRUCTURE_FAILURE, retryable=True,
    ),
    "initialization_failure": ReasonBinding(
        OutcomeClass.INFRASTRUCTURE_FAILURE, retryable=True,
    ),
    "genesis_failure": ReasonBinding(
        OutcomeClass.INFRASTRUCTURE_FAILURE, retryable=True,
    ),
}


class ReasonRegistry:
    """The closed registry of terminal reason mappings per runtime pair.

    Each runtime publishes its complete mapping before qualification.
    The mapping declares every common class at least once and includes
    every shared pre-execution reason. A model cannot add a reason and
    cannot choose a trusted terminal field.
    """

    def __init__(self) -> None:
        self._mappings: dict[RuntimeKey, dict[str, ReasonBinding]] = {}
        self._mapping_versions: dict[RuntimeKey, str] = {}

    def register_mapping(
        self,
        runtime_key: RuntimeKey,
        mapping: dict[str, ReasonBinding],
        *,
        mapping_version: str,
    ) -> None:
        require_runtime(runtime_key)
        _require_exact_string("mapping_version", mapping_version)
        complete = {**SHARED_PRE_EXECUTION_REASONS, **mapping}
        for reason_code, binding in complete.items():
            _require_exact_string("reason_code", reason_code)
            if _VERSION_TOKEN.search(reason_code):
                raise RunContractError(
                    f"Reason code {reason_code!r} carries a version token; "
                    "identifiers stay generation-neutral"
                )
            if not isinstance(binding, ReasonBinding):
                raise RunContractError(
                    f"Reason {reason_code!r} must bind a ReasonBinding"
                )
        for shared in SHARED_PRE_EXECUTION_REASONS:
            if complete[shared] != SHARED_PRE_EXECUTION_REASONS[shared]:
                raise RunContractError(
                    f"The shared reason {shared!r} cannot be redefined"
                )
        declared_classes = {binding.common_class for binding in complete.values()}
        missing_classes = set(OutcomeClass) - declared_classes
        if missing_classes:
            raise RunContractError(
                "The mapping must declare every common class; missing: "
                f"{sorted(cls.value for cls in missing_classes)}"
            )
        self._mappings[runtime_key] = dict(complete)
        self._mapping_versions[runtime_key] = mapping_version

    def mapping_for(self, runtime_key: RuntimeKey) -> dict[str, ReasonBinding]:
        mapping = self._mappings.get(runtime_key)
        if mapping is None:
            raise UnregisteredReasonError(
                f"Runtime pair {runtime_key} publishes no reason mapping"
            )
        return dict(mapping)

    def mapping_version_for(self, runtime_key: RuntimeKey) -> str:
        self.mapping_for(runtime_key)
        return self._mapping_versions[runtime_key]

    def binding_for(
        self, runtime_key: RuntimeKey, reason_code: str,
    ) -> ReasonBinding:
        mapping = self.mapping_for(runtime_key)
        binding = mapping.get(reason_code)
        if binding is None:
            raise UnregisteredReasonError(
                f"Reason {reason_code!r} is not registered for {runtime_key}"
            )
        return binding


@dataclass(frozen=True)
class RuntimeOutcome:
    """The one host-created terminal record of one run."""

    outcome_id: str
    run_id: str
    tenant_id: str
    runtime_key: RuntimeKey
    common_class: OutcomeClass
    reason_code: str
    mapping_version: str
    final_references: tuple[str, ...]
    resource_references: tuple[str, ...]
    terminal_journal_cursor: int
    metadata_schema_version: str = RUN_CONTRACT_SCHEMA_VERSION

    def digest(self) -> str:
        """Return the canonical digest of this outcome."""
        record = {
            "outcome_id": self.outcome_id,
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "runtime_key": self.runtime_key.to_dict(),
            "common_class": self.common_class.value,
            "reason_code": self.reason_code,
            "mapping_version": self.mapping_version,
            "final_references": list(self.final_references),
            "resource_references": list(self.resource_references),
            "terminal_journal_cursor": self.terminal_journal_cursor,
            "metadata_schema_version": self.metadata_schema_version,
        }
        return canonical_digest(record)


class OutcomeLedger:
    """The host authority that creates every terminal outcome.

    The ledger derives the common class from the registered reason
    mapping. It never reads a model-claimed status, class, or reason.
    Each run holds exactly one terminal outcome, written together with
    one terminal journal record.
    """

    def __init__(
        self, run_ledger: RunLedger, reason_registry: ReasonRegistry,
    ) -> None:
        self._runs = run_ledger
        self._reasons = reason_registry
        self._outcomes: dict[str, RuntimeOutcome] = {}
        self._journal: list[dict[str, Any]] = []

    @property
    def journal(self) -> list[dict[str, Any]]:
        return list(self._journal)

    def outcome_for(self, run_id: str) -> RuntimeOutcome:
        outcome = self._outcomes.get(run_id)
        if outcome is None:
            raise RunContractError(f"Run {run_id} holds no terminal outcome")
        return outcome

    def record_outcome(
        self,
        run_id: str,
        reason_code: str,
        *,
        final_references: tuple[str, ...] = (),
        resource_references: tuple[str, ...] = (),
    ) -> RuntimeOutcome:
        """Create the one terminal outcome for one run.

        The host supplies the registered reason code from its own
        observation. The registry supplies the trusted common class and
        the mapping version. A second outcome for the same run raises
        without a new journal record.
        """
        run = self._runs.get(run_id)
        if run_id in self._outcomes:
            raise OutcomeConflictError(
                f"Run {run_id} already holds its immutable terminal outcome"
            )
        if run.state in TERMINAL_RUN_STATES:
            raise TerminalRunError(
                f"Run {run_id} is already terminal without an outcome record"
            )
        binding = self._reasons.binding_for(run.runtime_key, reason_code)
        outcome = RuntimeOutcome(
            outcome_id=f"outcome-{uuid.uuid4()}",
            run_id=run_id,
            tenant_id=run.tenant_id,
            runtime_key=run.runtime_key,
            common_class=binding.common_class,
            reason_code=reason_code,
            mapping_version=self._reasons.mapping_version_for(run.runtime_key),
            final_references=tuple(final_references),
            resource_references=tuple(resource_references),
            terminal_journal_cursor=len(self._journal),
        )
        # One terminal transaction: the outcome, the run state, and the
        # journal record commit together.
        self._outcomes[run_id] = outcome
        run.state = _TERMINAL_STATE_FOR_CLASS[binding.common_class]
        self._journal.append(
            {
                "kind": "terminal_outcome",
                "cursor": outcome.terminal_journal_cursor,
                "run_id": run_id,
                "outcome_id": outcome.outcome_id,
                "outcome_digest": outcome.digest(),
            }
        )
        return outcome


TARGET_KINDS = (
    "projection",
    "slot",
    "analysis",
    "report",
    "claim",
    "evidence",
    "candidate",
)

INVALIDATION_REASONS = (
    "source_retracted",
    "evidence_corrected",
    "policy_breach_found",
)

DATA_CLASSIFICATIONS = ("public", "sensitive")


@dataclass(frozen=True)
class TargetReference:
    """One typed reference that an invalidation affects."""

    kind: str
    reference: str

    def __post_init__(self) -> None:
        if self.kind not in TARGET_KINDS:
            raise InvalidationRejectedError(
                f"Unknown target kind: {self.kind!r}"
            )
        _require_exact_string("reference", self.reference)


def derive_invalidation_idempotency_key(
    *,
    tenant_id: str,
    run_id: str,
    outcome_digest: str,
    source_digest: str,
    policy_version: str,
    targets: tuple[TargetReference, ...],
) -> str:
    """Derive the deterministic idempotency key for one invalidation."""
    sorted_targets = sorted(
        (target.kind, target.reference) for target in targets
    )
    frame = "\x00".join(
        [
            "bmas:post-terminal-invalidation",
            tenant_id,
            run_id,
            outcome_digest,
            source_digest,
            policy_version,
            json.dumps(sorted_targets, separators=(",", ":")),
        ]
    )
    return hashlib.sha256(frame.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InvalidationRequest:
    """One post-terminal invalidation request from an authority."""

    tenant_id: str
    task_id: str
    run_id: str
    outcome_id: str
    outcome_digest: str
    source_type: str
    source_id: str
    source_digest: str
    targets: tuple[TargetReference, ...]
    reason_code: str
    authority_id: str
    decision_reference: str
    data_classification: str = "public"
    redaction_policy: str = "none"
    requested_operations: tuple[str, ...] = ("invalidate",)

    def digest(self) -> str:
        """Return the canonical digest of this request."""
        record = {
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "outcome_id": self.outcome_id,
            "outcome_digest": self.outcome_digest,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_digest": self.source_digest,
            "targets": sorted(
                (target.kind, target.reference) for target in self.targets
            ),
            "reason_code": self.reason_code,
            "authority_id": self.authority_id,
            "decision_reference": self.decision_reference,
            "data_classification": self.data_classification,
            "redaction_policy": self.redaction_policy,
            "requested_operations": list(self.requested_operations),
        }
        return canonical_digest(record)


@dataclass(frozen=True)
class PostTerminalInvalidation:
    """The immutable record of one post-terminal invalidation."""

    metadata_schema_version: str
    invalidation_id: str
    idempotency_key: str
    tenant_id: str
    task_id: str
    run_id: str
    runtime_key: RuntimeKey
    outcome_id: str
    outcome_digest: str
    source_type: str
    source_id: str
    source_digest: str
    targets: tuple[TargetReference, ...]
    reason_code: str
    policy_version: str
    authority_id: str
    decision_reference: str
    prior_state_version: int
    prior_state_digest: str
    data_classification: str
    redaction_policy: str
    database_time: str
    journal_cursor: int


class InvalidationService:
    """Apply post-terminal invalidations to derived projections only.

    The service changes current validity and supersession projections.
    It cannot change a run state, a terminal class, a terminal reason,
    an outcome field, or an outcome digest. It never executes a model
    or a tool; new execution requires a separately admitted successor
    run.
    """

    def __init__(
        self,
        run_ledger: RunLedger,
        outcome_ledger: OutcomeLedger,
        *,
        authorized_authority_ids: frozenset[str],
        policy_version: str,
        known_targets: frozenset[tuple[str, str]],
        database_clock: Any = None,
    ) -> None:
        self._runs = run_ledger
        self._outcomes = outcome_ledger
        self._authorized = authorized_authority_ids
        self._policy_version = policy_version
        self._known_targets = set(known_targets)
        self._clock = database_clock or (
            lambda: "1970-01-01T00:00:00.000Z"
        )
        self._by_key: dict[str, tuple[str, PostTerminalInvalidation]] = {}
        self._journal: list[dict[str, Any]] = []
        self._projections: dict[tuple[str, str], dict[str, Any]] = {}
        self._protected_details: dict[str, dict[str, str]] = {}
        self._state_version = 0

    @property
    def journal(self) -> list[dict[str, Any]]:
        return list(self._journal)

    def projection_state(self) -> dict[tuple[str, str], dict[str, Any]]:
        return {key: dict(value) for key, value in self._projections.items()}

    def read_protected_detail(
        self, invalidation_id: str, authority_id: str,
    ) -> dict[str, str]:
        """Return the protected diagnostic detail to an authority only."""
        if authority_id not in self._authorized:
            raise InvalidationRejectedError(
                "Only an authorized authority reads a protected detail"
            )
        detail = self._protected_details.get(invalidation_id)
        if detail is None:
            raise InvalidationRejectedError(
                f"Unknown invalidation: {invalidation_id}"
            )
        return dict(detail)

    def _validate(self, request: InvalidationRequest) -> RuntimeOutcome:
        if request.authority_id not in self._authorized:
            raise InvalidationRejectedError(
                "The invalidation authority is not authorized"
            )
        improper = tuple(
            operation for operation in request.requested_operations
            if operation != "invalidate"
        )
        if improper:
            raise InvalidationExecutionError(
                f"The invalidation service denies {sorted(improper)}. New "
                "model or tool execution requires a separately admitted "
                "successor run."
            )
        run = self._runs.get(request.run_id)
        if run.tenant_id != request.tenant_id:
            raise InvalidationRejectedError(
                "The invalidation names a foreign tenant"
            )
        if run.task_id != request.task_id:
            raise InvalidationRejectedError(
                "The invalidation names the wrong task for this run"
            )
        if run.state not in TERMINAL_RUN_STATES:
            raise InvalidationRejectedError(
                "Post-terminal invalidation requires a terminal run"
            )
        outcome = self._outcomes.outcome_for(request.run_id)
        if outcome.outcome_id != request.outcome_id:
            raise InvalidationRejectedError(
                "The invalidation names the wrong terminal outcome"
            )
        if outcome.digest() != request.outcome_digest:
            raise InvalidationRejectedError(
                "The invalidation outcome digest does not match"
            )
        if request.reason_code not in INVALIDATION_REASONS:
            raise InvalidationRejectedError(
                f"Unregistered invalidation reason: {request.reason_code!r}"
            )
        if request.data_classification not in DATA_CLASSIFICATIONS:
            raise InvalidationRejectedError(
                f"Unknown data classification: {request.data_classification!r}"
            )
        if not request.targets:
            raise InvalidationRejectedError(
                "An invalidation names at least one target"
            )
        for target in request.targets:
            if (target.kind, target.reference) not in self._known_targets:
                raise InvalidationRejectedError(
                    f"Inaccessible invalidation target: {target}"
                )
        return outcome

    def submit(self, request: InvalidationRequest) -> PostTerminalInvalidation:
        """Validate, deduplicate, and apply one invalidation request."""
        outcome = self._validate(request)
        key = derive_invalidation_idempotency_key(
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            outcome_digest=request.outcome_digest,
            source_digest=request.source_digest,
            policy_version=self._policy_version,
            targets=request.targets,
        )
        request_digest = request.digest()
        stored = self._by_key.get(key)
        if stored is not None:
            stored_digest, stored_record = stored
            if stored_digest == request_digest:
                return stored_record
            raise InvalidationConflictError(
                "The idempotency key was reused with a different request"
            )

        redacted = request.data_classification == "sensitive"
        record = PostTerminalInvalidation(
            metadata_schema_version=RUN_CONTRACT_SCHEMA_VERSION,
            invalidation_id=f"invalidation-{uuid.uuid4()}",
            idempotency_key=key,
            tenant_id=request.tenant_id,
            task_id=request.task_id,
            run_id=request.run_id,
            runtime_key=outcome.runtime_key,
            outcome_id=outcome.outcome_id,
            outcome_digest=request.outcome_digest,
            source_type=request.source_type,
            source_id="redacted" if redacted else request.source_id,
            source_digest=request.source_digest,
            targets=request.targets,
            reason_code="redacted" if redacted else request.reason_code,
            policy_version=self._policy_version,
            authority_id=request.authority_id,
            decision_reference=request.decision_reference,
            prior_state_version=self._state_version,
            prior_state_digest=canonical_digest(
                sorted(
                    (list(key_pair), dict(value))
                    for key_pair, value in self._projections.items()
                )
            ),
            data_classification=request.data_classification,
            redaction_policy=request.redaction_policy,
            database_time=self._clock(),
            journal_cursor=len(self._journal),
        )
        if redacted:
            self._protected_details[record.invalidation_id] = {
                "reason_code": request.reason_code,
                "source_id": request.source_id,
            }

        # One commit: the record, the projection changes, and the
        # journal record apply together.
        self._by_key[key] = (request_digest, record)
        self._state_version += 1
        for target in request.targets:
            self._projections[(target.kind, target.reference)] = {
                "current": False,
                "superseded_by": record.invalidation_id,
            }
        self._journal.append(
            {
                "kind": "post_terminal_invalidation",
                "cursor": record.journal_cursor,
                "invalidation_id": record.invalidation_id,
                "idempotency_key": key,
                "reason_code": record.reason_code,
                "source_id": record.source_id,
                "targets": sorted(
                    (target.kind, target.reference)
                    for target in request.targets
                ),
            }
        )
        return record


def replay_invalidation_projections(
    journal: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Rebuild the current invalidation projections from the journal."""
    projections: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in journal:
        if entry.get("kind") != "post_terminal_invalidation":
            continue
        for kind, reference in entry["targets"]:
            projections[(kind, reference)] = {
                "current": False,
                "superseded_by": entry["invalidation_id"],
            }
    return projections
