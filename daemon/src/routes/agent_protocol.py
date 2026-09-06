"""The agent protocol routes: keys, acknowledgements, effect grants, receipts, and dispatch.

An agent authenticates with the node key. It registers its public key,
reads the daemon grant keys, posts the exact signed acknowledgement
bytes, requests one effect grant per nested provider or tool call, and
posts signed attempt receipts. An operator dispatches one activation
to a qualified agent and reads the durable activation state.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import activation_service as activations
import agent_dispatch
import agent_protocol as protocol
import database as db
import edge_access
import effect_service as effects
import protocol_keys
import runtime_journal as journal
from core.signing import SigningError

router = APIRouter(prefix="/agent-protocol", tags=["agent-protocol"])
NODE_ROLE = "agent_service"


def _require_node_or_operator() -> None:
    principal = edge_access.current_principal()
    if NODE_ROLE in principal.roles or "operator" in principal.roles:
        return
    raise HTTPException(status_code=403, detail="The agent protocol routes accept the node key or the operator key")


def _require_operator() -> None:
    principal = edge_access.current_principal()
    if "operator" not in principal.roles:
        raise HTTPException(status_code=403, detail="This route accepts the operator key only")


class AgentKeyRegistration(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    key_id: str = Field(..., min_length=1, max_length=128)
    public_key_hex: str = Field(..., min_length=64, max_length=64)


class EffectGrantRequest(BaseModel):
    run_id: str
    parent_grant_id: str
    kind: str = "provider"
    request_digest: str = Field(..., min_length=64, max_length=64)
    child_idempotency_key: str
    retry_safety: str = "conditional"
    target: str
    operation: str = "chat"
    provider: str | None = None
    model: str | None = None
    tool: str | None = None
    capability_digest: str = Field(..., min_length=64, max_length=64)
    task_fence: str | None = None
    reservation_id: str | None = None
    max_authorized_amount_nanos: int | None = None


class RunAdmissionRequest(BaseModel):
    """Admit one Foundation run for the native protocol journey."""

    run_id: str = Field(..., min_length=1, max_length=128)
    task_id: str = Field(..., min_length=1, max_length=128)
    runtime_id: str = "classic"
    runtime_contract_version: str = "1"
    task_fence: str | None = None
    budget_limit_usd_millionths: int = Field(default=1_000_000, ge=1)
    reservation_usd_millionths: int = Field(default=100_000, ge=1)
    reservation_id: str | None = None


class DispatchRequest(BaseModel):
    run_id: str
    task_id: str
    agent_url: str
    request: dict[str, Any]
    activation_id: str | None = None
    attempt: int = 1
    reservation_id: str | None = None
    timeout_s: float = 600.0


@router.get("/keys")
async def daemon_keys() -> dict[str, Any]:
    _require_node_or_operator()
    return {
        "daemon_keys": protocol_keys.daemon_public_records(),
        "audience": protocol_keys.AUDIENCE,
        "protocol_version": protocol.CURRENT_AGENT_PROTOCOL_VERSION,
    }


@router.post("/agent-keys")
async def register_agent_key(body: AgentKeyRegistration) -> dict[str, Any]:
    _require_node_or_operator()
    try:
        return await protocol_keys.register_agent_key(body.agent_id, body.key_id, body.public_key_hex)
    except protocol_keys.KeyRegistrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/agent-keys")
async def list_agent_keys(agent_id: str | None = None) -> dict[str, Any]:
    _require_node_or_operator()
    return {"keys": await protocol_keys.registered_agent_keys(agent_id)}


async def _raw_body(request: Request) -> str:
    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=422, detail="The body carries the exact signed bytes")
    return payload.decode("utf-8")


@router.post("/acknowledgements")
async def post_acknowledgement(request: Request) -> dict[str, Any]:
    _require_node_or_operator()
    text = await _raw_body(request)
    registry = await protocol_keys.registry()
    try:
        outcome = await activations.process_acknowledgement(text=text, key_registry=registry)
    except (protocol.AgentProtocolError, SigningError, activations.ActivationServiceError) as exc:
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}") from exc
    return _plain(outcome)


@router.post("/effect-grants")
async def request_effect_grant(body: EffectGrantRequest) -> dict[str, Any]:
    _require_node_or_operator()
    registry = await protocol_keys.registry()
    grant_row = await activations.get_grant_row(body.parent_grant_id)
    activation = await activations.get_activation(str(grant_row["activation_id"]), int(grant_row["attempt"]))
    reservation_id = body.reservation_id or str(activation.get("reservation_id") or "")
    if not reservation_id:
        raise HTTPException(status_code=422, detail="The parent activation carries no budget reservation")
    amount = body.max_authorized_amount_nanos
    if amount is None:
        amount = await _reservation_remaining_nanos(reservation_id)
    claim_arguments = {
        "dispatcher": agent_dispatch.DISPATCHER_ID,
        "claim_ttl_seconds": agent_dispatch.LEASE_TTL_SECONDS,
        "grant_ttl_seconds": agent_dispatch.GRANT_TTL_SECONDS,
        "daemon_private_key": protocol_keys.daemon_private_key(),
        "key_id": protocol_keys.DAEMON_KEY_ID,
        "key_registry": registry,
        "artifact_store": protocol_keys.artifact_store(),
        "agent_id": str(grant_row["agent_id"]),
        "audience": protocol_keys.AUDIENCE,
        "protocol_version": protocol.CURRENT_AGENT_PROTOCOL_VERSION,
        "capability_digest": body.capability_digest,
        "operation": body.operation,
        "max_authorized_amount_nanos": amount,
        "provider": body.provider,
        "model": body.model,
        "tool": body.tool,
    }
    try:
        result = await effects.request_child_effect_grant(
            run_id=body.run_id, parent_grant_id=body.parent_grant_id, kind=body.kind,
            request_digest=body.request_digest, child_idempotency_key=body.child_idempotency_key,
            reservation_id=reservation_id, retry_safety=body.retry_safety, target=body.target,
            claim_arguments=claim_arguments, task_fence=body.task_fence or str(grant_row["task_fence"]),
        )
    except (effects.EffectServiceError, activations.ActivationServiceError, protocol.AgentProtocolError) as exc:
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}") from exc
    claim = result.get("claim") or {}
    grant = claim.get("grant")
    if grant is None:
        # A duplicate child request returns the stored intent. Serve the
        # exact stored grant bytes from the protected artifact store.
        grant_payload = await _stored_effect_grant(str(result["dispatch_ref"]))
        if grant_payload is None:
            raise HTTPException(status_code=409, detail="The duplicate child request has no stored grant")
    else:
        grant_payload = json.loads(grant.to_bytes().decode("utf-8"))
    return {
        "grant": grant_payload,
        "effect_id": result["effect_id"],
        "effect_operation_id": result["effect_operation_id"],
        "effect_attempt_number": int(result.get("effect_attempt_number", 1)),
        "dispatch_ref": result["dispatch_ref"],
        "duplicate": bool(result.get("duplicate")),
    }


async def _stored_effect_grant(dispatch_ref: str) -> dict[str, Any] | None:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT grant_artifact_digest FROM effect_grants WHERE dispatch_ref = ? ORDER BY created_at DESC LIMIT 1",
            (dispatch_ref,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    stored = protocol_keys.artifact_store().read_object(str(row["grant_artifact_digest"]))
    if stored.get("redacted"):
        return None
    return json.loads(bytes(stored["payload"]).decode("utf-8"))


async def _reservation_remaining_nanos(reservation_id: str) -> int:
    import budget_service as budget

    reservation = await budget.get_reservation(reservation_id)
    reserved = int(reservation.get("reserved_amount_nanos") or 0)
    if reserved > 0:
        return reserved
    resources = reservation.get("resources") or {}
    if isinstance(resources, str):
        resources = json.loads(resources)
    amount = int(resources.get("provider_cost", 0) or 0)
    # The budget ledger counts USD millionths; the effect grant binds nanos.
    return max(amount, 1) * 1_000


@router.post("/receipts")
async def post_receipt(request: Request) -> dict[str, Any]:
    _require_node_or_operator()
    text = await _raw_body(request)
    registry = await protocol_keys.registry()
    try:
        receipt = protocol.parse_attempt_receipt(text)
        stored = await effects.record_attempt_receipt(receipt=receipt, key_registry=registry)
    except (protocol.AgentProtocolError, SigningError, effects.EffectServiceError) as exc:
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}") from exc
    return _plain(stored)


@router.get("/activations/{activation_id}/{attempt}")
async def read_activation(activation_id: str, attempt: int) -> dict[str, Any]:
    _require_node_or_operator()
    try:
        activation = await activations.get_activation(activation_id, attempt)
    except activations.ActivationServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    async with db._connect() as connection:  # noqa: SLF001
        grants = [dict(row) for row in await connection.execute_fetchall(
            "SELECT grant_id, agent_id, agent_protocol_version, task_fence, expires_at "
            "FROM activation_grants WHERE activation_id = ? AND attempt = ? ORDER BY created_at",
            (activation_id, attempt),
        )]
        acknowledgements = [dict(row) for row in await connection.execute_fetchall(
            "SELECT acknowledgement_id, activation_grant_id, decision, decision_reason_code, agent_id, "
            "key_id, received_at FROM activation_acknowledgements WHERE activation_grant_id IN "
            "(SELECT grant_id FROM activation_grants WHERE activation_id = ? AND attempt = ?) ORDER BY received_at",
            (activation_id, attempt),
        )]
        receipts = [dict(row) for row in await connection.execute_fetchall(
            "SELECT receipt_id, effect_id, receipt_sequence, stage, provider, model, usage, key_id, received_at "
            "FROM attempt_receipts WHERE activation_id = ? AND activation_attempt = ? "
            "ORDER BY effect_id, receipt_sequence",
            (activation_id, attempt),
        )]
    for receipt in receipts:
        if isinstance(receipt.get("usage"), str):
            receipt["usage"] = json.loads(receipt["usage"])
    return {
        "activation": _plain(activation),
        "grants": grants,
        "acknowledgements": acknowledgements,
        "receipts": receipts,
    }


@router.post("/runs")
async def admit_run(body: RunAdmissionRequest) -> dict[str, Any]:
    """Admit one run: journal genesis, run control, budget, and reservation.

    The route follows the benchmark admission anchor: the journal
    genesis names the exact runtime pair, the run-control row carries
    the task fence, and the budget holds one reserved reservation the
    activation grants bind.
    """
    import uuid

    import budget_service as budget
    from core.digest_profile import digest_hex

    _require_operator()
    fence = body.task_fence or f"fence-{uuid.uuid4().hex[:12]}"
    try:
        await activations.run_identity(body.run_id)
    except activations.ActivationServiceError:
        payload = {
            "admission_id": f"admission-{body.run_id}",
            "version_set": {"checkpoint_schema_version": "1"},
            "specification_digest": digest_hex("journal-payload", {"task_id": body.task_id}),
            "capability_document_digest": digest_hex("journal-payload", {"agent_protocol": "2"}),
            "admission_digest": digest_hex("journal-payload", {"run_id": body.run_id, "fence": fence}),
        }
        await journal.commit_operation(journal.JournalOperation(
            operation_type="admission_identity", task_id=body.task_id, run_id=body.run_id,
            runtime_id=body.runtime_id, runtime_contract_version=body.runtime_contract_version,
            payload=payload, idempotency_token=f"admission-{body.run_id}",
        ))
    control = await db.get_run_control(body.run_id)
    if control is None:
        await db.create_run_control(body.run_id, body.task_id, fence)
        control = await db.get_run_control(body.run_id)
    budget_id = f"budget-{body.run_id}"
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute("SELECT budget_id FROM run_budgets WHERE budget_id = ?", (budget_id,))
        existing = await cursor.fetchone()
        if existing is None:
            await budget.create_run_budget(
                connection, budget_id=budget_id, run_id=body.run_id, task_id=body.task_id,
                currency="USD",
                limits=(budget.LimitSpec("run", body.run_id, "provider_cost",
                                         body.budget_limit_usd_millionths, currency="USD"),),
            )
            await connection.commit()
    reservation_id = body.reservation_id or f"reservation-{body.run_id}"
    try:
        reservation = await budget.get_reservation(reservation_id)
    except Exception:  # noqa: BLE001 - an absent reservation is the normal first call
        reservation = None
    if reservation is None:
        await budget.request_reservation(
            reservation_id=reservation_id, budget_id=budget_id,
            resources={"provider_cost": body.reservation_usd_millionths},
        )
        if not await budget.reserve(reservation_id):
            raise HTTPException(status_code=422, detail="The reservation does not fit the run budget")
    identity = await activations.run_identity(body.run_id)
    assert control is not None
    return {
        "run_id": body.run_id,
        "task_id": body.task_id,
        "runtime_key": identity,
        "task_fence": str(control["task_fence"]),
        "budget_id": budget_id,
        "reservation_id": reservation_id,
    }


@router.get("/runs/{run_id}")
async def read_run(run_id: str) -> dict[str, Any]:
    """The durable run view: control row, activations, and the replayed projection digest."""
    _require_node_or_operator()
    control = await db.get_run_control(run_id)
    if control is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    state = journal.empty_projection_state()
    records = await journal.read_journal(run_id=run_id)
    for record in records:
        state = journal.apply_record_to_state(state, record)
    async with db._connect() as connection:  # noqa: SLF001
        rows = [dict(row) for row in await connection.execute_fetchall(
            "SELECT activation_id, attempt, state FROM activations WHERE run_id = ? ORDER BY activation_id, attempt",
            (run_id,),
        )]
    return {
        "run_id": run_id,
        "task_id": control["task_id"],
        "task_fence": control["task_fence"],
        "journal_records": len(records),
        "journal_cursor": records[-1].journal_cursor if records else 0,
        "projection_digest": journal.projection_digest(state),
        "activations": rows,
    }


@router.post("/dispatch")
async def dispatch(body: DispatchRequest, request: Request) -> dict[str, Any]:
    """Dispatch one activation to a qualified agent through the native protocol."""
    _require_operator()
    control = await db.get_run_control(body.run_id)
    if control is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    http: httpx.AsyncClient | None = getattr(request.app.state, "health_client", None)
    client = http or httpx.AsyncClient(timeout=body.timeout_s + 15.0)
    try:
        return await agent_dispatch.dispatch_activation(
            client, agent_url=body.agent_url, run_id=body.run_id, task_id=body.task_id,
            activation_id=agent_dispatch.new_activation_id(body.activation_id),
            request=body.request, task_fence=str(control["task_fence"]), attempt=body.attempt,
            reservation_id=body.reservation_id, timeout_s=body.timeout_s,
        )
    except agent_dispatch.DispatchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (activations.ActivationServiceError, protocol.AgentProtocolError, SigningError) as exc:
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}") from exc
    finally:
        if http is None:
            await client.aclose()


def _plain(value: Any) -> Any:
    """Return a JSON-safe copy of one service result."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if hasattr(value, "__dataclass_fields__"):
        import dataclasses

        return _plain(dataclasses.asdict(value))
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value
