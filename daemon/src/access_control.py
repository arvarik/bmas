"""Foundation role and object access control.

Every read and write checks the tenant, project, task, run, and
object scope. A role alone never grants cross-tenant access: the
tenant check runs first and a valid foreign identifier changes
nothing. Effect request, approval, and execution authority stay
separated, so the requester cannot solely approve an irreversible
effect or privacy waiver.

Display and persistence redaction follow the security matrix data
classes. A secret value never renders; a sensitive value renders only
for a role with explicit permission; a prohibited value never
persists at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.asset_store import DataClass

ROLES = (
    "task_owner",
    "task_collaborator",
    "read_only_viewer",
    "runtime_service",
    "agent_service",
    "effect_approver",
    "operator",
    "security_administrator",
    "auditor",
    "benchmark_publisher",
)

OBJECT_KINDS = (
    "task",
    "run",
    "artifact",
    "evidence",
    "effect",
    "trace",
    "goal",
    "claim",
    "recovery_item",
    "report",
)

ACTIONS = ("read", "write", "approve", "execute", "erase")

# The roles that can read sensitive fields after redaction rules run.
_SENSITIVE_READ_ROLES = frozenset(
    {"operator", "security_administrator", "auditor", "task_owner"},
)

# Write authority per role, from the security matrix table.
_WRITE_ROLES = frozenset(
    {
        "task_owner",
        "task_collaborator",
        "runtime_service",
        "agent_service",
        "effect_approver",
        "operator",
        "security_administrator",
        "benchmark_publisher",
    },
)


class AccessDeniedError(PermissionError):
    """The access check rejected one action."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Access denied: {reason}")
        self.reason = reason


class SeparationError(PermissionError):
    """Request, approval, and execution authority must stay separate."""


@dataclass(frozen=True)
class Principal:
    """One authenticated principal with its roles and scopes."""

    principal_id: str
    tenant_id: str
    roles: tuple[str, ...]
    project_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()
    operational_scope: tuple[str, ...] = ()
    approval_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = [role for role in self.roles if role not in ROLES]
        if unknown:
            raise ValueError(f"Unknown roles: {unknown}")


@dataclass(frozen=True)
class ObjectRef:
    """One scoped object reference."""

    kind: str
    tenant_id: str
    object_id: str
    project_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in OBJECT_KINDS:
            raise ValueError(f"Unknown object kind: {self.kind!r}")


def _scope_matches(principal: Principal, obj: ObjectRef) -> bool:
    if obj.project_id is not None and principal.project_ids and (
        obj.project_id not in principal.project_ids
    ):
        return False
    if obj.task_id is not None and principal.task_ids and (
        obj.task_id not in principal.task_ids
    ):
        return False
    return not (
        obj.run_id is not None
        and principal.run_ids
        and obj.run_id not in principal.run_ids
    )


def check_access(
    principal: Principal, action: str, obj: ObjectRef,
) -> dict[str, Any]:
    """Authorize one action on one object or fail closed.

    The tenant check runs first: no role grants cross-tenant access.
    The scope check follows, then the role rules.
    """
    if action not in ACTIONS:
        raise AccessDeniedError(f"unknown action {action!r}")
    if principal.tenant_id != obj.tenant_id:
        raise AccessDeniedError("tenant_boundary")
    if not _scope_matches(principal, obj):
        raise AccessDeniedError("object_scope")
    if not principal.roles:
        raise AccessDeniedError("no_role")

    if action == "read":
        if obj.kind == "recovery_item" and not (
            {"operator", "security_administrator", "auditor"}
            & set(principal.roles)
        ):
            raise AccessDeniedError("role_read")
        return {"authorized": True, "roles": list(principal.roles)}

    if action == "write":
        if "auditor" in principal.roles and len(principal.roles) == 1:
            raise AccessDeniedError("auditor_writes_nothing")
        if "read_only_viewer" in principal.roles and (
            len(principal.roles) == 1
        ):
            raise AccessDeniedError("viewer_writes_nothing")
        if not (_WRITE_ROLES & set(principal.roles)):
            raise AccessDeniedError("role_write")
        return {"authorized": True, "roles": list(principal.roles)}

    if action == "approve":
        if obj.kind == "effect" and "effect_approver" not in principal.roles:
            raise AccessDeniedError("approval_requires_effect_approver")
        if obj.kind in ("artifact", "evidence") and (
            "security_administrator" not in principal.roles
        ):
            raise AccessDeniedError(
                "privacy_approval_requires_security_administrator"
            )
        return {"authorized": True, "roles": list(principal.roles)}

    if action == "execute":
        if not ({"runtime_service", "agent_service", "operator"}
                & set(principal.roles)):
            raise AccessDeniedError("role_execute")
        return {"authorized": True, "roles": list(principal.roles)}

    if "security_administrator" not in principal.roles:
        raise AccessDeniedError("erasure_requires_security_administrator")
    return {"authorized": True, "roles": list(principal.roles)}


# ── Separation of request, approval, and execution ───────────────────


