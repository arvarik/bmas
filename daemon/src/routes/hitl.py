# /opt/bmas/daemon/src/routes/hitl.py
"""Human-in-the-loop routes for Phase 5 (doc 05 §6, doc 12 §5.1).

Endpoints:
  POST /api/tasks/{taskId}/steer     — boost/retract board entries
  POST /api/tasks/{taskId}/run-steer — steer a live Hermes run
  POST /api/tasks/{taskId}/pause     — pause task at round boundary
  POST /api/tasks/{taskId}/resume    — resume a paused task
  POST /api/tasks/{taskId}/directive — inject an operator directive
  POST /api/tasks/{taskId}/approval  — answer a pending run approval
"""

import asyncio
import contextlib
import logging
import re
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import AliasChoices, BaseModel, Field, field_validator

import database as db
from core.variants import UnknownVariantError, VariantConfigurationError

logger = logging.getLogger("bmas.daemon")

router = APIRouter(prefix="/api/tasks", tags=["hitl"])


def _authorize_operator(request: Request) -> None:
    """Require operator authentication when the deployment enables it."""
    from auth import require_api_key
    from config import BMAS_API_KEY

    require_api_key(request, BMAS_API_KEY)


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


def _operator_identity(request: Request) -> str:
    """Return one bounded non-secret operator identity."""
    value = request.headers.get("X-Operator-Id", "operator").strip()
    return value[:128] or "operator"


def _action_id(request: Request) -> str:
    """Return the caller idempotency key or create one action identifier."""
    supplied = request.headers.get("X-Idempotency-Key", "").strip()
    if supplied and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", supplied):
        return supplied
    return f"action-{uuid.uuid4().hex}"


