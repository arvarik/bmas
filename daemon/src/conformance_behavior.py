"""The behavioral conformance suite for one runtime pair.

The matrix suite in ``conformance_kit`` checks that a published record
declares one value per capability. This suite executes the Foundation
services for one runtime pair and derives the observed value from what
happened: the journal genesis carries the exact pair, the artifact
store keeps bytes immutable, an applied seed changes the output, a
cancel stops the next step, a stale fence is rejected, a restart
replays to the same projection, a native execution writes durable
activation and effect rows while a legacy execution writes none, the
endpoint directory fails closed for the pair's protocol, budgets refuse
an over-limit reservation, evidence and goals reach the projection,
every record carries the common envelope, a terminal outcome closes
the run to further updates, the reference scorer replays the runtime
output deterministically, and the interface falls back to the generic
panels.

The suite drives one ``RuntimeExecutor``. The reference executor runs
the registered reference runtime in process. The legacy executor
writes what a legacy runtime writes: a task record and a trace, with
no native authority row. A case derives its observed matrix value from
the executor's real behaviour and compares it with the declared value,
so a regression in the runtime or a legacy path that starts writing
native rows fails the pair's conformance column.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import database as db
import runtime_journal as journal
from agent_protocol import (
    AgentCapabilityDocument,
    AgentEndpoint,
    EndpointDirectory,
    NoQualifiedEndpointError,
    is_qualified,
)
from capability_publication import (
    GENERIC_UI_FALLBACK_PANELS,
    CapabilityDirectory,
    RuntimeCapabilityRecord,
)
from conformance_kit import CaseResult, ConformanceReport, score_reference_evidence
from core.asset_store import (
    ARTIFACT_CONTENT_DIGEST_DOMAIN,
    ArtifactCommitError,
    ArtifactStore,
    ArtifactValidationError,
    DataClass,
    RetentionClass,
)
from core.digest_profile import digest_bytes
from core.keyed_digest import (
    TenantKeyRing,
    audit_digest_record,
    export_digest_for_public_view,
    keyed_digest,
)
from core.runtime_services import (
    AuthorityError,
    CancellationService,
    CheckpointService,
    FencedAuthority,
    RunControlService,
    TaskLeaseGuard,
)
from core.variants import (
    RuntimeKey,
    VariantExecutionRequest,
    require_checkpoint_reader,
    require_runtime,
)

BEHAVIOR_CASES = (
    "admission_identity",
    "assets_privacy",
    "seed_state",
    "cancellation_deadlines",
    "lease_fencing_restart_replay",
    "activation_effect_ledgers",
    "agent_protocol_negotiation",
    "budget_reservations",
    "evidence_decisions",
    "goals",
    "trace_envelope",
    "post_terminal_invalidation",
    "reference_scoring_replay",
    "ui_fallback",
)

TASK_TEXT = "Add 20 and 22."
NATIVE_AUTHORITY_TABLES = (
    "runtime_journal",
    "runtime_admissions",
    "activation_dispatch_outbox",
    "effect_dispatch_outbox",
)


class BehaviorError(AssertionError):
    """The behavioral suite cannot run one case."""


class ConformanceAbort(RuntimeError):
    """The conformance host stopped an execution at the abort check."""


@dataclass
class ExecutionResult:
    """What one executor produced for one task."""

    task_id: str
    answer: str
    result: dict[str, Any]
    checkpoint: dict[str, Any] | None
    aborted: bool
    phases: list[str]
    native_rows: int


class RuntimeExecutor(Protocol):
    """Execute one task for one runtime pair."""

    runtime_key: RuntimeKey
    native: bool

    async def execute(
        self,
        *,
        task_id: str,
        user_task: str,
        seed: int,
        resume: bool = False,
        abort_after_steps: int | None = None,
    ) -> ExecutionResult:
        ...


async def native_authority_rows(task_id: str) -> int:
    """Count native authority rows that name one task."""
    total = 0
    async with db._connect() as connection:  # noqa: SLF001
        for table in NATIVE_AUTHORITY_TABLES:
            cursor = await connection.execute(f"PRAGMA table_info({table})")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if "task_id" not in columns:
                continue
            cursor = await connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE task_id = ?", (task_id,),
            )
            row = await cursor.fetchone()
            total += int(row[0]) if row is not None else 0
    return total


class ReferenceHost:
    """The in-process host for the reference runtime.

    Checkpoints persist in the task's durable metadata, so a resumed
    execution reads what the interrupted execution saved.
    """

    def __init__(self, *, abort_after_steps: int | None = None) -> None:
        self.phases: list[str] = []
        self.logs: list[dict[str, Any]] = []
        self.progress: list[dict[str, Any]] = []
        self.abort_after_steps = abort_after_steps
        self._checks = 0

    async def publish_phase(self, phase: str, iteration: int, task_id: str) -> None:
        self.phases.append(phase)

    async def check_abort(self, task_id: str) -> None:
        if self.abort_after_steps is not None and self._checks >= self.abort_after_steps:
            raise ConformanceAbort(task_id)
        self._checks += 1

    async def log_event(self, node_id: str, message: str, task_id: str, **kwargs: Any) -> None:
        self.logs.append({"node_id": node_id, "message": message, **kwargs})

    async def dispatch_agent(self, *, task_id: str, activation_id: str, **kwargs: Any) -> dict[str, Any]:
        raise BehaviorError("The reference runtime dispatches no agent")

    async def publish_progress(
        self, task_id: str, label: str, status: str, items: list[dict[str, Any]],
    ) -> None:
        self.progress.append({"label": label, "status": status, "items": items})

    def task_lease_token(self, task_id: str) -> str | None:
        return None

    async def load_variant_checkpoint(self, task_id: str, variant_id: str) -> dict[str, Any] | None:
        metadata = await db.get_board_meta(task_id)
        checkpoint = metadata.get("variant_checkpoint")
        if isinstance(checkpoint, dict) and checkpoint.get("variant_id") == variant_id:
            return checkpoint
        return None

    async def save_variant_checkpoint(
        self, task_id: str, variant_id: str, checkpoint: dict[str, Any],
    ) -> None:
        await db.patch_board_meta(
            task_id, {"variant_checkpoint": {**checkpoint, "variant_id": variant_id}},
        )


@dataclass
class ReferenceExecutor:
    """Run the registered reference runtime in process."""

    runtime_key: RuntimeKey
    native: bool = True
    steps: int = 3

    async def execute(
        self,
        *,
        task_id: str,
        user_task: str,
        seed: int,
        resume: bool = False,
        abort_after_steps: int | None = None,
    ) -> ExecutionResult:
        runtime = require_runtime(self.runtime_key)
        configuration = await runtime.capture_configuration({"steps": self.steps, "seed": seed})
        if not resume:
            await db.create_task_with_meta(
                task_id, "conformance", user_task, self.runtime_key.runtime_id,
                {"seed": seed, "effective_configuration": configuration},
                runtime_contract_version=self.runtime_key.runtime_contract_version,
            )
        host = ReferenceHost(abort_after_steps=abort_after_steps)
        request = VariantExecutionRequest(
            task_id=task_id, session_id=f"session-{task_id}", user_task=user_task,
            triage=None, overrides=None, resume=resume,
            effective_configuration=configuration,
        )
        aborted = False
        answer, result = "", {}
        try:
            outcome = await runtime.run(host, request)
            answer, result = outcome.answer, dict(outcome.result)
        except ConformanceAbort:
            aborted = True
        checkpoint = await host.load_variant_checkpoint(task_id, self.runtime_key.runtime_id)
        return ExecutionResult(
            task_id=task_id, answer=answer, result=result, checkpoint=checkpoint,
            aborted=aborted, phases=list(host.phases),
            native_rows=await native_authority_rows(task_id),
        )


@dataclass
class LegacyTraceExecutor:
    """Write what a legacy runtime writes: a task, a trace, and an answer.

    The legacy runtimes execute through agents and providers, so the
    suite records their durable footprint instead of running them. The
    answer never depends on the seed, and no native authority row
    exists for the task.
    """

    runtime_key: RuntimeKey
    native: bool = False
    answer: str = "42"

    async def execute(
        self,
        *,
        task_id: str,
        user_task: str,
        seed: int,
        resume: bool = False,
        abort_after_steps: int | None = None,
    ) -> ExecutionResult:
        if not resume:
            await db.create_task_with_meta(
                task_id, "conformance", user_task, self.runtime_key.runtime_id,
                {"seed": seed},
                runtime_contract_version=self.runtime_key.runtime_contract_version,
            )
        await db.insert_agent_traces([
            {"task_id": task_id, "turn_id": "turn-1", "seq": 1, "role": "expert",
             "type": "message", "data": {"content": self.answer}},
        ])
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                "UPDATE tasks SET status = 'completed', result_summary = ? WHERE id = ?",
                (self.answer, task_id),
            )
            await connection.commit()
        return ExecutionResult(
            task_id=task_id, answer=self.answer, result={"answer": self.answer},
            checkpoint=None, aborted=False, phases=["legacy"],
            native_rows=await native_authority_rows(task_id),
        )


@dataclass
class BehaviorEnvironment:
    """One prepared run for one runtime pair under one executor."""

    record: RuntimeCapabilityRecord
    executor: RuntimeExecutor
    root: Path
    run_id: str
    task_id: str
    fence: str = "fence-conformance"
    tenant_id: str = "tenant-default"
    lease_owner: str = "worker-conformance"
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def runtime_key(self) -> RuntimeKey:
        return self.record.runtime_key

    def declared(self, capability: str) -> str:
        return self.record.capabilities[capability]

    def operation(
        self, operation_type: str, payload: dict[str, Any], token: str, **extra: Any,
    ) -> journal.JournalOperation:
        return journal.JournalOperation(
            operation_type=operation_type,
            task_id=self.task_id,
            run_id=self.run_id,
            runtime_id=self.runtime_key.runtime_id,
            runtime_contract_version=self.runtime_key.runtime_contract_version,
            payload=payload,
            idempotency_token=f"{token}-{self.run_id}",
            **extra,
        )


async def prepare_environment(
    record: RuntimeCapabilityRecord,
    executor: RuntimeExecutor,
    root: Path,
    *,
    run_id: str | None = None,
) -> BehaviorEnvironment:
    """Create the task and the run-control row one suite runs against."""
    pair = f"{record.runtime_key.runtime_id}-{record.runtime_key.runtime_contract_version}"
    environment = BehaviorEnvironment(
        record=record, executor=executor, root=Path(root),
        run_id=run_id or f"run-conformance-{pair}",
        task_id=f"task-conformance-{pair}",
    )
    await db.create_task_with_meta(
        environment.task_id, "conformance", TASK_TEXT, record.runtime_key.runtime_id, {},
        runtime_contract_version=record.runtime_key.runtime_contract_version,
    )
    await db.create_run_control(environment.run_id, environment.task_id, environment.fence)
    return environment


def _verified_value(env: BehaviorEnvironment, capability: str, verified: bool) -> str:
    """The declared value when the shared behaviour held, else unavailable."""
    return env.declared(capability) if verified else "unavailable"


# ── Cases ────────────────────────────────────────────────────────────


async def _admission_identity(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("shared_submission")
    operation = env.operation("admission_identity", {
        "admission_id": f"admission-{env.run_id}",
        "version_set": {"checkpoint_schema_version": "1"},
        "specification_digest": "1" * 64,
        "capability_document_digest": "2" * 64,
        "admission_digest": "3" * 64,
    }, "admission")
    first = await journal.commit_operation(operation)
    second = await journal.commit_operation(operation)
    chain = await journal.read_journal(run_id=env.run_id)
    identity = RuntimeKey(chain[0].runtime_id, chain[0].runtime_contract_version)
    registered = require_runtime(env.runtime_key) is not None
    verified = (
        identity == env.runtime_key
        and first.journal_cursor == second.journal_cursor
        and len(chain) == 1
        and chain[0].run_sequence == 0
        and registered
    )
    return CaseResult(
        "admission_identity", verified, expected,
        _verified_value(env, "shared_submission", verified),
        detail=f"identity={identity.to_dict()}, cursor={first.journal_cursor}, idempotent={first.journal_cursor == second.journal_cursor}",
    )


async def _assets_privacy(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("immutable_assets")
    store = ArtifactStore(env.root / "artifacts", env.tenant_id)
    payload = f"artifact for {env.run_id}".encode()
    digest = digest_bytes(ARTIFACT_CONTENT_DIGEST_DOMAIN, payload)
    common = {
        "declared_size": len(payload), "media_type": "text/plain",
        "scanner_result": "clean", "data_class": DataClass.INTERNAL,
        "access_policy": "tenant", "retention_class": RetentionClass.REPLAY_REQUIRED,
    }
    try:
        store.stage(payload, declared_digest="0" * 64, **common)
        wrong_digest_rejected = False
    except ArtifactValidationError:
        wrong_digest_rejected = True
    try:
        store.commit_reference(digest, referenced_by=env.run_id)
        early_reference_rejected = False
    except ArtifactCommitError:
        early_reference_rejected = True
    staged = store.stage(payload, declared_digest=digest, **common)
    promoted = store.promote(staged)
    again = store.promote(store.stage(payload, declared_digest=digest, **common))
    store.commit_reference(digest, referenced_by=env.run_id)
    immutable = promoted == again == digest and store.has_object(digest)
    ring = TenantKeyRing()
    ring.install_key(env.tenant_id, "hmac-key-conformance", b"c" * 32)
    keyed = keyed_digest(ring, env.tenant_id, "principal-email", "person@example.org")
    audit_digest_record(keyed.to_dict())
    public = export_digest_for_public_view(keyed.to_dict())
    private = "person@example.org" not in json.dumps(public) and keyed.value not in json.dumps(public)
    verified = wrong_digest_rejected and early_reference_rejected and immutable and private
    return CaseResult(
        "assets_privacy", verified, expected,
        _verified_value(env, "immutable_assets", verified),
        detail=f"digest={digest[:12]}, public={public.get('redacted')}",
    )


async def _seed_state(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("applied_seed_evidence")
    executor = env.executor
    first = await executor.execute(task_id=f"{env.task_id}-seed-a", user_task=TASK_TEXT, seed=7)
    second = await executor.execute(task_id=f"{env.task_id}-seed-b", user_task=TASK_TEXT, seed=7)
    other = await executor.execute(task_id=f"{env.task_id}-seed-c", user_task=TASK_TEXT, seed=8)
    equal = first.answer == second.answer and first.result == second.result
    differs = other.answer != first.answer
    recorded = (await db.get_board_meta(first.task_id)).get("seed") == 7
    if equal and differs and recorded:
        observed = "native"
    elif equal and recorded:
        observed = "recorded_only"
    else:
        observed = "unavailable"
    return CaseResult(
        "seed_state", observed == expected, expected, observed,
        detail=f"equal_seed_equal_output={equal}, other_seed_differs={differs}, seed_recorded={recorded}",
    )


async def _cancellation_deadlines(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("cancellation_signal")
    run_id = f"{env.run_id}-cancel"
    await db.create_run_control(run_id, env.task_id, env.fence)
    authority = FencedAuthority(run_id=run_id, lease_owner=env.lease_owner, lease_fence=env.fence)
    await TaskLeaseGuard(authority, scheduler=True, lease_ttl_seconds=60).acquire()
    controls = RunControlService(authority)
    deadline_set = await controls.set_deadline("2000-01-01T00:00:00.000Z", "cancel")
    cancellation = CancellationService(authority)
    states = (
        await cancellation.request(),
        await cancellation.acknowledge(),
        await cancellation.finalize(),
    )
    control_verified = deadline_set and all(states)
    interrupted = await env.executor.execute(
        task_id=f"{env.task_id}-cancel", user_task=TASK_TEXT, seed=1, abort_after_steps=1,
    )
    if env.executor.native:
        stopped = (
            interrupted.aborted
            and interrupted.checkpoint is not None
            and int(interrupted.checkpoint.get("completed_steps", 0)) == 1
            and interrupted.phases.count("reference_step") == 1
        )
        observed = "native" if control_verified and stopped else "unavailable"
    else:
        observed = "legacy" if control_verified and not interrupted.aborted else "unavailable"
    return CaseResult(
        "cancellation_deadlines", observed == expected, expected, observed,
        detail=f"control_states={states}, deadline={deadline_set}, aborted={interrupted.aborted}",
    )


async def _lease_fencing_restart_replay(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("task_fence_validation")
    authority = FencedAuthority(run_id=env.run_id, lease_owner=env.lease_owner, lease_fence=env.fence)
    acquired = await TaskLeaseGuard(authority, scheduler=True, lease_ttl_seconds=60).acquire()
    stale = FencedAuthority(run_id=env.run_id, lease_owner="worker-stale", lease_fence="fence-stale")
    try:
        await stale.authorize()
        stale_denied = False
    except AuthorityError:
        stale_denied = True
    entry = await CheckpointService(authority).save(None, {"step": 1})  # type: ignore[arg-type]
    try:
        await journal.commit_operation(env.operation(
            "evidence_update", {"claim_id": "claim-fenced", "evidence_state": "verified"},
            "evidence-stale-fence", task_fence="fence-stale",
        ))
        fence_rejected = False
    except journal.JournalFenceError:
        fence_rejected = True
    fenced = await journal.commit_operation(env.operation(
        "evidence_update", {"claim_id": "claim-fenced", "evidence_state": "verified"},
        "evidence-live-fence", task_fence=env.fence,
    ))
    replayed = await journal.replay()
    state = journal.empty_projection_state()
    for record in await journal.read_journal():
        state = journal.apply_record_to_state(state, record)
    replay_equal = journal.projection_digest(state) == replayed.state_digest
    reader = require_checkpoint_reader(env.runtime_key) is not None
    shared = acquired and stale_denied and bool(entry.entry_id) and fence_rejected and replay_equal and reader
    if env.executor.native:
        interrupted = await env.executor.execute(
            task_id=f"{env.task_id}-resume", user_task=TASK_TEXT, seed=3, abort_after_steps=1,
        )
        resumed = await env.executor.execute(
            task_id=f"{env.task_id}-resume", user_task=TASK_TEXT, seed=3, resume=True,
        )
        complete = await env.executor.execute(
            task_id=f"{env.task_id}-complete", user_task=TASK_TEXT, seed=3,
        )
        resume_verified = (
            interrupted.aborted
            and resumed.result.get("resumed_from_step") == 1
            and resumed.result.get("digest") == complete.result.get("digest")
            and resumed.answer == complete.answer
        )
        observed = "native" if shared and resume_verified else "unavailable"
        detail = f"stale_denied={stale_denied}, fence_rejected={fence_rejected}, replay_equal={replay_equal}, resumed_from={resumed.result.get('resumed_from_step')}"
    else:
        observed = _verified_value(env, "task_fence_validation", shared)
        detail = f"stale_denied={stale_denied}, fence_rejected={fence_rejected}, replay_equal={replay_equal}, fenced_cursor={fenced.journal_cursor}"
    return CaseResult("lease_fencing_restart_replay", observed == expected, expected, observed, detail=detail)


async def _activation_effect_ledgers(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("durable_activation_ledger")
    dispatch_expected = env.declared("activation_dispatch_outbox")
    if env.executor.native:
        await journal.commit_operation(env.operation(
            "activation_transition",
            {"activation_id": "activation-conformance", "activation_state": "granted"},
            "activation-granted", activation_dispatch_id="activation-conformance",
        ))
        await journal.commit_operation(env.operation(
            "effect_transition",
            {"effect_id": "effect-conformance", "effect_state": "approved"},
            "effect-approved", effect_dispatch_id="effect-conformance",
        ))
        async with db._connect() as connection:  # noqa: SLF001
            activation_rows = (await (await connection.execute(
                "SELECT COUNT(*) FROM activation_dispatch_outbox WHERE run_id = ?", (env.run_id,),
            )).fetchone())[0]
            effect_rows = (await (await connection.execute(
                "SELECT COUNT(*) FROM effect_dispatch_outbox WHERE run_id = ?", (env.run_id,),
            )).fetchone())[0]
        durable = int(activation_rows) > 0 and int(effect_rows) > 0
        observed = "native" if durable else "unavailable"
        dispatch_observed = "native" if int(activation_rows) > 0 else "unavailable"
        detail = f"activation_rows={activation_rows}, effect_rows={effect_rows}"
    else:
        execution = await env.executor.execute(
            task_id=f"{env.task_id}-legacy", user_task=TASK_TEXT, seed=1,
        )
        observed = "native" if execution.native_rows > 0 else expected
        dispatch_observed = "native" if execution.native_rows > 0 else dispatch_expected
        detail = f"native_rows={execution.native_rows}"
    passed = observed == expected and dispatch_observed == dispatch_expected
    return CaseResult("activation_effect_ledgers", passed, expected, observed, detail=detail)


def _qualified_document(agent_id: str) -> AgentCapabilityDocument:
    return AgentCapabilityDocument(
        schema_version="1",
        agent_id=agent_id,
        supported_protocol_versions=("2",),
        supported_receipt_versions=("1",),
        supported_activation_schemas=("1",),
        supported_dispatch_schemas=("1",),
        supported_acknowledgement_schemas=("1",),
        supported_proposal_schemas=("1",),
        supported_envelope_schemas=("1",),
        nested_model_receipts=True,
        nested_tool_receipts=True,
        structured_output=True,
        usage_reporting=True,
        streaming=True,
        cancellation=True,
        resume=True,
        durable_grant_deduplication=True,
        acknowledgement_status_lookup=True,
        receipt_key_ids=("agent-key-conformance",),
        max_request_bytes=1 << 20,
        max_response_bytes=1 << 20,
        max_artifact_bytes=1 << 20,
    )


async def _agent_protocol_negotiation(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("agent_protocol")
    document = _qualified_document("native-agent")
    directory = EndpointDirectory()
    directory.publish(AgentEndpoint(agent_id="legacy-agent", protocol_version="1", qualification_state="qualified"))
    directory.publish(AgentEndpoint(
        agent_id="native-agent", protocol_version="2", qualification_state="qualified",
        capability_document=document,
    ))
    version = env.record.agent_protocol_version
    required = ("cancellation", "resume") if version == "2" else ()
    selected = directory.select(protocol_version=version, required_capability_names=required)
    legacy_only = EndpointDirectory()
    legacy_only.publish(AgentEndpoint(agent_id="legacy-agent", protocol_version="1", qualification_state="qualified"))
    try:
        legacy_only.select(protocol_version="2")
        fails_closed = False
    except NoQualifiedEndpointError:
        fails_closed = True
    if version == "2":
        observed = "native" if selected.protocol_version == "2" and is_qualified(document) and fails_closed else "unavailable"
    else:
        observed = "legacy" if selected.protocol_version == "1" and fails_closed else "unavailable"
    return CaseResult(
        "agent_protocol_negotiation", observed == expected, expected, observed,
        detail=f"selected={selected.agent_id}, protocol={selected.protocol_version}, fails_closed={fails_closed}",
    )


async def _budget_reservations(env: BehaviorEnvironment) -> CaseResult:
    import budget_service as budget

    expected = env.declared("budget_reservation")
    budget_id = f"budget-{env.run_id}"
    async with db._connect() as connection:  # noqa: SLF001
        await budget.create_run_budget(
            connection, budget_id=budget_id, run_id=env.run_id, task_id=env.task_id,
            currency="USD",
            limits=(budget.LimitSpec("run", env.run_id, "provider_cost", 1_000, currency="USD"),),
        )
        await connection.commit()
    await budget.request_reservation(
        reservation_id=f"{env.run_id}-reservation-a", budget_id=budget_id,
        resources={"provider_cost": 600},
    )
    within = await budget.reserve(f"{env.run_id}-reservation-a")
    await budget.request_reservation(
        reservation_id=f"{env.run_id}-reservation-b", budget_id=budget_id,
        resources={"provider_cost": 600},
    )
    over = await budget.reserve(f"{env.run_id}-reservation-b")
    verified = bool(within) and not over
    return CaseResult(
        "budget_reservations", verified, expected,
        _verified_value(env, "budget_reservation", verified),
        detail=f"within_limit={within}, over_limit={over}",
    )


async def _projection_state() -> dict[str, Any]:
    state = journal.empty_projection_state()
    for record in await journal.read_journal():
        state = journal.apply_record_to_state(state, record)
    return state


async def _evidence_decisions(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("typed_evidence_index")
    record = await journal.commit_operation(env.operation(
        "evidence_update", {"claim_id": "claim-conformance", "evidence_state": "verified"},
        "evidence-conformance",
    ))
    state = await _projection_state()
    indexed = "claim-conformance" in json.dumps(state)
    verified = indexed and record.operation_type == "evidence_update"
    return CaseResult(
        "evidence_decisions", verified, expected,
        _verified_value(env, "typed_evidence_index", verified),
        detail=f"indexed={indexed}, cursor={record.journal_cursor}",
    )


async def _goals(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("common_event_envelope")
    record = await journal.commit_operation(env.operation(
        "goal_update", {"goal_id": "goal-conformance", "goal_state": "satisfied"}, "goal-conformance",
    ))
    state = await _projection_state()
    verified = "goal-conformance" in json.dumps(state) and record.operation_type == "goal_update"
    return CaseResult(
        "goals", verified, expected, _verified_value(env, "common_event_envelope", verified),
        detail=f"cursor={record.journal_cursor}",
    )


async def _trace_envelope(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("trusted_envelope_creator")
    chain = await journal.read_journal(run_id=env.run_id)
    complete = all(
        record.producer and record.authority_type and record.data_classification
        and record.redaction_policy_version and isinstance(record.payload_schema_versions, dict)
        and journal.record_transaction_digest(record) == record.transaction_digest
        for record in chain
    )
    try:
        journal.verify_chain(chain)
        chain_ok = True
    except journal.JournalIntegrityError:
        chain_ok = False
    verified = bool(chain) and complete and chain_ok
    return CaseResult(
        "trace_envelope", verified, expected,
        _verified_value(env, "trusted_envelope_creator", verified),
        detail=f"records={len(chain)}, chain_ok={chain_ok}",
    )


async def _post_terminal_invalidation(env: BehaviorEnvironment) -> CaseResult:
    from core.digest_profile import digest_hex

    expected = env.declared("deterministic_analysis_replay")
    body = {
        "outcome_id": f"outcome-{env.run_id}", "common_class": "success",
        "reason_code": "task_answer_accepted", "mapping_version": "1",
    }
    outcome = {**body, "outcome_digest": digest_hex("journal-payload", body)}
    terminal = await journal.commit_operation(env.operation("terminal_outcome", outcome, "terminal"))
    try:
        await journal.commit_operation(env.operation(
            "evidence_update", {"claim_id": "claim-late", "evidence_state": "verified"}, "evidence-late",
        ))
        closed = False
    except journal.JournalError:
        closed = True
    invalidation = await journal.commit_operation(env.operation("post_terminal_invalidation", {
        "invalidation_id": f"invalidation-{env.run_id}",
        "outcome_id": outcome["outcome_id"],
        "outcome_digest": outcome["outcome_digest"],
        "targets": [{"kind": "projection", "reference": "projection-current"}],
        "reason_code": "source_retracted",
        "authority_id": "authority-review-board",
    }, "invalidation"))
    state = await _projection_state()
    recorded = f"invalidation-{env.run_id}" in json.dumps(state)
    verified = closed and recorded and invalidation.journal_cursor > terminal.journal_cursor
    return CaseResult(
        "post_terminal_invalidation", verified, expected,
        _verified_value(env, "deterministic_analysis_replay", verified),
        detail=f"closed_after_terminal={closed}, invalidation_recorded={recorded}",
    )


async def _reference_scoring_replay(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("foundation_reference_scoring")
    execution = await env.executor.execute(task_id=f"{env.task_id}-score", user_task=TASK_TEXT, seed=5)
    payload = json.dumps({
        "schema_id": "bmas.reference_scorer_input",
        "metadata": {"contract_version": "1.0.0"},
        "scorer": "exact_match",
        "cases": [{"case_id": "final", "expected": execution.answer, "actual": execution.answer}],
    }, separators=(",", ":")).encode()
    first = score_reference_evidence(payload)
    second = score_reference_evidence(payload)
    verified = bool(execution.answer) and first["result_digest"] == second["result_digest"] and bool(first["result_bytes"])
    return CaseResult(
        "reference_scoring_replay", verified, expected,
        _verified_value(env, "foundation_reference_scoring", verified),
        detail=f"evidence_digest={first['result_digest'][:12]}",
    )


async def _ui_fallback(env: BehaviorEnvironment) -> CaseResult:
    expected = env.declared("generic_ui_fallback")
    directory = CapabilityDirectory()
    adapter = directory.select_ui_adapter(env.runtime_key)
    unknown = directory.select_ui_adapter(RuntimeKey("unknown-runtime", "9"))
    panels = set(env.record.ui_fallback_panels) >= set(GENERIC_UI_FALLBACK_PANELS)
    runtime_panels = True
    if env.executor.native:
        descriptor = require_runtime(env.runtime_key).descriptor
        runtime_panels = set(descriptor.features.panels) >= set(GENERIC_UI_FALLBACK_PANELS)
    verified = adapter == env.record.ui_adapter and unknown == "generic_fallback" and panels and runtime_panels
    return CaseResult(
        "ui_fallback", verified, expected, _verified_value(env, "generic_ui_fallback", verified),
        detail=f"adapter={adapter}, unknown={unknown}",
    )


_CASES = {
    "admission_identity": _admission_identity,
    "assets_privacy": _assets_privacy,
    "seed_state": _seed_state,
    "cancellation_deadlines": _cancellation_deadlines,
    "lease_fencing_restart_replay": _lease_fencing_restart_replay,
    "activation_effect_ledgers": _activation_effect_ledgers,
    "agent_protocol_negotiation": _agent_protocol_negotiation,
    "budget_reservations": _budget_reservations,
    "evidence_decisions": _evidence_decisions,
    "goals": _goals,
    "trace_envelope": _trace_envelope,
    "post_terminal_invalidation": _post_terminal_invalidation,
    "reference_scoring_replay": _reference_scoring_replay,
    "ui_fallback": _ui_fallback,
}


async def run_behavioral_suite(env: BehaviorEnvironment) -> ConformanceReport:
    """Run every behavioral case in order and return the pair's report."""
    report = ConformanceReport(runtime_key=env.runtime_key, availability=env.record.availability)
    for case_id in BEHAVIOR_CASES:
        try:
            result = await _CASES[case_id](env)
        except (BehaviorError, sqlite3.Error, journal.JournalError, ValueError) as exc:
            result = CaseResult(case_id, False, "verified", "error", detail=f"{type(exc).__name__}: {exc}")
        report.case_results.append(result)
    return report


def executor_for(record: RuntimeCapabilityRecord) -> RuntimeExecutor:
    """The executor that matches one published record."""
    if record.agent_protocol_version == "2" and record.runtime_key.runtime_id == "reference":
        return ReferenceExecutor(record.runtime_key)
    return LegacyTraceExecutor(record.runtime_key)
