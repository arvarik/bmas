"""The local effect adapter for daemon control-plane model calls.

Runtime Governance for AI Agents (https://arxiv.org/abs/2603.16586)
motivates the live authority check immediately before transport.
An uncertain transport stays in the effect recovery queue.
"""
from __future__ import annotations

import asyncio
import hashlib
import json as jsonlib
import sqlite3
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import activation_service as activations
import agent_dispatch
import agent_protocol as protocol
import budget_service as budget
import database as db
import effect_service as effects
import protocol_keys
import runtime_journal as journal
from core.digest_profile import digest_hex, plain_json
from core.signing import public_bytes_of
from core.variants.classic.activations import reserve_call, seal_response

if TYPE_CHECKING:
    import httpx

CURRENT_TASK: ContextVar[str | None] = ContextVar("classic_effect_task", default=None)
LOCAL_AGENT_ID = "daemon-model-adapter"
LOCAL_KEY_ID = "daemon-model-receipt-key"


def capability_document() -> protocol.AgentCapabilityDocument:
    return protocol.AgentCapabilityDocument(
        schema_version="1", agent_id=LOCAL_AGENT_ID, supported_protocol_versions=("2",),
        supported_receipt_versions=("1",), supported_activation_schemas=("1",),
        supported_dispatch_schemas=("1",), supported_acknowledgement_schemas=("1",),
        supported_proposal_schemas=("1",), supported_envelope_schemas=("1",),
        nested_model_receipts=True, nested_tool_receipts=True, structured_output=True,
        usage_reporting=True, streaming=False, cancellation=True, resume=True,
        durable_grant_deduplication=True, acknowledgement_status_lookup=True,
        receipt_key_ids=(LOCAL_KEY_ID,), max_request_bytes=4 * 1024 * 1024,
        max_response_bytes=4 * 1024 * 1024, max_artifact_bytes=16 * 1024 * 1024,
    )


async def _now() -> str:
    async with db._connect() as connection:  # noqa: SLF001
        return await db._control_now(connection, None)  # noqa: SLF001


async def observation_context(task_id: str | None) -> agent_dispatch.NativeContext:
    """Create a host observation record when no native admission exists."""
    task_id = task_id or "daemon-model-calls"
    try:
        async with db._connect() as connection:  # noqa: SLF001
            controls = await connection.execute_fetchall(
                "SELECT run_id, task_fence FROM run_controls WHERE task_id = ? ORDER BY control_version DESC",
                (task_id,),
            )
        if controls:
            return agent_dispatch.NativeContext(str(controls[0]["run_id"]), str(controls[0]["task_fence"]))
        task = await db.get_task(task_id)
    except sqlite3.Error as exc:
        raise effects.EffectServiceError("The model call cannot read its effect ledger") from exc
    if task and str(task.get("runtime_contract_version")) != "1":
        raise effects.EffectServiceError("A native model call requires its admitted run")
    run_id, fence = f"observe-{task_id}", f"observe-fence-{task_id}"
    metadata = {"mode": "observe_only", "task_id": task_id}
    digest = digest_hex("local-model-observation", metadata)
    await journal.commit_operation(journal.JournalOperation(
        operation_type="admission_identity", task_id=task_id, run_id=run_id,
        runtime_id=str((task or {}).get("variant") or "classic"), runtime_contract_version="1",
        payload={"admission_id": run_id, "version_set": {"agent_protocol_version": "2"},
                 "specification_digest": digest, "capability_document_digest": capability_document().digest(),
                 "admission_digest": digest, "observation": metadata},
        idempotency_token=run_id, authority_type="host", producer=LOCAL_AGENT_ID,
    ))
    await db.create_run_control(run_id, task_id, fence)
    return agent_dispatch.NativeContext(run_id, fence)


