"""Foundation Stage 0C: the complete fenced runtime service boundary.

Every mutating service validates the live run authority inside the
durable run-control row before it commits: the lease owner, the fence,
the lease expiry, the cancellation state, and the deadline. A stale
fence, an expired lease, a cancellation, a deadline, or a clock fault
rejects the mutation.

The ledgers here are in-memory reference authorities behind the
disabled foundation gates; the storage authority stage binds the same
service contracts to the durable journal. The run-control row itself
is durable already.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import database as db

if TYPE_CHECKING:
    from core.asset_store import ArtifactStore, AssetCatalog
    from core.run_context import RunContext
    from core.run_contracts import (
        InvalidationService,
        OutcomeLedger,
        RunLedger,
    )

DatabaseTimeSource = Callable[[], Awaitable[str]]
ReservationValidator = Callable[[str], Awaitable[bool]]


class AuthorityError(PermissionError):
    """The live run authority rejected one mutation."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"The run authority rejected the mutation: {reason}")
        self.reason = reason


class LeaseAuthorityError(PermissionError):
    """Only the scheduler acquires, renews, or transfers task leases."""


async def _default_database_time() -> str:
    return await db.database_utc_now()


@dataclass
class FencedAuthority:
    """One lease holder's identity against the durable run-control row."""

    run_id: str
    lease_owner: str
    lease_fence: str
    database_time: DatabaseTimeSource = _default_database_time

    async def authorize(self, *, deny_paused: bool = False) -> dict:
        """Validate the live authority row or raise ``AuthorityError``."""
        decision = await db.check_run_authority(
            self.run_id,
            self.lease_owner,
            self.lease_fence,
            deny_paused=deny_paused,
            database_time=await self.database_time(),
        )
        if not decision["authorized"]:
            raise AuthorityError(str(decision["reason"]))
        return decision


@dataclass
class LedgerEntry:
    """One committed reference-ledger entry."""

    entry_id: str
    run_id: str
    kind: str
    payload: dict[str, Any]
    control_version: int
    database_time: str


class _FencedLedger:
    """Shared fenced commit path for the reference ledgers."""

    def __init__(self, authority: FencedAuthority, kind: str) -> None:
        self._authority = authority
        self._kind = kind
        self.entries: list[LedgerEntry] = []

    async def commit(
        self, payload: dict[str, Any], *, deny_paused: bool = False,
    ) -> LedgerEntry:
        decision = await self._authority.authorize(deny_paused=deny_paused)
        entry = LedgerEntry(
            entry_id=f"{self._kind}-{uuid.uuid4()}",
            run_id=self._authority.run_id,
            kind=self._kind,
            payload=dict(payload),
            control_version=int(decision["control_version"]),
            database_time=str(decision["database_time"]),
        )
        self.entries.append(entry)
        return entry


class RuntimeUnitOfWork:
    """Commit state changes and terminal outcomes under the live fence."""

    def __init__(
        self,
        authority: FencedAuthority,
        run_ledger: RunLedger,
        outcome_ledger: OutcomeLedger,
    ) -> None:
        self._ledger = _FencedLedger(authority, "state-change")
        self._authority = authority
        self._runs = run_ledger
        self._outcomes = outcome_ledger

    @property
    def committed_changes(self) -> list[LedgerEntry]:
        return list(self._ledger.entries)

    async def commit_state_change(
        self, context: RunContext, change: dict[str, Any],
    ) -> LedgerEntry:
        return await self._ledger.commit(
            {"task_id": context.task_id, "change": change},
        )

    async def commit_terminal_outcome(
        self, context: RunContext, reason_code: str,
    ) -> Any:
        """Commit the one host terminal outcome under the live fence."""
        await self._authority.authorize()
        return self._outcomes.record_outcome(context.run_id, reason_code)


