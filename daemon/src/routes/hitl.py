# /opt/bmas/daemon/src/routes/hitl.py
"""Human-in-the-loop routes for Phase 5 (doc 05 §6, doc 12 §5.1).

Endpoints:
  POST /api/tasks/{taskId}/steer     — boost/retract board entries
  POST /api/tasks/{taskId}/pause     — pause task at round boundary
  POST /api/tasks/{taskId}/resume    — resume a paused task
  POST /api/tasks/{taskId}/directive — inject an operator directive
  POST /api/tasks/{taskId}/approval  — approve/deny a pending run approval
"""

import json
import logging
import os
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

import httpx

logger = logging.getLogger("bmas.daemon")

router = APIRouter(prefix="/api/tasks", tags=["hitl"])


# ── Input Validation ─────────────────────────────────────────────────

# Allow only safe task/entry ID formats: alphanumeric, hyphens, underscores
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_id(value: str, label: str) -> str:
    if not _ID_PATTERN.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: must be 1-64 alphanumeric/hyphen/underscore chars",
        )
    return value


# ── Request Models ───────────────────────────────────────────────────

class SteerRequest(BaseModel):
    action: str  # "boost" | "retract"
    entry_id: str

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ("boost", "retract"):
            raise ValueError("action must be 'boost' or 'retract'")
        return v

    @field_validator("entry_id")
    @classmethod
    def validate_entry_id(cls, v: str) -> str:
        if not _ID_PATTERN.match(v):
            raise ValueError("entry_id must be 1-64 alphanumeric/hyphen/underscore chars")
        return v


class DirectiveRequest(BaseModel):
    body: str  # The directive text (1-2000 chars)

    @field_validator("body")
    @classmethod
    def validate_body(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("directive body cannot be empty")
        if len(v) > 2000:
            raise ValueError("directive body must be ≤2000 characters")
        return v


# ── Steer Endpoint ───────────────────────────────────────────────────

@router.post("/{task_id}/steer")
async def steer_entry(task_id: str, req: SteerRequest):
    """Boost or retract a board entry (doc 05 §6 — HITL steer).

    - boost: multiply entry's salience by 2.0 (clamped to 1.0)
    - retract: set entry status to 'superseded'
    """
    task_id = _validate_id(task_id, "task_id")
    from app import app

    orch = app.state.orchestrator
    bb = orch.bb

    if req.action == "boost":
        # Read current salience from Redis board entries
        try:
            entry_key = f"bmas:board:{task_id}:entries"
            raw = await bb.redis.hget(entry_key, req.entry_id)
            if not raw:
                raise HTTPException(status_code=404, detail="Entry not found")

            entry_data = json.loads(raw)
            current_salience = float(entry_data.get("salience", 0.5))
            new_salience = min(1.0, current_salience * 2.0)
            entry_data["salience"] = new_salience
            await bb.redis.hset(entry_key, req.entry_id, json.dumps(entry_data))

            # Emit status change event
            await bb.publish_event(task_id, "entry_status_changed", {
                "entry_id": req.entry_id,
                "by": "operator",
                "old_salience": current_salience,
                "salience": new_salience,
                "action": "boost",
            })

            logger.info(
                "Steer boost | task=%s entry=%s salience=%.2f→%.2f",
                task_id, req.entry_id, current_salience, new_salience,
            )
            return {"status": "boosted", "entry_id": req.entry_id, "salience": new_salience}

        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Steer boost failed for %s/%s: %s", task_id, req.entry_id, e)
            raise HTTPException(status_code=500, detail="Boost failed") from e

    elif req.action == "retract":
        try:
            entry_key = f"bmas:board:{task_id}:entries"
            raw = await bb.redis.hget(entry_key, req.entry_id)
            if not raw:
                raise HTTPException(status_code=404, detail="Entry not found")

            entry_data = json.loads(raw)
            old_status = entry_data.get("status", "open")
            entry_data["status"] = "superseded"
            await bb.redis.hset(entry_key, req.entry_id, json.dumps(entry_data))

            await bb.publish_event(task_id, "entry_status_changed", {
                "entry_id": req.entry_id,
                "by": "operator",
                "old_status": old_status,
                "status": "superseded",
                "action": "retract",
            })

            logger.info(
                "Steer retract | task=%s entry=%s %s→superseded",
                task_id, req.entry_id, old_status,
            )
            return {"status": "retracted", "entry_id": req.entry_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Steer retract failed for %s/%s: %s", task_id, req.entry_id, e)
            raise HTTPException(status_code=500, detail="Retract failed") from e

    # Unreachable due to validator, but satisfies type checker
    raise HTTPException(status_code=400, detail="Unknown action")


# ── Pause Endpoint ───────────────────────────────────────────────────

@router.post("/{task_id}/pause")
async def pause_task(task_id: str):
    """Pause a running task at the next round boundary (doc 05 §6)."""
    task_id = _validate_id(task_id, "task_id")
    from app import app

    orch = app.state.orchestrator
    bb = orch.bb

    try:
        pause_key = f"bmas:public:pause:{task_id}"
        await bb.redis.set(pause_key, "1", ex=3600)  # TTL 1 hour
        logger.info("Pause requested for task %s", task_id)
        return {"status": "pause_requested", "task_id": task_id}
    except Exception as e:
        logger.warning("Pause failed for %s: %s", task_id, e)
        raise HTTPException(status_code=500, detail="Pause failed") from e


# ── Resume Endpoint ──────────────────────────────────────────────────

@router.post("/{task_id}/resume")
async def resume_task(task_id: str):
    """Resume a paused task (doc 05 §6)."""
    task_id = _validate_id(task_id, "task_id")
    from app import app

    orch = app.state.orchestrator
    bb = orch.bb

    try:
        pause_key = f"bmas:public:pause:{task_id}"
        await bb.redis.delete(pause_key)
        logger.info("Resume requested for task %s", task_id)
        return {"status": "resumed", "task_id": task_id}
    except Exception as e:
        logger.warning("Resume failed for %s: %s", task_id, e)
        raise HTTPException(status_code=500, detail="Resume failed") from e


# ── Directive Endpoint ───────────────────────────────────────────────

@router.post("/{task_id}/directive")
async def inject_directive(task_id: str, req: DirectiveRequest):
    """Inject an operator directive into the hint queue (doc 05 §6).

    The directive will be converted to a board entry at the next
    round boundary by the variant's inject_directives() method.
    """
    task_id = _validate_id(task_id, "task_id")
    from app import app

    orch = app.state.orchestrator
    bb = orch.bb

    try:
        hint_key = f"bmas:public:hints:{task_id}"
        await bb.redis.rpush(hint_key, req.body)
        # TTL to prevent stale hints from accumulating
        await bb.redis.expire(hint_key, 3600)
        logger.info(
            "Directive queued for task %s (%d chars)",
            task_id, len(req.body),
        )
        return {"status": "queued", "task_id": task_id}
    except Exception as e:
        logger.warning("Directive injection failed for %s: %s", task_id, e)
        raise HTTPException(status_code=500, detail="Directive injection failed") from e


# ── Approval Request Model ───────────────────────────────────────────

class ApprovalRequest(BaseModel):
    run_id: str     # The Hermes run ID to approve/deny
    decision: str   # "approve" | "deny"
    reason: str = ""  # Optional reason for the decision

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, v: str) -> str:
        if v not in ("approve", "deny"):
            raise ValueError("decision must be 'approve' or 'deny'")
        return v

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, v: str) -> str:
        if not _ID_PATTERN.match(v):
            raise ValueError("run_id must be 1-64 alphanumeric/hyphen/underscore chars")
        return v