async def _record_receipt(grant: Any, *, sequence: int, raw: bytes | None = None,
                          usage: dict[str, int] | None = None, observation: str | None = None) -> None:
    receipt = protocol.sign_attempt_receipt({
        "schema_version": "1", "receipt_id": f"receipt-{grant.effect_id}-{sequence}",
        "effect_operation_id": grant.effect_operation_id, "effect_id": grant.effect_id,
        "effect_attempt_number": grant.effect_attempt_number, "dispatch_ref": grant.dispatch_ref,
        "token_id": grant.token_id, "activation_id": grant.activation_id,
        "activation_attempt": grant.activation_attempt, "receipt_sequence": sequence,
        "request_digest": grant.request_digest, "provider": grant.provider, "model": grant.model,
        "tool": grant.tool, "operation": grant.operation,
        "stage": "transport_starting" if sequence == 1 else "response_observed",
        "transport_observation": observation, "provider_run_id": None, "provider_receipt": None,
        "raw_response_digest": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "usage": usage, "agent_id": LOCAL_AGENT_ID, "protocol_version": "2",
        "agent_observed_at": await _now(), "key_id": LOCAL_KEY_ID,
    }, protocol_keys.daemon_private_key())
    await effects.record_attempt_receipt(receipt=receipt, key_registry=await protocol_keys.registry())