class TaskLeaseGuard:
    """Lease authority. The scheduler alone transfers leases.

    A runtime handle inspects lease validity only; the acquisition,
    renewal, release, and transfer paths raise for a non-scheduler
    caller.
    """

    def __init__(
        self,
        authority: FencedAuthority,
        *,
        scheduler: bool,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        self._authority = authority
        self._scheduler = scheduler
        self._ttl = lease_ttl_seconds

    def _require_scheduler(self) -> None:
        if not self._scheduler:
            raise LeaseAuthorityError(
                "Only the scheduler holds lease transfer authority"
            )

    async def acquire(self) -> bool:
        self._require_scheduler()
        return await db.acquire_run_lease(
            self._authority.run_id,
            self._authority.lease_owner,
            self._authority.lease_fence,
            self._ttl,
            database_time=await self._authority.database_time(),
        )

    async def renew(self) -> bool:
        self._require_scheduler()
        return await db.renew_run_lease(
            self._authority.run_id,
            self._authority.lease_owner,
            self._authority.lease_fence,
            self._ttl,
            database_time=await self._authority.database_time(),
        )

    async def release(self) -> bool:
        self._require_scheduler()
        return await db.release_run_lease(
            self._authority.run_id,
            self._authority.lease_owner,
            self._authority.lease_fence,
        )

    async def inspect(self) -> dict[str, Any]:
        """Report live lease validity without transfer authority."""
        control = await db.get_run_control(self._authority.run_id)
        if control is None:
            return {"valid": False, "reason": "unknown_run"}
        valid = (
            control["lease_owner"] == self._authority.lease_owner
            and control["lease_fence"] == self._authority.lease_fence
            and not control["lease_expired"]
        )
        return {"valid": valid, "lease_owner": control["lease_owner"]}


class ActivationLedger:
    """Dispatch and transition activations under the live fence."""

    def __init__(self, authority: FencedAuthority) -> None:
        self._ledger = _FencedLedger(authority, "activation")

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._ledger.entries)

    async def dispatch(
        self, context: RunContext, activation_id: str, role: str,
    ) -> LedgerEntry:
        # Dispatch of new work also stops while the run pauses.
        return await self._ledger.commit(
            {"activation_id": activation_id, "role": role, "state": "granted"},
            deny_paused=True,
        )

    async def transition(
        self, context: RunContext, activation_id: str, state: str,
    ) -> LedgerEntry:
        return await self._ledger.commit(
            {"activation_id": activation_id, "state": state},
        )


