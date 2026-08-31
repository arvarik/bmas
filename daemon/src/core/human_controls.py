"""Foundation Stage 0C: authenticated human controls.

Every control operation authenticates its actor and appends one audit
record with the prior state, the new state, the runtime pair, the task
fence, and the journal cursor. A control never changes an admitted
specification in place: an ordinary settings edit affects new run
admissions only, and a reroute creates a successor run without
touching the current admission or runtime pair.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import database as db

if TYPE_CHECKING:
    from core.run_contracts import RunLedger, RunRecord
    from core.variants import RuntimeKey

DatabaseTimeSource = Callable[[], Awaitable[str]]

HUMAN_CONTROL_OPERATIONS = (
    "pause",
    "resume",
    "cancel",
    "reroute",
    "approve",
    "waive",
)


class HumanControlError(ValueError):
    """A human control operation was rejected."""


class HumanControlAuthenticationError(HumanControlError):
    """The actor is not an authenticated control authority."""


@dataclass(frozen=True)
class HumanControlRecord:
    """One immutable audit record of one human control operation."""

    control_id: str
    operation: str
    actor_id: str
    reason: str
    prior_state: str
    new_state: str
    runtime_key: RuntimeKey
    task_fence: str
    journal_cursor: int
    recorded_at: str


class HumanControlService:
    """Authenticated pause, cancel, reroute, approve, waive, and resume."""

    def __init__(
        self,
        *,
        run_ledger: RunLedger,
        authorized_actor_ids: frozenset[str],
        database_time: DatabaseTimeSource,
    ) -> None:
        self._runs = run_ledger
        self._authorized = authorized_actor_ids
        self._database_time = database_time
        self._audit: list[HumanControlRecord] = []

    @property
    def audit_journal(self) -> list[HumanControlRecord]:
        return list(self._audit)

    def _authenticate(self, actor_id: str) -> None:
        if actor_id not in self._authorized:
            raise HumanControlAuthenticationError(
                f"Actor {actor_id!r} holds no control authority"
            )

    async def _record(
        self,
        *,
        operation: str,
        actor_id: str,
        reason: str,
        prior_state: str,
        new_state: str,
        run: RunRecord,
        task_fence: str,
    ) -> HumanControlRecord:
        record = HumanControlRecord(
            control_id=f"control-{uuid.uuid4()}",
            operation=operation,
            actor_id=actor_id,
            reason=reason,
            prior_state=prior_state,
            new_state=new_state,
            runtime_key=run.runtime_key,
            task_fence=task_fence,
            journal_cursor=len(self._audit),
            recorded_at=await self._database_time(),
        )
        self._audit.append(record)
        return record

    async def _control_row(self, run_id: str) -> dict[str, Any]:
        control = await db.get_run_control(run_id)
        if control is None:
            raise HumanControlError(f"Run {run_id} has no control row")
        return control

    async def pause(
        self, run_id: str, *, actor_id: str, reason: str,
    ) -> HumanControlRecord:
        self._authenticate(actor_id)
        run = self._runs.get(run_id)
        control = await self._control_row(run_id)
        await db.set_run_pause_state(run_id, True)
        return await self._record(
            operation="pause",
            actor_id=actor_id,
            reason=reason,
            prior_state=str(control["pause_state"]),
            new_state="paused",
            run=run,
            task_fence=str(control["task_fence"]),
        )

    async def resume(
        self, run_id: str, *, actor_id: str, reason: str,
    ) -> HumanControlRecord:
        self._authenticate(actor_id)
        run = self._runs.get(run_id)
        control = await self._control_row(run_id)
        await db.set_run_pause_state(run_id, False)
        return await self._record(
            operation="resume",
            actor_id=actor_id,
            reason=reason,
            prior_state=str(control["pause_state"]),
            new_state="active",
            run=run,
            task_fence=str(control["task_fence"]),
        )

    async def cancel(
        self, run_id: str, *, actor_id: str, reason: str,
    ) -> HumanControlRecord:
        self._authenticate(actor_id)
        run = self._runs.get(run_id)
        control = await self._control_row(run_id)
        await db.request_run_cancellation_control(run_id)
        return await self._record(
            operation="cancel",
            actor_id=actor_id,
            reason=reason,
            prior_state=str(control["cancellation_state"]),
            new_state="requested",
            run=run,
            task_fence=str(control["task_fence"]),
        )

    async def reroute(
        self,
        run_id: str,
        *,
        actor_id: str,
        reason: str,
        runtime_key: RuntimeKey,
    ) -> tuple[HumanControlRecord, RunRecord]:
        """Create one successor run for an active run.

        The current run keeps its immutable runtime pair and admission
        record; the successor carries the reroute lineage.
        """
        self._authenticate(actor_id)
        run = self._runs.get(run_id)
        control = await self._control_row(run_id)
        prior_state = run.state.value
        successor = self._runs.reroute(run_id, runtime_key=runtime_key)
        record = await self._record(
            operation="reroute",
            actor_id=actor_id,
            reason=reason,
            prior_state=prior_state,
            new_state=run.state.value,
            run=run,
            task_fence=str(control["task_fence"]),
        )
        return record, successor

    async def approve(
        self, run_id: str, *, actor_id: str, reason: str, subject: str,
    ) -> HumanControlRecord:
        self._authenticate(actor_id)
        run = self._runs.get(run_id)
        control = await self._control_row(run_id)
        return await self._record(
            operation="approve",
            actor_id=actor_id,
            reason=f"{subject}: {reason}",
            prior_state="pending",
            new_state="approved",
            run=run,
            task_fence=str(control["task_fence"]),
        )

    async def waive(
        self, run_id: str, *, actor_id: str, reason: str, subject: str,
    ) -> HumanControlRecord:
        self._authenticate(actor_id)
        run = self._runs.get(run_id)
        control = await self._control_row(run_id)
        return await self._record(
            operation="waive",
            actor_id=actor_id,
            reason=f"{subject}: {reason}",
            prior_state="pending",
            new_state="waived",
            run=run,
            task_fence=str(control["task_fence"]),
        )
