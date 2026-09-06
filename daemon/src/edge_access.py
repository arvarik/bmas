"""Authentication and object access at the HTTP edge.

Every route module checked its own mutations against the operator key,
and nothing checked reads, so a task, a run, an artifact, or an
evidence bundle was readable by anyone who reached the daemon. This
module enforces one rule at the edge: when an operator key is
configured, every request outside the public health surface carries
either the operator key or the node key, and the request receives one
``Principal`` the read routes pass to ``access_control.check_access``.

Without a configured operator key the daemon keeps its single-user
behaviour: every request runs as the local operator. The per-route
mutation checks stay in place, so a node key never authorizes an
operator mutation.
"""

from __future__ import annotations

import contextvars
import hmac
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from access_control import AccessDeniedError, ObjectRef, Principal, check_access

DEFAULT_TENANT = "tenant-default"
LOCAL_OPERATOR = Principal(
    principal_id="local-operator",
    tenant_id=DEFAULT_TENANT,
    roles=("operator", "security_administrator", "effect_approver"),
)
# Reads that never need credentials: liveness, readiness, the state
# summary the health probes read, and the interactive schema.
PUBLIC_PREFIXES = (
    "/health",
    "/readiness",
    "/state",
    "/docs",
    "/openapi.json",
    "/redoc",
)

_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "bmas_edge_principal", default=None,
)


def operator_key() -> str:
    from config import BMAS_API_KEY

    return str(BMAS_API_KEY or "")


def node_key() -> str:
    try:
        from config import BMAS_NODE_KEY
    except Exception:  # noqa: BLE001 - configuration absent in some tests
        return ""
    return str(BMAS_NODE_KEY or "")


def _presented_credentials(request: Request) -> list[str]:
    presented: list[str] = []
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        presented.append(authorization[7:].strip())
    for header in ("X-BMAS-API-Key", "X-API-Key"):
        value = request.headers.get(header, "")
        if value:
            presented.append(value.strip())
    return presented


def _matches(presented: list[str], expected: str) -> bool:
    return bool(expected) and any(
        hmac.compare_digest(candidate, expected) for candidate in presented
    )


def principal_for_request(request: Request) -> Principal | None:
    """Resolve the principal one request authenticates as, or None."""
    presented = _presented_credentials(request)
    operator_id = request.headers.get("X-Operator-Id", "").strip() or "operator"
    if _matches(presented, operator_key()):
        return Principal(
            principal_id=operator_id[:128],
            tenant_id=DEFAULT_TENANT,
            roles=("operator", "security_administrator", "effect_approver"),
        )
    if _matches(presented, node_key()):
        node_id = request.headers.get("X-Node-Id", "").strip() or "agent"
        return Principal(
            principal_id=node_id[:128],
            tenant_id=DEFAULT_TENANT,
            roles=("agent_service",),
        )
    return None


def is_public_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?")
               for prefix in PUBLIC_PREFIXES)


async def enforce_edge_access(request: Request, call_next: Any) -> Any:
    """Authenticate one request before any route runs."""
    if not operator_key():
        principal: Principal | None = LOCAL_OPERATOR
    elif is_public_path(request.url.path) or request.method == "OPTIONS":
        principal = principal_for_request(request)
    else:
        principal = principal_for_request(request)
        if principal is None:
            return JSONResponse(
                {"detail": "Missing or invalid daemon credentials"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    token = _current_principal.set(principal)
    try:
        request.state.principal = principal
        return await call_next(request)
    finally:
        _current_principal.reset(token)


def current_principal() -> Principal:
    """The principal of the request in flight, or the local operator."""
    principal = _current_principal.get()
    return principal or LOCAL_OPERATOR


def authorize(
    action: str,
    kind: str,
    object_id: str,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Check one object action for the current principal or raise 403."""
    principal = current_principal()
    reference = ObjectRef(
        kind=kind, tenant_id=tenant_id, object_id=object_id,
        task_id=task_id, run_id=run_id,
    )
    try:
        return check_access(principal, action, reference)
    except AccessDeniedError as error:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: {error.reason}",
        ) from error


def authorize_read(kind: str, object_id: str, **binding: Any) -> dict[str, Any]:
    return authorize("read", kind, object_id, **binding)
