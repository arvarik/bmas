# /opt/bmas/daemon/src/routes/hitl.py
"""Human-in-the-loop routes for Phase 5 (doc 05 §6, doc 12 §5.1).

Endpoints:
  POST /api/tasks/{taskId}/steer   — boost/retract board entries
  POST /api/tasks/{taskId}/pause   — pause task at round boundary
  POST /api/tasks/{taskId}/resume  — resume a paused task
  POST /api/tasks/{taskId}/directive — inject an operator directive
"""

import logging
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

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
            import json
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
            import json
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
