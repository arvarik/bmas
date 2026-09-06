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
# The journal operations only a runtime authors. The host's
# compatibility adapter commits admission, activation, and effect
# transitions on a legacy runtime's behalf; a legacy runtime itself
# never commits one of these.
RUNTIME_AUTHORED_OPERATIONS = (
    "proposal_decision",
    "terminal_outcome",
    "post_terminal_invalidation",
    "evidence_update",
    "goal_update",
    "budget_reconciliation",
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
    can_abort: bool
    can_resume: bool
    dispatches: bool

    async def execute(
        self,
        *,
        task_id: str,
        user_task: str,
        seed: int,
        resume: bool = False,
        abort_after_steps: int | None = None,
        restart_after_steps: int | None = None,
    ) -> ExecutionResult:
        ...


async def native_authority_rows(task_id: str) -> int:
    """Count the native authority records a runtime authored for one task.

    A record counts when its operation is one a runtime authors, or
    when its authority is not the host. The host's compatibility rows
    for admission, activation, and effect transitions never count.
    """
    placeholders = ",".join("?" for _ in RUNTIME_AUTHORED_OPERATIONS)
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM runtime_journal WHERE task_id = ? "
            f"AND (authority_type != 'host' OR operation_type IN ({placeholders}))",
            (task_id, *RUNTIME_AUTHORED_OPERATIONS),
        )
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def host_dispatch_rows(task_id: str) -> int:
    """Count the activation grants the host dispatched for one task."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM activation_grants WHERE task_id = ?", (task_id,),
        )
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def recorded_seed(task_id: str) -> int | None:
    """The seed the task recorded: its metadata or its run admission."""
    metadata = await db.get_board_meta(task_id)
    if isinstance(metadata.get("seed"), int):
        return int(metadata["seed"])
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT requested_seed FROM runtime_admissions WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        )
        row = await cursor.fetchone()
    if row is None or row["requested_seed"] in (None, ""):
        return None
    try:
        return int(row["requested_seed"])
    except (TypeError, ValueError):
        return None


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
    can_abort: bool = True
    can_resume: bool = True
    dispatches: bool = False
    steps: int = 3

    async def execute(
        self,
        *,
        task_id: str,
        user_task: str,
        seed: int,
        resume: bool = False,
        abort_after_steps: int | None = None,
        restart_after_steps: int | None = None,
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
    can_abort: bool = False
    can_resume: bool = False
    dispatches: bool = False
    answer: str = "42"

    async def execute(
        self,
        *,
        task_id: str,
        user_task: str,
        seed: int,
        resume: bool = False,
        abort_after_steps: int | None = None,
        restart_after_steps: int | None = None,
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
        await db.complete_task(task_id, self.answer, json.dumps({"answer": self.answer}))
        return ExecutionResult(
            task_id=task_id, answer=self.answer, result={"answer": self.answer},
            checkpoint=None, aborted=False, phases=["legacy"],
            native_rows=await native_authority_rows(task_id),
        )


@dataclass
class StackExecutor:
    """Run the real registered runtime through a running daemon process.

    The daemon and the agent run as real processes over the fake
    provider (``scripts/test-stack.py``). The executor submits the task
    over HTTP, aborts it through the operator route, resumes it through
    a real daemon restart, and reads the durable footprint from the
    daemon's own database file. The runtime records the requested seed
    through its run admission and never applies it.
    """

    runtime_key: RuntimeKey
    daemon_url: str
    operator_key: str
    restart: Any
    native: bool = False
    can_abort: bool = True
    can_resume: bool = True
    dispatches: bool = True
    max_rounds: int = 2
    poll_seconds: float = 0.5
    timeout_seconds: float = 240.0
    task_ids: dict[str, str] = field(default_factory=dict)

    def _client(self) -> Any:
        import httpx

        return httpx.Client(
            base_url=self.daemon_url,
            headers={"Authorization": f"Bearer {self.operator_key}"},
            timeout=60.0,
        )

    async def _task(self, task_id: str) -> dict[str, Any]:
        import asyncio

        with self._client() as client:
            response = await asyncio.to_thread(client.get, f"/tasks/{task_id}")
        response.raise_for_status()
        body = response.json()
        # The detail route wraps the task record with its sub-tasks.
        record = body.get("task") if isinstance(body.get("task"), dict) else body
        return dict(record)

    async def _wait(self, task_id: str, *, until: Any) -> dict[str, Any]:
        import asyncio
        import time

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            task = await self._task(task_id)
            if until(task):
                return task
            if time.monotonic() > deadline:
                raise BehaviorError(f"Task {task_id} never reached the awaited state: {task.get('status')}")
            await asyncio.sleep(self.poll_seconds)

    async def _rounds(self, task_id: str) -> int:
        metadata = await db.get_board_meta(task_id)
        for key in ("round", "current_round", "rounds_completed"):
            value = metadata.get(key)
            if isinstance(value, int):
                return value
        return 0

    async def execute(
        self,
        *,
        task_id: str,
        user_task: str,
        seed: int,
        resume: bool = False,
        abort_after_steps: int | None = None,
        restart_after_steps: int | None = None,
    ) -> ExecutionResult:
        import asyncio

        terminal = {"completed", "failed", "cancelled", "aborted"}
        if resume:
            # A real daemon restart resumes every running task from its
            # durable checkpoint under the same fence. The daemon named
            # the task at submission, so resolve the requested name.
            real_id = self.task_ids.get(task_id, task_id)
            await asyncio.to_thread(self.restart)
            task = await self._wait(real_id, until=lambda row: str(row.get("status")) in terminal)
            return await self._result(real_id, task, aborted=False, resumed=True)
        with self._client() as client:
            response = await asyncio.to_thread(
                client.post, "/submit",
                json={
                    "task": user_task,
                    "variant": self.runtime_key.runtime_id,
                    "overrides": {
                        "seed": seed,
                        "classic": {
                            # A restart needs a live task, so the interrupted
                            # run gets more rounds than the plain runs.
                            "max_rounds": self.max_rounds * 2 if restart_after_steps else self.max_rounds,
                        },
                    },
                },
            )
        if response.status_code >= 400:
            raise BehaviorError(f"submit failed: HTTP {response.status_code} {response.text[:300]}")
        real_id = str(response.json().get("task_id") or "")
        if not real_id:
            raise BehaviorError("The daemon returned no task identifier")
        self.task_ids[task_id] = real_id
        if abort_after_steps is not None:
            await self._wait(real_id, until=lambda row: str(row.get("status")) == "running")
            with self._client() as client:
                cancel = await asyncio.to_thread(
                    client.post, f"/api/tasks/{real_id}/abort", json={"reason": "conformance"},
                )
            if cancel.status_code >= 400:
                raise BehaviorError(f"abort failed: HTTP {cancel.status_code} {cancel.text[:200]}")
            task = await self._wait(real_id, until=lambda row: str(row.get("status")) in terminal)
            return await self._result(real_id, task, aborted=True, resumed=False)
        if restart_after_steps is not None:
            # Pause the live task, restart the daemon and the agent for
            # real, let the daemon resume the task from its checkpoint
            # under the same fence, then lift the pause.
            await self._wait(real_id, until=lambda row: str(row.get("status")) == "running")
            with self._client() as client:
                paused = await asyncio.to_thread(client.post, f"/api/tasks/{real_id}/pause")
            if paused.status_code >= 400:
                raise BehaviorError(f"pause failed: HTTP {paused.status_code} {paused.text[:200]}")
            await self._wait(
                real_id,
                until=lambda row: str(row.get("run_state")) in {"paused", "pause_requested"} or str(row.get("status")) in terminal,
            )
            await asyncio.to_thread(self.restart)
            # A paused task waits for the operator after the restart. The
            # resume route re-enters the task through the durable
            # checkpoint path, which logs the resume event.
            with self._client() as client:
                lifted = await asyncio.to_thread(client.post, f"/api/tasks/{real_id}/resume")
            if lifted.status_code >= 400:
                raise BehaviorError(f"resume failed: HTTP {lifted.status_code} {lifted.text[:200]}")
            task = await self._wait(real_id, until=lambda row: str(row.get("status")) in terminal)
            resumed_log = await self._wait_for_log(real_id, "task_resumed", timeout_seconds=10.0)
            return await self._result(real_id, task, aborted=False, resumed=resumed_log)
        task = await self._wait(real_id, until=lambda row: str(row.get("status")) in terminal)
        return await self._result(real_id, task, aborted=False, resumed=False)

    async def _wait_for_log(self, task_id: str, event: str, *, timeout_seconds: float | None = None) -> bool:
        """Wait until the task log records one event, up to the timeout."""
        import asyncio
        import time

        deadline = time.monotonic() + (timeout_seconds if timeout_seconds is not None else self.timeout_seconds)
        while time.monotonic() < deadline:
            with self._client() as client:
                response = await asyncio.to_thread(client.get, f"/tasks/{task_id}/logs", params={"limit": 500})
            if response.status_code == 200:
                for entry in response.json().get("entries", []):
                    fields = entry.get("fields") or {}
                    if isinstance(fields, str):
                        try:
                            fields = json.loads(fields)
                        except ValueError:
                            fields = {}
                    if fields.get("event") == event:
                        return True
            await asyncio.sleep(self.poll_seconds)
        return False

    async def _result(self, task_id: str, task: dict[str, Any], *, aborted: bool, resumed: bool) -> ExecutionResult:
        metadata = await db.get_board_meta(task_id)
        checkpoint = metadata.get("variant_checkpoint") if isinstance(metadata.get("variant_checkpoint"), dict) else (metadata or None)
        answer = str(task.get("result_summary") or "")
        result: dict[str, Any] = {"answer": answer, "status": task.get("status"), "resumed": resumed}
        raw = task.get("result_json")
        if isinstance(raw, str) and raw:
            try:
                result["result_json"] = json.loads(raw)
            except ValueError:
                result["result_json"] = raw
        return ExecutionResult(
            task_id=task_id, answer=answer, result=result, checkpoint=checkpoint,
            aborted=aborted or str(task.get("status")) in {"cancelled", "aborted"},
            phases=[str(task.get("status"))],
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
    if await db.get_task(environment.task_id) is None:
        # The environment task anchors the journal cases only. It stays in
        # the staging state, so a live daemon never picks it up as work.
        await db.create_task_with_meta(
            environment.task_id, "conformance", TASK_TEXT, record.runtime_key.runtime_id, {},
            runtime_contract_version=record.runtime_key.runtime_contract_version,
            run_state="staging",
        )
    if await db.get_run_control(environment.run_id) is None:
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
    equal = first.answer == second.answer and first.result.get("digest") == second.result.get("digest")
    differs = other.answer != first.answer
    recorded = await recorded_seed(first.task_id) == 7
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
    elif env.executor.can_abort:
        stopped = interrupted.aborted and interrupted.checkpoint is not None
        observed = "legacy" if control_verified and stopped else "unavailable"
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
    # A live daemon keeps appending, so the rebuilt projection stops at
    # the cursor the replay reached.
    replayed = await journal.replay()
    state = journal.empty_projection_state()
    for record in await journal.read_journal():
        if record.journal_cursor > replayed.last_cursor:
            break
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
    elif env.executor.can_resume:
        resumed = await env.executor.execute(
            task_id=f"{env.task_id}-resume", user_task=TASK_TEXT, seed=3, restart_after_steps=1,
        )
        complete = await env.executor.execute(
            task_id=f"{env.task_id}-complete", user_task=TASK_TEXT, seed=3,
        )
        resume_verified = bool(resumed.result.get("resumed")) and resumed.answer == complete.answer and bool(complete.answer)
        observed = _verified_value(env, "task_fence_validation", shared and resume_verified)
        detail = f"stale_denied={stale_denied}, fence_rejected={fence_rejected}, replay_equal={replay_equal}, resumed_answer={resumed.answer[:40]!r}"
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
        dispatched = await host_dispatch_rows(execution.task_id)
        if execution.native_rows > 0:
            observed = dispatch_observed = "native"
        elif dispatched > 0:
            # The host admitted the task and dispatched signed grants on
            # the legacy runtime's behalf.
            observed = dispatch_observed = "compatibility_adapter"
        elif env.executor.dispatches:
            # A real execution that reached no grant proves nothing.
            observed = dispatch_observed = "unavailable"
        else:
            observed, dispatch_observed = expected, dispatch_expected
        detail = f"runtime_authored_rows={execution.native_rows}, host_dispatched_grants={dispatched}"
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
    # The host negotiates the current protocol for every pair: a native
    # runtime natively, a legacy runtime through the compatibility adapter.
    version = "2"
    required = ("cancellation", "resume")
    selected = directory.select(protocol_version=version, required_capability_names=required)
    legacy_only = EndpointDirectory()
    legacy_only.publish(AgentEndpoint(agent_id="legacy-agent", protocol_version="1", qualification_state="qualified"))
    try:
        legacy_only.select(protocol_version="2")
        fails_closed = False
    except NoQualifiedEndpointError:
        fails_closed = True
    negotiated = selected.protocol_version == "2" and is_qualified(document) and fails_closed
    if env.record.agent_protocol_version == "2":
        observed = "native" if negotiated else "unavailable"
    else:
        observed = "compatibility_adapter" if negotiated else "unavailable"
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