@dataclass
class SeparatedDecision:
    """One irreversible effect or privacy waiver decision chain."""

    decision_id: str
    kind: str
    requested_by: str | None = None
    approved_by: str | None = None
    executed_by: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)


class SeparationRegistry:
    """Enforce separated request, approval, and execution authority."""

    def __init__(self) -> None:
        self._decisions: dict[str, SeparatedDecision] = {}

    def request(
        self, decision_id: str, kind: str, principal: Principal,
    ) -> SeparatedDecision:
        if kind not in ("irreversible_effect", "privacy_waiver"):
            raise SeparationError(f"Unknown decision kind: {kind!r}")
        decision = SeparatedDecision(
            decision_id=decision_id,
            kind=kind,
            requested_by=principal.principal_id,
        )
        decision.history.append(
            {"step": "requested", "actor": principal.principal_id},
        )
        self._decisions[decision_id] = decision
        return decision

    def approve(
        self, decision_id: str, principal: Principal,
    ) -> SeparatedDecision:
        decision = self._require(decision_id)
        if decision.requested_by == principal.principal_id:
            raise SeparationError(
                "The requester cannot solely approve an irreversible "
                "effect or privacy waiver"
            )
        if decision.kind == "irreversible_effect" and (
            "effect_approver" not in principal.roles
        ):
            raise SeparationError(
                "Only an effect approver approves an unsafe retry"
            )
        if decision.kind == "privacy_waiver" and (
            "security_administrator" not in principal.roles
        ):
            raise SeparationError(
                "Only a security administrator approves a privacy waiver"
            )
        decision.approved_by = principal.principal_id
        decision.history.append(
            {"step": "approved", "actor": principal.principal_id},
        )
        return decision

    def execute(
        self, decision_id: str, principal: Principal,
    ) -> SeparatedDecision:
        decision = self._require(decision_id)
        if decision.approved_by is None:
            raise SeparationError("The decision lacks its approval")
        if principal.principal_id == decision.approved_by:
            raise SeparationError(
                "The approver cannot also execute the effect"
            )
        decision.executed_by = principal.principal_id
        decision.history.append(
            {"step": "executed", "actor": principal.principal_id},
        )
        return decision

    def _require(self, decision_id: str) -> SeparatedDecision:
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise SeparationError(f"Unknown decision: {decision_id!r}")
        return decision


# ── Role-aware display redaction ─────────────────────────────────────


def redact_for_display(
    record: dict[str, Any],
    classifications: dict[str, DataClass],
    *,
    principal: Principal,
) -> dict[str, Any]:
    """Redact one record for one viewer before display.

    A secret never renders for any role. A prohibited value never
    exists in the record at all. A sensitive value renders only for a
    role with explicit permission; every other viewer sees one
    redaction marker. An advanced view never bypasses this policy.
    """
    can_read_sensitive = bool(
        _SENSITIVE_READ_ROLES & set(principal.roles),
    )
    redacted: dict[str, Any] = {}
    for name, value in record.items():
        data_class = classifications.get(name)
        if data_class is None:
            raise AccessDeniedError(f"field {name!r} has no data class")
        if data_class is DataClass.PROHIBITED:
            continue
        if data_class is DataClass.SECRET:
            redacted[name] = {"redacted": "secret_reference_only"}
            continue
        if data_class is DataClass.SENSITIVE and not can_read_sensitive:
            redacted[name] = {"redacted": "sensitive"}
            continue
        redacted[name] = value
    return redacted


# ── Guarded direct lookups ───────────────────────────────────────────


async def guarded_evidence_lookup(
    principal: Principal, claim_id: str,
) -> dict[str, Any]:
    """Read one claim only after the full object access check.

    A valid identifier alone grants no access; the lookup repeats the
    tenant, scope, and role checks.
    """
    import evidence_service

    claim = await evidence_service.get_claim(claim_id)
    check_access(
        principal,
        "read",
        ObjectRef(
            kind="evidence",
            tenant_id=str(claim["tenant_id"]),
            object_id=claim_id,
            task_id=str(claim["task_id"]),
            run_id=str(claim["run_id"]),
        ),
    )
    return claim


def guarded_artifact_lookup(
    principal: Principal,
    store: Any,
    *,
    tenant_id: str,
    content_digest: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Read one artifact object only after the full access check."""
    check_access(
        principal,
        "read",
        ObjectRef(
            kind="artifact",
            tenant_id=tenant_id,
            object_id=content_digest,
            task_id=task_id,
        ),
    )
    return store.read_object(content_digest)


async def guarded_trace_lookup(
    principal: Principal, *, tenant_id: str, run_id: str,
) -> list[Any]:
    """Read one run's trace projection only after the access check."""
    import typed_indexes

    check_access(
        principal,
        "read",
        ObjectRef(
            kind="trace",
            tenant_id=tenant_id,
            object_id=run_id,
            run_id=run_id,
        ),
    )
    return await typed_indexes.trace_projection(run_id)