async def post_completion(http: httpx.AsyncClient, url: str, *, json: dict[str, Any],
                          task_id: str | None = None, phase: str = "triage", **kwargs: Any) -> httpx.Response:
    """Post one completion under a native reservation or a legacy observation."""
    task_id = task_id or CURRENT_TASK.get()
    context = await observation_context(task_id)
    identity = await activations.run_identity(context.run_id)
    observe_only = identity["runtime_contract_version"] == "1"
    activation_id = f"control-{uuid.uuid4().hex}"
    reservation_id = "" if observe_only else await reserve_call(context.run_id, activation_id, 1, json)
    document = capability_document()
    await protocol_keys.register_agent_key(LOCAL_AGENT_ID, LOCAL_KEY_ID,
        public_bytes_of(protocol_keys.daemon_private_key()).hex())
    response: httpx.Response | None = None
    execution_error: BaseException | None = None

    async def execute_effect() -> dict[str, Any]:
        nonlocal response
        intent = await effects.create_effect_intent(
            run_id=context.run_id, activation_id=activation_id, activation_attempt=1,
            kind="provider", request_digest=digest_hex("agent-request", plain_json(json)),
            idempotency_scope=activation_id, child_idempotency_key=phase,
            reservation_id=reservation_id, retry_safety="unsafe", task_fence=context.task_fence,
        )
        effect_id = str(intent["effect_id"])
        await effects.approve_effect(run_id=context.run_id, effect_id=effect_id,
                                    observe_only=observe_only, task_fence=context.task_fence)
        await effects.queue_effect_dispatch(run_id=context.run_id, effect_id=effect_id, target=url,
            dispatch_policy="observe_only" if observe_only else "default", task_fence=context.task_fence)
        claim = await effects.claim_effect_dispatch(
            run_id=context.run_id, effect_id=effect_id, dispatcher=LOCAL_AGENT_ID,
            claim_ttl_seconds=900, grant_ttl_seconds=900,
            daemon_private_key=protocol_keys.daemon_private_key(), key_id=protocol_keys.DAEMON_KEY_ID,
            key_registry=await protocol_keys.registry(), artifact_store=protocol_keys.artifact_store(),
            agent_id=LOCAL_AGENT_ID, audience=protocol_keys.AUDIENCE, protocol_version="2",
            capability_digest=document.digest(), operation=phase,
            max_authorized_amount_nanos=0 if observe_only else int(
                (await budget.get_reservation(reservation_id))["reserved_amount_nanos"]),
            provider="litellm", model=str(json.get("model")), observe_only=observe_only,
            task_fence=context.task_fence,
        )
        grant = claim["grant"]
        started = False
        try:
            started = await effects.record_transport_start(dispatch_ref=grant.dispatch_ref, dispatcher=LOCAL_AGENT_ID)
            if not started:
                raise effects.EffectDispatchError("The model transport no longer holds authority")
            await _record_receipt(grant, sequence=1)
            response = await http.post(url, json=json, **kwargs)
            raw = response.content
            # Persist bytes before reading the provider JSON or its usage.
            await effects.observe_response(run_id=context.run_id, effect_id=effect_id, raw_response=raw,
                artifact_store=protocol_keys.artifact_store(), outcome="response_received")
            try:
                body = response.json()
                usage = body.get("usage") if isinstance(body, dict) else None
                usage = ({k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool) and v >= 0}
                         if isinstance(usage, dict) else None)
            except ValueError:
                body = None
                usage = None
            choices = body.get("choices", []) if isinstance(body, dict) else []
            finish_reason = choices[0].get("finish_reason") if choices else None
            await _record_receipt(grant, sequence=2, raw=raw, usage=usage,
                observation=jsonlib.dumps({"finish_reason": finish_reason,
                    "truncated": finish_reason in ("length", "max_tokens"), "http_status": response.status_code}))
            return {"result": raw.decode("utf-8", errors="replace"), "status": "completed"}
        except BaseException as exc:
            if started:
                attempt_row = await effects.get_attempt(effect_id)
                if attempt_row["state"] == "dispatch_claimed":
                    await asyncio.shield(effects.mark_outcome_unknown(run_id=context.run_id,
                        effect_id=effect_id, reason="The local model transport ended without a response"))
                    await asyncio.shield(_record_receipt(grant, sequence=2,
                        observation=f"No response: {type(exc).__name__}"))
            else:
                await effects.unclaim_unstarted_dispatch(run_id=context.run_id, effect_id=effect_id, dispatcher=LOCAL_AGENT_ID)
                await effects.cancel_effect(run_id=context.run_id, effect_id=effect_id, reason="Cancelled before transport")
            raise

    if observe_only:
        await activations.create_activation(run_id=context.run_id, activation_id=activation_id,
            request_digest=digest_hex("agent-request", plain_json(json)), task_fence=context.task_fence)
        await activations.claim_activation(run_id=context.run_id, activation_id=activation_id, attempt=1,
            owner=LOCAL_AGENT_ID, lease_ttl_seconds=900, task_fence=context.task_fence)
        await execute_effect()
        await activations.transition_activation(run_id=context.run_id, activation_id=activation_id,
            attempt=1, target_state="abandoned")
    else:
        async def local_executor(grant: Any, grant_digest: str) -> dict[str, Any]:
            nonlocal execution_error
            ack = protocol.sign_acknowledgement({
                "schema_version": "1", "acknowledgement_id": f"acknowledgement-{grant.activation_grant_id}",
                "activation_grant_id": grant.activation_grant_id, "activation_grant_digest": grant_digest,
                "task_id": grant.task_id, "run_id": grant.run_id, "runtime_key": grant.runtime_key,
                "activation_id": grant.activation_id, "attempt": grant.attempt, "task_fence": grant.task_fence,
                "activation_fence": grant.activation_fence, "agent_id": LOCAL_AGENT_ID,
                "audience": protocol_keys.AUDIENCE, "agent_protocol_version": "2",
                "capability_digest": document.digest(), "decision": "accepted", "decision_reason_code": "accepted",
                "agent_execution_id": None, "grant_nonce": grant.grant_nonce,
                "agent_observed_at": await _now(), "key_id": LOCAL_KEY_ID,
            }, protocol_keys.daemon_private_key())
            await activations.process_acknowledgement(text=ack.to_bytes().decode(),
                key_registry=await protocol_keys.registry(), task_fence=context.task_fence)
            try:
                result = await execute_effect()
            except BaseException as exc:
                execution_error = exc
                result = {"result": "", "status": "failed"}
            return {"acknowledgement": jsonlib.loads(ack.to_bytes()), "result": result}

        outcome = await agent_dispatch.dispatch_activation(http, agent_url="local://model", run_id=context.run_id,
            task_id=str(task_id), activation_id=activation_id, request=json, task_fence=context.task_fence,
            reservation_id=reservation_id, document=document, local_executor=local_executor)
        await seal_response(run_id=context.run_id, activation_id=activation_id, attempt=1,
                            result=outcome["result"], role=None)
        if execution_error is not None:
            raise execution_error
    assert response is not None
    return response