# ── Approval Endpoint (doc 12 §5.1) ──────────────────────────────────

@router.post("/{task_id}/approval")
async def handle_approval(task_id: str, req: ApprovalRequest):
    """Forward an approval decision to the Hermes agent node.

    The daemon looks up which agent node owns the run_id and forwards
    the decision via POST /v1/runs/{run_id}/approval on that node.
    """
    task_id = _validate_id(task_id, "task_id")
    from app import app

    orch = app.state.orchestrator

    # Resolve agent endpoint — try role registry first, fall back to
    # iterating all known agent endpoints
    from config import AGENT_ENDPOINTS, ROLE_REGISTRY

    agent_urls: list[str] = []
    for _role, reg in ROLE_REGISTRY.items():
        agent_urls.extend(reg.get("endpoints", []))
    if not agent_urls:
        agent_urls = list(AGENT_ENDPOINTS.values())

    if not agent_urls:
        raise HTTPException(
            status_code=503,
            detail="No agent nodes configured — cannot forward approval",
        )

    # Forward to all known agent endpoints (the one owning the run_id
    # will handle it; others will 404 harmlessly)
    forwarded = False
    last_error = ""
    gateway_key = os.getenv("HERMES_GATEWAY_KEY", os.getenv("API_SERVER_KEY", ""))

    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in agent_urls:
            try:
                # Forward to the Hermes Gateway via the agent's proxy
                resp = await client.post(
                    f"{url}/v1/runs/{req.run_id}/approval",
                    json={
                        "decision": req.decision,
                        "reason": req.reason,
                    },
                    headers={"Authorization": f"Bearer {gateway_key}"},
                )
                if resp.status_code == 200:
                    forwarded = True
                    logger.info(
                        "Approval %s forwarded | task=%s run=%s → %s",
                        req.decision, task_id, req.run_id, url,
                    )
                    break
                elif resp.status_code == 404:
                    continue  # This node doesn't own this run
                else:
                    last_error = f"{url}: HTTP {resp.status_code}"
            except Exception as e:
                last_error = f"{url}: {e}"
                continue

    if not forwarded:
        logger.warning(
            "Approval forward failed for task=%s run=%s: %s",
            task_id, req.run_id, last_error,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not forward approval to any agent node: {last_error}",
        )

    # Emit approval_request event to SSE so the UI updates
    try:
        await orch.bb.publish_event(task_id, "approval_request", {
            "run_id": req.run_id,
            "decision": req.decision,
            "reason": req.reason,
            "by": "operator",
        })
    except Exception:
        pass

    return {
        "status": f"{req.decision}d",
        "task_id": task_id,
        "run_id": req.run_id,
    }
