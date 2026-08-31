"""Shared builders for the Foundation activation and effect suites.

The builders seed one admitted run with its control row and budget,
create the daemon and agent Ed25519 keys, and drive one activation to
the ``dispatched`` state for the nested-effect tests. The qualified
reference agent implements durable grant deduplication and
acknowledgement lookup, so duplicate-delivery tests run against real
agent-side behavior.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import journal_test_support as support
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

import activation_service as activations
import agent_protocol as protocol
import budget_service as budget
import database as db
import runtime_journal as journal
from core.asset_store import ArtifactStore
from core.signing import KeyRegistry, SigningKeyRecord, public_bytes_of

RUN_ID = "run-protocol"
TASK_ID = "task-protocol"
TASK_FENCE = "fence-1"
AGENT_ID = "agent-a"
AUDIENCE = "bmas-agent"
PROTOCOL_VERSION = protocol.CURRENT_AGENT_PROTOCOL_VERSION
DAEMON_KEY_ID = "daemon-key-a"
AGENT_KEY_ID = "agent-key-a"
REQUEST_DIGEST = "a" * 64
CONTEXT_DIGEST = "b" * 64
CAPABILITY_DIGEST = "c" * 64
EARLY_TIME = "2000-01-01T00:00:00.000Z"


async def seed_run(
    run_id: str = RUN_ID,
    task_id: str = TASK_ID,
    *,
    task_fence: str = TASK_FENCE,
) -> None:
    """Create one admitted run with its durable control row."""
    await journal.commit_operation(
        support.admission_operation(run_id, task_id),
    )
    await db.create_run_control(run_id, task_id, task_fence)


async def seed_budget(
    *,
    budget_id: str = "budget-a",
    run_id: str = RUN_ID,
    task_id: str = TASK_ID,
    limit: int = 1_000_000,
) -> None:
    """Create one run budget with a single provider-cost limit."""
    async with db._connect() as connection:  # noqa: SLF001
        await budget.create_run_budget(
            connection,
            budget_id=budget_id,
            run_id=run_id,
            task_id=task_id,
            currency="USD",
            limits=(
                budget.LimitSpec(
                    "run", run_id, "provider_cost", limit, currency="USD",
                ),
            ),
        )
        await connection.commit()


async def make_reservation(
    reservation_id: str,
    *,
    budget_id: str = "budget-a",
    cost: int = 1_000,
) -> str:
    """Create and reserve one budget reservation."""
    await budget.request_reservation(
        reservation_id=reservation_id,
        budget_id=budget_id,
        resources={"provider_cost": cost},
    )
    reserved = await budget.reserve(reservation_id)
    assert reserved
    return reservation_id


def make_keys() -> dict[str, Any]:
    """Create the daemon and agent keys and their registry."""
    registry = KeyRegistry()
    daemon_key = Ed25519PrivateKey.generate()
    agent_key = Ed25519PrivateKey.generate()
    registry.register(
        SigningKeyRecord(
            key_id=DAEMON_KEY_ID,
            owner_id="daemon",
            purpose="daemon-grant",
            public_bytes=public_bytes_of(daemon_key),
            not_before=EARLY_TIME,
        ),
    )
    registry.register(
        SigningKeyRecord(
            key_id=AGENT_KEY_ID,
            owner_id=AGENT_ID,
            purpose="agent-receipt",
            public_bytes=public_bytes_of(agent_key),
            not_before=EARLY_TIME,
        ),
    )
    return {
        "registry": registry,
        "daemon_key": daemon_key,
        "agent_key": agent_key,
    }


def make_store(tmp_path: Path) -> ArtifactStore:
    """Create one tenant-scoped artifact store under the test path."""
    return ArtifactStore(Path(tmp_path) / "artifacts", "tenant-default")


async def queue_dispatch(
    keys: dict[str, Any],
    store: ArtifactStore,
    *,
    run_id: str = RUN_ID,
    activation_id: str = "activation-a",
    attempt: int = 1,
    reservation_id: str = "reservation-activation",
    grant_ttl_seconds: float = 3600,
    key_id: str = DAEMON_KEY_ID,
) -> dict[str, Any]:
    """Create, claim, and dispatch-queue one activation."""
    await activations.create_activation(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        request_digest=REQUEST_DIGEST,
        context_view_digest=CONTEXT_DIGEST,
        task_fence=TASK_FENCE,
    )
    claim = await activations.claim_activation(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        owner="worker-a",
        lease_ttl_seconds=3600,
        task_fence=TASK_FENCE,
    )
    queued = await activations.queue_activation_dispatch(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        agent_id=AGENT_ID,
        audience=AUDIENCE,
        agent_protocol_version=PROTOCOL_VERSION,
        request_digest=REQUEST_DIGEST,
        context_view_digest=CONTEXT_DIGEST,
        task_fence=TASK_FENCE,
        lease_id=claim["lease_id"],
        owner="worker-a",
        reservation_id=reservation_id,
        daemon_private_key=keys["daemon_key"],
        key_id=key_id,
        key_registry=keys["registry"],
        artifact_store=store,
        grant_ttl_seconds=grant_ttl_seconds,
    )
    return {**queued, "claim": claim, "activation_id": activation_id,
            "attempt": attempt, "run_id": run_id}


def acknowledgement_fields(
    queued: dict[str, Any],
    **overrides: Any,
) -> dict[str, Any]:
    """Return valid acknowledgement fields for one queued dispatch."""
    grant = queued["grant"]
    fields: dict[str, Any] = {
        "schema_version": "1",
        "acknowledgement_id": f"acknowledgement-{grant.activation_grant_id}",
        "activation_grant_id": grant.activation_grant_id,
        "activation_grant_digest": queued["grant_artifact_digest"],
        "task_id": grant.task_id,
        "run_id": grant.run_id,
        "runtime_key": grant.runtime_key,
        "activation_id": grant.activation_id,
        "attempt": grant.attempt,
        "task_fence": grant.task_fence,
        "activation_fence": grant.activation_fence,
        "agent_id": AGENT_ID,
        "audience": AUDIENCE,
        "agent_protocol_version": PROTOCOL_VERSION,
        "capability_digest": CAPABILITY_DIGEST,
        "decision": "accepted",
        "decision_reason_code": "accepted",
        "agent_execution_id": None,
        "grant_nonce": grant.grant_nonce,
        "agent_observed_at": "2026-08-31T00:00:00.000Z",
        "key_id": AGENT_KEY_ID,
    }
    fields.update(overrides)
    return fields


def build_acknowledgement(
    queued: dict[str, Any],
    keys: dict[str, Any],
    **overrides: Any,
) -> protocol.ActivationAcknowledgement:
    """Sign one acknowledgement for one queued dispatch."""
    return protocol.sign_acknowledgement(
        acknowledgement_fields(queued, **overrides), keys["agent_key"],
    )


async def dispatch_and_accept(
    keys: dict[str, Any],
    store: ArtifactStore,
    **queue_arguments: Any,
) -> dict[str, Any]:
    """Drive one activation to ``dispatched`` with an accepted response."""
    queued = await queue_dispatch(keys, store, **queue_arguments)
    grant = queued["grant"]
    claimed = await activations.claim_activation_dispatch(
        grant_id=grant.activation_grant_id,
        run_id=queued["run_id"],
        dispatcher="dispatcher-a",
        claim_ttl_seconds=3600,
        key_registry=keys["registry"],
        artifact_store=store,
        expected_target_agent_id=AGENT_ID,
        task_fence=TASK_FENCE,
    )
    await activations.record_send_start(
        grant_id=grant.activation_grant_id,
        claim_owner=str(claimed["claim_owner"]),
        claim_fence=str(claimed["claim_fence"]),
    )
    acknowledgement = build_acknowledgement(queued, keys)
    outcome = await activations.process_acknowledgement(
        text=acknowledgement.to_bytes().decode("utf-8"),
        key_registry=keys["registry"],
        task_fence=TASK_FENCE,
    )
    return {
        **queued,
        "claimed": claimed,
        "acknowledgement": acknowledgement,
        "outcome": outcome,
    }


def claim_arguments(
    keys: dict[str, Any],
    store: ArtifactStore,
    **overrides: Any,
) -> dict[str, Any]:
    """Return the standard nested-effect claim arguments."""
    arguments: dict[str, Any] = {
        "dispatcher": AGENT_ID,
        "claim_ttl_seconds": 3600,
        "grant_ttl_seconds": 3600,
        "daemon_private_key": keys["daemon_key"],
        "key_id": DAEMON_KEY_ID,
        "key_registry": keys["registry"],
        "artifact_store": store,
        "agent_id": AGENT_ID,
        "audience": AUDIENCE,
        "protocol_version": PROTOCOL_VERSION,
        "capability_digest": CAPABILITY_DIGEST,
        "operation": "chat",
        "max_authorized_amount_nanos": 1_000,
        "provider": "litellm",
        "model": "claude",
    }
    arguments.update(overrides)
    return arguments


def receipt_fields(
    child: dict[str, Any],
    token: protocol.EffectGrant,
    *,
    sequence: int,
    stage: str,
    **overrides: Any,
) -> dict[str, Any]:
    """Return valid receipt fields for one claimed nested effect."""
    fields: dict[str, Any] = {
        "schema_version": "1",
        "receipt_id": f"receipt-{child['effect_id']}-{sequence}",
        "effect_operation_id": child["effect_operation_id"],
        "effect_id": child["effect_id"],
        "effect_attempt_number": child["effect_attempt_number"],
        "dispatch_ref": child["dispatch_ref"],
        "token_id": token.token_id,
        "activation_id": token.activation_id,
        "activation_attempt": token.activation_attempt,
        "receipt_sequence": sequence,
        "request_digest": token.request_digest,
        "provider": token.provider,
        "model": token.model,
        "tool": token.tool,
        "operation": token.operation,
        "stage": stage,
        "transport_observation": None,
        "provider_run_id": None,
        "provider_receipt": None,
        "raw_response_digest": None,
        "usage": None,
        "agent_id": AGENT_ID,
        "protocol_version": PROTOCOL_VERSION,
        "agent_observed_at": "2026-08-31T00:00:01.000Z",
        "key_id": AGENT_KEY_ID,
    }
    fields.update(overrides)
    return fields


def build_receipt(
    child: dict[str, Any],
    token: protocol.EffectGrant,
    keys: dict[str, Any],
    *,
    sequence: int,
    stage: str,
    **overrides: Any,
) -> protocol.AgentAttemptReceipt:
    """Sign one attempt receipt for one claimed nested effect."""
    return protocol.sign_attempt_receipt(
        receipt_fields(
            child, token, sequence=sequence, stage=stage, **overrides,
        ),
        keys["agent_key"],
    )


def tamper(record: Any, **changes: Any) -> Any:
    """Return one signed record with changed fields and the old signature."""
    return dataclasses.replace(record, **changes)


def capability_document(**overrides: Any) -> protocol.AgentCapabilityDocument:
    """Return one fully qualified capability document."""
    fields: dict[str, Any] = {
        "schema_version": "1",
        "agent_id": AGENT_ID,
        "supported_protocol_versions": (PROTOCOL_VERSION,),
        "supported_receipt_versions": (
            protocol.RECEIPT_CONTRACT_VERSION,
        ),
        "supported_activation_schemas": ("1",),
        "supported_dispatch_schemas": ("1",),
        "supported_acknowledgement_schemas": ("1",),
        "supported_proposal_schemas": ("1",),
        "supported_envelope_schemas": ("1",),
        "nested_model_receipts": True,
        "nested_tool_receipts": True,
        "structured_output": True,
        "usage_reporting": True,
        "streaming": True,
        "cancellation": True,
        "resume": True,
        "durable_grant_deduplication": True,
        "acknowledgement_status_lookup": True,
        "receipt_key_ids": (AGENT_KEY_ID,),
        "max_request_bytes": 1_000_000,
        "max_response_bytes": 4_000_000,
        "max_artifact_bytes": 16_000_000,
    }
    fields.update(overrides)
    return protocol.AgentCapabilityDocument(**fields)


class QualifiedReferenceAgent:
    """One qualified agent with durable grant deduplication.

    The agent stores each grant identifier and digest before any
    activation work starts. It returns the same stored acknowledgement
    after an exact duplicate delivery, and it rejects the same grant
    identifier with different bytes.
    """

    def __init__(self, keys: dict[str, Any]) -> None:
        self._keys = keys
        self._store: dict[str, tuple[str, bytes]] = {}
        self.executions = 0

    def handle_delivery(
        self, grant_bytes: bytes, queued: dict[str, Any],
    ) -> bytes:
        from core.digest_profile import digest_bytes

        grant = queued["grant"]
        digest = digest_bytes("artifact-content", grant_bytes)
        stored = self._store.get(grant.activation_grant_id)
        if stored is not None:
            stored_digest, stored_acknowledgement = stored
            if stored_digest != digest:
                raise protocol.AgentProtocolError(
                    "The grant identifier arrived with different bytes"
                )
            return stored_acknowledgement
        acknowledgement = build_acknowledgement(queued, self._keys)
        acknowledgement_bytes = acknowledgement.to_bytes()
        # The grant identifier and digest store before work starts.
        self._store[grant.activation_grant_id] = (
            digest, acknowledgement_bytes,
        )
        self.executions += 1
        return acknowledgement_bytes