async def _record_operator_action(
    task_id: str,
    action: str,
    action_id: str,
    actor: str,
    stage: Literal["requested", "result"],
    *,
    status: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one authoritative operator action event before delivery."""
    await db.append_delivery_event(
        f"task:{task_id}",
        f"operator_action_{stage}",
        {
            "action_id": action_id,
            "action": action,
            "actor": actor,
            "status": status,
            "detail": detail or {},
        },
        task_id=task_id,
        idempotency_key=f"operator:{action_id}:{stage}",
    )


async def _begin_operator_action(
    task_id: str,
    action: str,
    request: Request,
    detail: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any] | None]:
    action_id = _action_id(request)
    actor = _operator_identity(request)
    request_detail = detail or {}
    try:
        record, created = await db.claim_operator_action(
            action_id=action_id,
            task_id=task_id,
            action=action,
            actor=actor,
            detail=request_detail,
        )
        await _record_operator_action(
            task_id,
            action,
            action_id,
            actor,
            "requested",
            status="requested",
            detail=request_detail,
        )
    except Exception as error:
        logger.exception("Operator action journal failed for %s", task_id)
        raise HTTPException(
            status_code=503,
            detail="The action was not sent because its audit record could not be saved",
        ) from error
    return action_id, actor, None if created else record


async def _finish_operator_action(
    task_id: str,
    action: str,
    action_id: str,
    actor: str,
    *,
    status: str,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        await db.finish_operator_action(
            action_id=action_id,
            status=status,
            detail=detail or {},
        )
        await _record_operator_action(
            task_id,
            action,
            action_id,
            actor,
            "result",
            status=status,
            detail=detail,
        )
    except Exception as error:
        logger.exception("Operator action result journal failed for %s", task_id)
        raise HTTPException(
            status_code=503,
            detail="The action completed, but its audit outcome could not be saved",
        ) from error


def _replayed_action(task_id: str, action_id: str, record: dict[str, Any]) -> dict:
    """Return a stable response when an idempotency key already exists."""
    return {
        "status": record.get("status", "requested"),
        "task_id": task_id,
        "action_id": action_id,
        "replayed": True,
        "result": record.get("result_detail"),
    }


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


class AbortRequest(BaseModel):
    reason: str = "operator_request"

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("abort reason cannot be empty")
        if len(value) > 200:
            raise ValueError("abort reason must be at most 200 characters")
        return value


# ── Steer Endpoint ───────────────────────────────────────────────────


@router.post("/{task_id}/steer")
async def steer_entry(task_id: str, req: SteerRequest, request: Request):
    """Boost or retract a board entry (doc 05 §6 — HITL steer).

    - boost: multiply entry's salience by 2.0 (clamped to 1.0)
    - retract: set entry status to 'superseded'
    """
    task_id = _validate_id(task_id, "task_id")
    _authorize_operator(request)
    from app import app

    orch = app.state.orchestrator
    action_id, actor, prior = await _begin_operator_action(
        task_id,
        f"board_{req.action}",
        request,
        {"entry_id": req.entry_id},
    )
    if prior is not None:
        return _replayed_action(task_id, action_id, prior)
    try:
        result = await orch.steer_entry(task_id, req.entry_id, req.action)
        logger.info(
            "Steer %s | task=%s entry=%s",
            req.action,
            task_id,
            req.entry_id,
        )
    except KeyError as exc:
        await _finish_operator_action(
            task_id,
            f"board_{req.action}",
            action_id,
            actor,
            status="rejected",
            detail={"entry_id": req.entry_id, "error": "Entry not found"},
        )
        raise HTTPException(status_code=404, detail="Entry not found") from exc
    except Exception as exc:
        await _finish_operator_action(
            task_id,
            f"board_{req.action}",
            action_id,
            actor,
            status="failed",
            detail={"entry_id": req.entry_id, "error": str(exc)[:1000]},
        )
        logger.warning(
            "Steer %s failed for %s/%s: %s",
            req.action,
            task_id,
            req.entry_id,
            exc,
        )
        raise HTTPException(status_code=500, detail="Steering failed") from exc
    await _finish_operator_action(
        task_id,
        f"board_{req.action}",
        action_id,
        actor,
        status="accepted",
        detail={"entry_id": req.entry_id},
    )
    return result


# ── Pause Endpoint ───────────────────────────────────────────────────


@router.post("/{task_id}/abort", status_code=202)
async def abort_task(task_id: str, req: AbortRequest, request: Request):
    """Stop an active task or mark a queued task for cancellation."""
    task_id = _validate_id(task_id, "task_id")
    _authorize_operator(request)
    from app import app
    from routes.submit import abort_scheduled_task

    orch = app.state.orchestrator
    action_id, actor, prior = await _begin_operator_action(
        task_id, "cancel", request, {"reason": req.reason}
    )
    if prior is not None:
        return _replayed_action(task_id, action_id, prior)
    if not await db.request_task_cancellation(task_id):
        await _finish_operator_action(
            task_id,
            "cancel",
            action_id,
            actor,
            status="rejected",
            detail={"reason": req.reason, "error": "Task is not active"},
        )
        raise HTTPException(status_code=409, detail="The task is not active")
    try:
        await orch.bb.redis.set(
            f"bmas:public:abort:{task_id}",
            req.reason,
            ex=3600,
        )
    except Exception as exc:
        logger.warning("Redis abort marker failed for %s: %s", task_id, exc)

    scheduled = False
    try:
        scheduled = await abort_scheduled_task(task_id, req.reason)
    except Exception as exc:
        logger.warning("Local abort failed for %s: %s", task_id, exc)

    remote_cancelled = 0
    try:
        remote_cancelled = await orch.cancel_remote_task(task_id)
    except Exception as exc:
        logger.warning("Remote abort failed for %s: %s", task_id, exc)

    logger.info("Abort requested for task %s: %s", task_id, req.reason)
    result = {
        "status": "abort_requested",
        "task_id": task_id,
        "scheduled": scheduled,
        "remote_cancelled": remote_cancelled,
    }
    await _finish_operator_action(
        task_id,
        "cancel",
        action_id,
        actor,
        status="accepted",
        detail={
            "reason": req.reason,
            "scheduled": scheduled,
            "remote_cancelled": remote_cancelled,
        },
    )
    return result


# ── Pause Endpoint ───────────────────────────────────────────────────


@router.post("/{task_id}/pause")
async def pause_task(task_id: str, request: Request):
    """Pause a running task at the next round boundary (doc 05 §6)."""
    task_id = _validate_id(task_id, "task_id")
    _authorize_operator(request)
    from app import app

    orch = app.state.orchestrator
    bb = orch.bb
    action_id, actor, prior = await _begin_operator_action(task_id, "pause", request)
    if prior is not None:
        return _replayed_action(task_id, action_id, prior)

    try:
        pause_key = f"bmas:public:pause:{task_id}"
        await bb.redis.set(pause_key, "1", ex=3600)  # TTL 1 hour
        from database import update_run_state

        await update_run_state(task_id, "pause_requested")
        logger.info("Pause requested for task %s", task_id)
    except Exception as e:
        await _finish_operator_action(
            task_id,
            "pause",
            action_id,
            actor,
            status="failed",
            detail={"error": str(e)[:1000]},
        )
        logger.warning("Pause failed for %s: %s", task_id, e)
        raise HTTPException(status_code=500, detail="Pause failed") from e
    await _finish_operator_action(task_id, "pause", action_id, actor, status="accepted")
    return {"status": "pause_requested", "task_id": task_id}


# ── Resume Endpoint ──────────────────────────────────────────────────


@router.post("/{task_id}/resume")
async def resume_task(task_id: str, request: Request):
    """Resume a paused task (doc 05 §6)."""
    task_id = _validate_id(task_id, "task_id")
    _authorize_operator(request)
    from app import app

    orch = app.state.orchestrator
    bb = orch.bb
    action_id, actor, prior = await _begin_operator_action(task_id, "resume", request)
    if prior is not None:
        return _replayed_action(task_id, action_id, prior)
    response_status = "resumed"
    response_detail: dict[str, Any] = {}

    try:
        task = await db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.get("run_state") == "blocked":
            from routes.submit import resume_blocked_task

            resumed = await resume_blocked_task(task_id)
            if not resumed:
                raise HTTPException(
                    status_code=409,
                    detail="The task is no longer blocked",
                )
            logger.info("Blocked task resumed: %s", task_id)
            response_status = "recovery_queued"
            response_detail = {"mode": "recovery"}
        else:
            pause_key = f"bmas:public:pause:{task_id}"
            await bb.redis.delete(pause_key)
            await db.update_run_state(task_id, "running")
            logger.info("Resume requested for task %s", task_id)
    except HTTPException as error:
        await _finish_operator_action(
            task_id,
            "resume",
            action_id,
            actor,
            status="rejected",
            detail={"error": str(error.detail)[:1000]},
        )
        raise
    except asyncio.QueueFull as exc:
        await _finish_operator_action(
            task_id,
            "resume",
            action_id,
            actor,
            status="rejected",
            detail={"error": "Task queue is full"},
        )
        raise HTTPException(
            status_code=429,
            detail="The task queue is full. Retry when capacity is available.",
        ) from exc
    except (UnknownVariantError, VariantConfigurationError) as exc:
        await _finish_operator_action(
            task_id,
            "resume",
            action_id,
            actor,
            status="rejected",
            detail={"error": str(exc)[:1000]},
        )
        raise HTTPException(
            status_code=409,
            detail=f"Blocked task recovery is incompatible: {exc}",
        ) from exc
    except RuntimeError as exc:
        await _finish_operator_action(
            task_id,
            "resume",
            action_id,
            actor,
            status="failed",
            detail={"error": str(exc)[:1000]},
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as e:
        await _finish_operator_action(
            task_id,
            "resume",
            action_id,
            actor,
            status="failed",
            detail={"error": str(e)[:1000]},
        )
        logger.warning("Resume failed for %s: %s", task_id, e)
        raise HTTPException(status_code=500, detail="Resume failed") from e
    await _finish_operator_action(
        task_id,
        "resume",
        action_id,
        actor,
        status="accepted",
        detail=response_detail,
    )
    return {"status": response_status, "task_id": task_id}


# ── Directive Endpoint ───────────────────────────────────────────────


@router.post("/{task_id}/directive")
async def inject_directive(task_id: str, req: DirectiveRequest, request: Request):
    """Inject an operator directive into the hint queue (doc 05 §6).

    The directive will be converted to a board entry at the next
    round boundary by the variant's inject_directives() method.
    """
    task_id = _validate_id(task_id, "task_id")
    _authorize_operator(request)
    from app import app

    orch = app.state.orchestrator
    bb = orch.bb
    action_id, actor, prior = await _begin_operator_action(
        task_id,
        "directive",
        request,
        {"character_count": len(req.body)},
    )
    if prior is not None:
        return _replayed_action(task_id, action_id, prior)

    try:
        hint_key = f"bmas:public:hints:{task_id}"
        await bb.redis.rpush(hint_key, req.body)
        # TTL to prevent stale hints from accumulating
        await bb.redis.expire(hint_key, 3600)
        logger.info(
            "Directive queued for task %s (%d chars)",
            task_id,
            len(req.body),
        )
    except Exception as e:
        await _finish_operator_action(
            task_id,
            "directive",
            action_id,
            actor,
            status="failed",
            detail={"error": str(e)[:1000]},
        )
        logger.warning("Directive injection failed for %s: %s", task_id, e)
        raise HTTPException(status_code=500, detail="Directive injection failed") from e
    await _finish_operator_action(
        task_id,
        "directive",
        action_id,
        actor,
        status="accepted",
        detail={"character_count": len(req.body)},
    )
    return {"status": "queued", "task_id": task_id}


# ── Approval Request Model ───────────────────────────────────────────


class ApprovalRequest(BaseModel):
    run_id: str
    choice: Literal["once", "session", "always", "deny"] = Field(
        validation_alias=AliasChoices("choice", "decision"),
    )
    reason: str = ""

    @field_validator("choice", mode="before")
    @classmethod
    def validate_choice(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = value.strip().lower()
        if value == "approve":
            return "once"
        return value

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, v: str) -> str:
        if not _ID_PATTERN.match(v):
            raise ValueError("run_id must be 1-64 alphanumeric/hyphen/underscore chars")
        return v


class RunSteerRequest(BaseModel):
    run_id: str
    input: str

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            raise ValueError("run_id must be 1-64 alphanumeric/hyphen/underscore chars")
        return value

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("input cannot be empty")
        if len(value) > 10000:
            raise ValueError("input must be at most 10000 characters")
        return value


def _configured_agent_urls() -> list[str]:
    """Return each configured agent URL once, in configuration order."""
    from config import AGENT_ENDPOINTS, ROLE_REGISTRY

    urls: list[str] = []
    for registration in ROLE_REGISTRY.values():
        urls.extend(registration.get("endpoints", []))
    urls.extend(AGENT_ENDPOINTS.values())
    return list(dict.fromkeys(url for url in urls if url))


def _agent_proxy_headers() -> dict[str, str] | None:
    """Return the bearer credential for daemon-to-agent proxy requests."""
    from config import BMAS_EXECUTE_KEY

    if not BMAS_EXECUTE_KEY:
        return None
    return {"Authorization": f"Bearer {BMAS_EXECUTE_KEY}"}


async def _forward_run_action(
    run_id: str,
    path_suffix: str,
    payload: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    """Forward one run action to the agent node that owns the Hermes run."""
    agent_urls = _configured_agent_urls()
    if not agent_urls:
        raise HTTPException(
            status_code=503,
            detail="No agent nodes configured",
        )

    not_found_count = 0
    last_error = "No agent node accepted the request"
    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in agent_urls:
            try:
                response = await client.post(
                    f"{url}/v1/runs/{run_id}/{path_suffix}",
                    json=payload,
                    headers=_agent_proxy_headers(),
                )
            except httpx.HTTPError as exc:
                last_error = f"{url}: {exc}"
                continue

            if response.status_code == 404:
                not_found_count += 1
                continue
            if 200 <= response.status_code < 300:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                return url, data if isinstance(data, dict) else {}
            last_error = f"{url}: HTTP {response.status_code}"

    if not_found_count == len(agent_urls):
        raise HTTPException(
            status_code=404,
            detail=f"Hermes run {run_id} was not found",
        )
    raise HTTPException(
        status_code=502,
        detail=f"No agent node accepted the Hermes run action: {last_error}",
    )


# ── Approval Endpoint (doc 12 §5.1) ──────────────────────────────────


@router.post("/{task_id}/approval")
async def handle_approval(task_id: str, req: ApprovalRequest, request: Request):
    """Forward one approval choice to the Hermes agent node."""
    task_id = _validate_id(task_id, "task_id")
    _authorize_operator(request)
    from app import app

    orch = app.state.orchestrator
    action_id, actor, prior = await _begin_operator_action(
        task_id,
        "approval",
        request,
        {"run_id": req.run_id, "choice": req.choice, "reason": req.reason},
    )
    if prior is not None:
        return _replayed_action(task_id, action_id, prior)
    try:
        agent_url, upstream = await _forward_run_action(
            req.run_id,
            "approval",
            {"choice": req.choice},
        )
    except HTTPException as error:
        await _finish_operator_action(
            task_id,
            "approval",
            action_id,
            actor,
            status="rejected",
            detail={
                "run_id": req.run_id,
                "choice": req.choice,
                "error": str(error.detail)[:1000],
            },
        )
        raise
    logger.info(
        "Approval %s forwarded | task=%s run=%s node=%s",
        req.choice,
        task_id,
        req.run_id,
        agent_url,
    )

    # Emit a response event so the UI clears the pending approval.
    with contextlib.suppress(Exception):
        await orch.bb.publish_event(
            task_id,
            "approval_response",
            {
                "run_id": req.run_id,
                "choice": req.choice,
                "reason": req.reason,
                "by": "operator",
                "status": "responded",
            },
        )

    result = {
        "status": "forwarded",
        "task_id": task_id,
        "run_id": req.run_id,
        "choice": req.choice,
        "resolved": upstream.get("resolved"),
    }
    await _finish_operator_action(
        task_id,
        "approval",
        action_id,
        actor,
        status="accepted",
        detail={
            "run_id": req.run_id,
            "choice": req.choice,
            "resolved": upstream.get("resolved"),
        },
    )
    return result


@router.post("/{task_id}/run-steer")
async def steer_run(task_id: str, req: RunSteerRequest, request: Request):
    """Forward live operator input to an active Hermes run."""
    task_id = _validate_id(task_id, "task_id")
    _authorize_operator(request)
    from app import app

    orch = app.state.orchestrator
    action_id, actor, prior = await _begin_operator_action(
        task_id,
        "run_steer",
        request,
        {"run_id": req.run_id, "character_count": len(req.input)},
    )
    if prior is not None:
        return _replayed_action(task_id, action_id, prior)
    try:
        agent_url, upstream = await _forward_run_action(
            req.run_id,
            "steer",
            {"input": req.input},
        )
    except HTTPException as error:
        await _finish_operator_action(
            task_id,
            "run_steer",
            action_id,
            actor,
            status="rejected",
            detail={
                "run_id": req.run_id,
                "error": str(error.detail)[:1000],
            },
        )
        raise
    logger.info(
        "Run steer forwarded | task=%s run=%s node=%s chars=%d",
        task_id,
        req.run_id,
        agent_url,
        len(req.input),
    )

    with contextlib.suppress(Exception):
        await orch.bb.publish_event(
            task_id,
            "run_steered",
            {
                "run_id": req.run_id,
                "input": req.input,
                "by": "operator",
            },
        )

    result = {
        "status": "accepted",
        "task_id": task_id,
        "run_id": req.run_id,
        "accepted": bool(upstream.get("accepted", True)),
    }
    await _finish_operator_action(
        task_id,
        "run_steer",
        action_id,
        actor,
        status="accepted",
        detail={
            "run_id": req.run_id,
            "accepted": bool(upstream.get("accepted", True)),
        },
    )
    return result
