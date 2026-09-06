"""Dispatch one activation to a real agent through the native protocol.

The dispatcher probes the agent's capability document, and when the
endpoint qualifies for the current protocol it creates the activation,
claims it, queues the signed grant, claims the dispatch row, records
the send start, delivers the grant with the request, and commits the
returned acknowledgement. A legacy endpoint keeps the bearer ``/execute``
path in the orchestrator.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

import activation_service as activations
import agent_protocol as protocol
import config
import database as db
import protocol_keys
from core.digest_profile import digest_hex

logger = logging.getLogger("bmas.daemon.agent_dispatch")

DISPATCHER_ID = "daemon-dispatcher"
GRANT_TTL_SECONDS = 900.0
LEASE_TTL_SECONDS = 900.0
CAPABILITY_CACHE_SECONDS = 60.0
REQUEST_DIGEST_DOMAIN = "agent-request"
CONTEXT_DIGEST_DOMAIN = "agent-context"
_capability_cache: dict[str, tuple[float, protocol.AgentCapabilityDocument | None]] = {}


class DispatchError(RuntimeError):
    """The native dispatch cannot complete."""


@dataclass(frozen=True)
class NativeContext:
    """The run a task executes under, with its live fence."""

    run_id: str
    task_fence: str


def node_headers() -> dict[str, str]:
    headers = {"X-Node-Id": DISPATCHER_ID}
    execute_key = getattr(config, "BMAS_EXECUTE_KEY", "")
    if execute_key:
        headers["Authorization"] = f"Bearer {execute_key}"
    return headers


def document_from_dict(data: dict[str, Any]) -> protocol.AgentCapabilityDocument:
    fields = dict(data)
    for name in (
        "supported_protocol_versions", "supported_receipt_versions",
        "supported_activation_schemas", "supported_dispatch_schemas",
        "supported_acknowledgement_schemas", "supported_proposal_schemas",
        "supported_envelope_schemas", "receipt_key_ids",
    ):
        fields[name] = tuple(str(item) for item in fields.get(name, ()))
    return protocol.AgentCapabilityDocument(**fields)


async def endpoint_capabilities(
    http: httpx.AsyncClient, agent_url: str, *, use_cache: bool = True,
) -> protocol.AgentCapabilityDocument | None:
    """Fetch the agent's capability document, or None for a legacy agent."""
    now = time.monotonic()
    cached = _capability_cache.get(agent_url)
    if use_cache and cached is not None and now - cached[0] < CAPABILITY_CACHE_SECONDS:
        return cached[1]
    document: protocol.AgentCapabilityDocument | None = None
    try:
        response = await http.get(f"{agent_url.rstrip('/')}/bmas/capabilities", timeout=5.0)
        if response.status_code == 200:
            body = response.json()
            document = document_from_dict(body["document"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.info("Agent %s publishes no native capability document: %s", agent_url, exc)
    _capability_cache[agent_url] = (now, document)
    return document


def supports_native_protocol(document: protocol.AgentCapabilityDocument | None) -> bool:
    return document is not None and protocol.is_qualified(document)


async def native_context(task_id: str) -> NativeContext | None:
    """The live run control for one task, when the task runs under one.

    A task with no run-control row, or a daemon without a reachable
    database, stays on the legacy bearer path.
    """
    try:
        async with db._connect() as connection:  # noqa: SLF001
            cursor = await connection.execute(
                "SELECT run_id, task_fence FROM run_controls WHERE task_id = ? ORDER BY control_version DESC LIMIT 1",
                (task_id,),
            )
            row = await cursor.fetchone()
    except sqlite3.Error as exc:
        logger.debug("No run control lookup for task %s: %s", task_id, exc)
        return None
    if row is None:
        return None
    return NativeContext(run_id=str(row["run_id"]), task_fence=str(row["task_fence"]))


async def dispatch_activation(
    http: httpx.AsyncClient,
    *,
    agent_url: str,
    run_id: str,
    task_id: str,
    activation_id: str,
    request: dict[str, Any],
    task_fence: str,
    attempt: int = 1,
    reservation_id: str | None = None,
    timeout_s: float = 600.0,
    document: protocol.AgentCapabilityDocument | None = None,
) -> dict[str, Any]:
    """Deliver one signed grant to the agent and commit its acknowledgement."""
    document = document or await endpoint_capabilities(http, agent_url, use_cache=False)
    if not supports_native_protocol(document):
        raise DispatchError(f"The agent at {agent_url} does not qualify for protocol {protocol.CURRENT_AGENT_PROTOCOL_VERSION}")
    assert document is not None
    registry = await protocol_keys.registry()
    store = protocol_keys.artifact_store()
    request_digest = digest_hex(REQUEST_DIGEST_DOMAIN, request)
    context_view_digest = digest_hex(CONTEXT_DIGEST_DOMAIN, request.get("context") or {})
    await activations.create_activation(
        run_id=run_id, activation_id=activation_id, attempt=attempt,
        reservation_id=reservation_id, request_digest=request_digest,
        context_view_digest=context_view_digest, task_fence=task_fence,
    )
    claim = await activations.claim_activation(
        run_id=run_id, activation_id=activation_id, attempt=attempt,
        owner=DISPATCHER_ID, lease_ttl_seconds=LEASE_TTL_SECONDS, task_fence=task_fence,
    )
    queued = await activations.queue_activation_dispatch(
        run_id=run_id, activation_id=activation_id, attempt=attempt,
        agent_id=document.agent_id, audience=protocol_keys.AUDIENCE,
        agent_protocol_version=protocol.CURRENT_AGENT_PROTOCOL_VERSION,
        request_digest=request_digest, context_view_digest=context_view_digest,
        task_fence=task_fence, lease_id=str(claim["lease_id"]), owner=DISPATCHER_ID,
        reservation_id=reservation_id or "",
        daemon_private_key=protocol_keys.daemon_private_key(),
        key_id=protocol_keys.DAEMON_KEY_ID, key_registry=registry,
        artifact_store=store, grant_ttl_seconds=GRANT_TTL_SECONDS,
    )
    grant = queued["grant"]
    claimed = await activations.claim_activation_dispatch(
        grant_id=grant.activation_grant_id, run_id=run_id, dispatcher=DISPATCHER_ID,
        claim_ttl_seconds=LEASE_TTL_SECONDS, key_registry=registry, artifact_store=store,
        expected_target_agent_id=document.agent_id, task_fence=task_fence,
    )
    await activations.record_send_start(
        grant_id=grant.activation_grant_id,
        claim_owner=str(claimed["claim_owner"]), claim_fence=str(claimed["claim_fence"]),
    )
    delivery = {
        "grant": json.loads(grant.to_bytes().decode("utf-8")),
        "grant_digest": str(queued["grant_artifact_digest"]),
        "request": {**request, "task_id": task_id, "activation_id": activation_id},
    }
    response = await http.post(
        f"{agent_url.rstrip('/')}/bmas/activations", json=delivery,
        headers=node_headers(), timeout=timeout_s,
    )
    if response.status_code >= 400:
        raise DispatchError(f"The agent rejected the activation: HTTP {response.status_code} {response.text[:300]}")
    body = response.json()
    acknowledgement = body.get("acknowledgement")
    if not isinstance(acknowledgement, dict):
        raise DispatchError("The agent returned no acknowledgement")
    outcome = await activations.process_acknowledgement(
        text=protocol.canonicalize(acknowledgement), key_registry=registry, task_fence=task_fence,
    )
    activation = await activations.get_activation(activation_id, attempt)
    return {
        "grant_id": grant.activation_grant_id,
        "grant_digest": str(queued["grant_artifact_digest"]),
        "agent_id": document.agent_id,
        "acknowledgement_status": str(outcome.get("status")),
        "activation_state": str(activation["state"]),
        "replayed": bool(body.get("replayed")),
        "result": body.get("result"),
        "acknowledgement": acknowledgement,
    }


def new_activation_id(turn_id: str | None = None) -> str:
    return turn_id or f"activation-{uuid.uuid4().hex[:12]}"
