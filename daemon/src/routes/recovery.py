"""Operator routes for the Recovery Center.

The Recovery Center service implemented every queue and every action,
and nothing exposed them, so an operator could not reach an unknown
effect, a stale lease, or a dead letter without a database session.
These routes list the queues and run the actions as the authenticated
operator principal, and every action journals its control decision
through the service.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

import edge_access
import recovery_center
from access_control import AccessDeniedError, Principal
from auth import require_api_key
from config import BMAS_API_KEY

router = APIRouter(prefix="/recovery-center", tags=["recovery-center"])


class RecoveryActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    arguments: dict[str, Any] = {}
    # A separate approver identity for the actions that separate the
    # requester from the approver.
    approver_id: str | None = None


def _principal(request: Request, *, approver_id: str | None = None) -> Principal:
    principal = edge_access.current_principal()
    if approver_id:
        return Principal(
            principal_id=approver_id[:128],
            tenant_id=principal.tenant_id,
            roles=("effect_approver", "operator"),
        )
    return principal


@router.get("/queues")
async def list_recovery_queues_endpoint(request: Request):
    require_api_key(request, BMAS_API_KEY)
    queues = await recovery_center.list_all_queues(principal=_principal(request))
    counts = {name: len(items) for name, items in queues.items()}
    return {
        "queues": queues,
        "counts": counts,
        "alerts": recovery_center.evaluate_alerts(counts),
        "thresholds": dict(recovery_center.DEFAULT_THRESHOLDS),
        "actions": list(recovery_center.RECOVERY_ACTIONS),
    }


@router.get("/queues/{queue}")
async def list_recovery_queue_endpoint(request: Request, queue: str):
    require_api_key(request, BMAS_API_KEY)
    if queue not in recovery_center.RECOVERY_QUEUES:
        raise HTTPException(status_code=404, detail="Unknown recovery queue")
    items = await recovery_center.list_queue(queue, principal=_principal(request))
    return {"queue": queue, "items": items, "count": len(items)}


_ACTIONS = {
    "reconcile_by_lookup": recovery_center.reconcile_by_lookup,
    "retry_safe_effect": recovery_center.retry_safe_effect,
    "request_unsafe_retry": recovery_center.request_unsafe_retry,
    "approve_unsafe_retry": recovery_center.approve_unsafe_retry,
    "reclaim_stale_lease": recovery_center.reclaim_stale_lease,
    "replay_outbox_record": recovery_center.replay_outbox_record,
    "run_wal_checkpoint": recovery_center.run_wal_checkpoint,
    "pause_new_work": recovery_center.pause_new_work,
}


@router.post("/actions/{action}")
async def run_recovery_action_endpoint(
    request: Request, action: str, payload: RecoveryActionInput,
):
    require_api_key(request, BMAS_API_KEY)
    handler = _ACTIONS.get(action)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown or non-routable recovery action: {action}",
        )
    principal = _principal(request, approver_id=payload.approver_id)
    try:
        outcome = await handler(
            principal=principal, run_id=payload.run_id, **payload.arguments,
        )
    except AccessDeniedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except TypeError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (ValueError, LookupError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if hasattr(outcome, "_asdict"):
        outcome = outcome._asdict()
    elif hasattr(outcome, "__dataclass_fields__"):
        import dataclasses

        outcome = dataclasses.asdict(outcome)
    return {"action": action, "run_id": payload.run_id, "outcome": outcome}