class EffectLedger:
    """Approve new effects under the live fence; reconcile without it.

    Cancellation cannot undo an external effect. An in-flight result
    reconciles into the ledger without an authorized state commit.
    When a reservation validator is attached, no cost-bearing effect
    approves without one valid reservation.
    """

    def __init__(
        self,
        authority: FencedAuthority,
        reservation_validator: ReservationValidator | None = None,
    ) -> None:
        self._authority = authority
        self._ledger = _FencedLedger(authority, "effect")
        self._reservation_validator = reservation_validator
        self.reconciled: list[dict[str, Any]] = []

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._ledger.entries)

    async def approve(
        self,
        context: RunContext,
        effect_id: str,
        action: str,
        *,
        reservation_id: str | None = None,
    ) -> LedgerEntry:
        if self._reservation_validator is not None and (
            reservation_id is None
            or not await self._reservation_validator(reservation_id)
        ):
            raise AuthorityError("reservation")
        return await self._ledger.commit(
            {
                "effect_id": effect_id,
                "action": action,
                "state": "approved",
                "reservation_id": reservation_id,
            },
        )

    async def reconcile(
        self, effect_id: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        """Record one in-flight result without a state commit."""
        record = {
            "effect_id": effect_id,
            "result": dict(result),
            "state": "reconciled",
        }
        self.reconciled.append(record)
        return record


class ExternalEffectService:
    """Execute every external action through the effect ledger.

    Model, tool, environment, import, admission, and judge adapters
    delegate here, so one approval boundary covers them all.
    """

    def __init__(self, effects: EffectLedger) -> None:
        self._effects = effects
        self.executed: list[dict[str, Any]] = []

    async def execute(
        self,
        context: RunContext,
        *,
        kind: str,
        request: dict[str, Any],
        reservation_id: str | None = None,
    ) -> dict[str, Any]:
        effect_id = f"effect-{uuid.uuid4()}"
        entry = await self._effects.approve(
            context, effect_id, kind, reservation_id=reservation_id,
        )
        record = {
            "effect_id": effect_id,
            "kind": kind,
            "request": dict(request),
            "control_version": entry.control_version,
        }
        self.executed.append(record)
        return record


class ModelInvocationService:
    """Delegate model execution to the external effect service."""

    def __init__(self, external_effects: ExternalEffectService) -> None:
        self._external_effects = external_effects

    async def invoke(
        self, context: RunContext, request: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._external_effects.execute(
            context, kind="model", request=request,
        )


class ToolInvocationService:
    """Delegate tool execution to the external effect service."""

    def __init__(self, external_effects: ExternalEffectService) -> None:
        self._external_effects = external_effects

    async def invoke(
        self, context: RunContext, request: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._external_effects.execute(
            context, kind="tool", request=request,
        )


class BudgetLedger:
    """Reserve budget under the live fence."""

    def __init__(self, authority: FencedAuthority) -> None:
        self._ledger = _FencedLedger(authority, "budget")

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._ledger.entries)

    async def reserve(
        self, context: RunContext, amount_usd_millionths: int,
    ) -> LedgerEntry:
        return await self._ledger.commit(
            {"reservation": amount_usd_millionths},
        )


class CheckpointService:
    """Save checkpoints under the live fence."""

    def __init__(self, authority: FencedAuthority) -> None:
        self._ledger = _FencedLedger(authority, "checkpoint")

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._ledger.entries)

    async def save(
        self, context: RunContext, payload: dict[str, Any],
    ) -> LedgerEntry:
        return await self._ledger.commit({"checkpoint": dict(payload)})


class _FencedRecorder:
    """Fenced append-only recorder for projection-style services."""

    def __init__(self, authority: FencedAuthority, kind: str) -> None:
        self._ledger = _FencedLedger(authority, kind)

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._ledger.entries)

    async def record(
        self, context: RunContext, entry: dict[str, Any],
    ) -> LedgerEntry:
        return await self._ledger.commit(dict(entry))


class TraceProjectionService(_FencedRecorder):
    """Record trace projections under the live fence."""


class EvidenceIndexService(_FencedRecorder):
    """Record evidence index entries under the live fence."""


class GoalIndexService(_FencedRecorder):
    """Record goal index entries under the live fence."""


class CancellationService:
    """Drive the durable cancellation states of one run."""

    def __init__(self, authority: FencedAuthority) -> None:
        self._authority = authority

    async def request(self) -> bool:
        return await db.request_run_cancellation_control(
            self._authority.run_id,
        )

    async def acknowledge(self) -> bool:
        return await db.acknowledge_run_cancellation_control(
            self._authority.run_id,
            self._authority.lease_owner,
            self._authority.lease_fence,
        )

    async def finalize(self) -> bool:
        return await db.finalize_run_cancellation_control(
            self._authority.run_id,
        )


class RunControlService:
    """Read and adjust the durable run-control row."""

    def __init__(self, authority: FencedAuthority) -> None:
        self._authority = authority

    async def read(self) -> dict[str, Any] | None:
        return await db.get_run_control(self._authority.run_id)

    async def set_deadline(self, deadline_at: str, policy: str) -> bool:
        return await db.set_run_deadline(
            self._authority.run_id, deadline_at, policy,
        )

    async def set_paused(self, paused: bool) -> bool:
        return await db.set_run_pause_state(self._authority.run_id, paused)

    async def clear_clock_fault(self, new_task_fence: str) -> bool:
        return await db.clear_run_clock_fault(
            self._authority.run_id, new_task_fence,
        )


class DatabaseClock:
    """Authoritative UTC time for durable records and deadlines."""

    def __init__(self, source: DatabaseTimeSource | None = None) -> None:
        self._source = source or _default_database_time

    async def now(self) -> str:
        return await self._source()


class MonotonicClock:
    """Local elapsed time only. A monotonic value never persists."""

    def elapsed_origin(self) -> int:
        return time.monotonic_ns()

    def elapsed_since(self, origin: int) -> int:
        return time.monotonic_ns() - origin


class RuntimeServices(Protocol):
    """The complete shared service boundary behind the fenced context."""

    mutations: RuntimeUnitOfWork
    task_leases: TaskLeaseGuard
    activations: ActivationLedger
    effects: EffectLedger
    external_effects: ExternalEffectService
    budgets: BudgetLedger
    checkpoints: CheckpointService
    assets: AssetCatalog
    artifacts: ArtifactStore
    traces: TraceProjectionService
    evidence: EvidenceIndexService
    goals: GoalIndexService
    controls: Any
    models: ModelInvocationService
    tools: ToolInvocationService
    cancellation: CancellationService
    run_controls: RunControlService
    post_terminal_invalidations: InvalidationService
    database_clock: DatabaseClock
    monotonic_clock: MonotonicClock


@dataclass
class ReferenceRuntimeServices:
    """The reference wiring of the complete fenced service boundary."""

    mutations: RuntimeUnitOfWork
    task_leases: TaskLeaseGuard
    activations: ActivationLedger
    effects: EffectLedger
    external_effects: ExternalEffectService
    budgets: BudgetLedger
    checkpoints: CheckpointService
    assets: AssetCatalog
    artifacts: ArtifactStore
    traces: TraceProjectionService
    evidence: EvidenceIndexService
    goals: GoalIndexService
    controls: Any
    models: ModelInvocationService
    tools: ToolInvocationService
    cancellation: CancellationService
    run_controls: RunControlService
    post_terminal_invalidations: InvalidationService
    database_clock: DatabaseClock
    monotonic_clock: MonotonicClock
    authority: FencedAuthority = field(repr=False, kw_only=True)


def create_runtime_services(
    *,
    run_id: str,
    lease_owner: str,
    lease_fence: str,
    scheduler: bool,
    run_ledger: RunLedger,
    outcome_ledger: OutcomeLedger,
    invalidations: InvalidationService,
    assets: AssetCatalog,
    artifacts: ArtifactStore,
    controls: Any,
    database_time: DatabaseTimeSource | None = None,
    lease_ttl_seconds: float = 30.0,
    reservation_validator: ReservationValidator | None = None,
) -> ReferenceRuntimeServices:
    """Wire the complete reference service boundary for one lease holder."""
    authority = FencedAuthority(
        run_id=run_id,
        lease_owner=lease_owner,
        lease_fence=lease_fence,
        database_time=database_time or _default_database_time,
    )
    effects = EffectLedger(authority, reservation_validator)
    external_effects = ExternalEffectService(effects)
    return ReferenceRuntimeServices(
        mutations=RuntimeUnitOfWork(authority, run_ledger, outcome_ledger),
        task_leases=TaskLeaseGuard(
            authority, scheduler=scheduler, lease_ttl_seconds=lease_ttl_seconds,
        ),
        activations=ActivationLedger(authority),
        effects=effects,
        external_effects=external_effects,
        budgets=BudgetLedger(authority),
        checkpoints=CheckpointService(authority),
        assets=assets,
        artifacts=artifacts,
        traces=TraceProjectionService(authority, "trace"),
        evidence=EvidenceIndexService(authority, "evidence"),
        goals=GoalIndexService(authority, "goal"),
        controls=controls,
        models=ModelInvocationService(external_effects),
        tools=ToolInvocationService(external_effects),
        cancellation=CancellationService(authority),
        run_controls=RunControlService(authority),
        post_terminal_invalidations=invalidations,
        database_clock=DatabaseClock(database_time),
        monotonic_clock=MonotonicClock(),
        authority=authority,
    )
