"""Foundation Stage 0F: the agent protocol and its dispatch chain.

The suite proves the signed activation grant, the durable dispatch
outbox with its claim lease and recovery branches, the authenticated
acknowledgement, the nested effect grant chain, the signed attempt
receipts, key rotation, and live qualification.
"""
from __future__ import annotations

import dataclasses
import json

import protocol_test_support as support
import pytest

import activation_service as activations
import agent_protocol as protocol
import budget_service as budget
import database as db
import effect_service as effects
import qualification_service as qualification
import runtime_journal as journal
from core.activation_states import UndeclaredTransitionError
from core.digest_profile import digest_bytes
from core.failpoints import InjectedFaultError, armed
from core.signing import (
    KeyNotValidError,
    KeyRegistry,
    SignatureMismatchError,
    SigningError,
    SigningKeyRecord,
    UnknownKeyError,
    public_bytes_of,
    signing_input,
)

RUN_ID = support.RUN_ID
FENCE = support.TASK_FENCE
FUTURE = "2100-01-01T00:00:00.000Z"


@pytest.fixture()
async def protocol_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "protocol.db"))
    await db.init_db()
    await support.seed_run()
    await support.seed_budget()
    await support.make_reservation("reservation-activation")
    await support.make_reservation("reservation-effect")
    await support.make_reservation("reservation-tool")
    return tmp_path


@pytest.fixture()
def keys():
    return support.make_keys()


@pytest.fixture()
def store(tmp_path):
    return support.make_store(tmp_path)


# ── Endpoint negotiation ─────────────────────────────────────────────


def test_current_protocol_activation_never_routes_to_a_legacy_agent():
    directory = protocol.EndpointDirectory()
    directory.publish(protocol.AgentEndpoint(
        agent_id="agent-legacy",
        protocol_version=protocol.LEGACY_AGENT_PROTOCOL_VERSION,
        qualification_state="qualified",
    ))
    directory.publish(protocol.AgentEndpoint(
        agent_id="agent-current",
        protocol_version=protocol.CURRENT_AGENT_PROTOCOL_VERSION,
        qualification_state="qualified",
        capability_document=support.capability_document(
            agent_id="agent-current",
        ),
    ))
    assert directory.health_partitions() == {
        "1": ["agent-legacy"], "2": ["agent-current"],
    }
    selected = directory.select(
        protocol_version=protocol.CURRENT_AGENT_PROTOCOL_VERSION,
    )
    assert selected.agent_id == "agent-current"


def test_no_downgrade_after_current_endpoints_disappear():
    directory = protocol.EndpointDirectory()
    directory.publish(protocol.AgentEndpoint(
        agent_id="agent-legacy",
        protocol_version=protocol.LEGACY_AGENT_PROTOCOL_VERSION,
        qualification_state="qualified",
    ))
    with pytest.raises(protocol.NoQualifiedEndpointError):
        directory.select(
            protocol_version=protocol.CURRENT_AGENT_PROTOCOL_VERSION,
        )


def test_admission_fails_without_every_required_capability():
    directory = protocol.EndpointDirectory()
    directory.publish(protocol.AgentEndpoint(
        agent_id="agent-no-streaming",
        protocol_version=protocol.CURRENT_AGENT_PROTOCOL_VERSION,
        qualification_state="qualified",
        capability_document=support.capability_document(streaming=False),
    ))
    with pytest.raises(protocol.NoQualifiedEndpointError):
        directory.select(
            protocol_version=protocol.CURRENT_AGENT_PROTOCOL_VERSION,
            required_capability_names=("streaming",),
        )


def test_agent_without_durable_deduplication_cannot_qualify():
    document = support.capability_document(
        durable_grant_deduplication=False,
        acknowledgement_status_lookup=False,
    )
    failures = protocol.qualification_failures(document)
    assert "durable_grant_deduplication" in failures
    assert "acknowledgement_status_lookup" in failures
    directory = protocol.EndpointDirectory()
    with pytest.raises(protocol.QualificationError):
        directory.publish(protocol.AgentEndpoint(
            agent_id=support.AGENT_ID,
            protocol_version=protocol.CURRENT_AGENT_PROTOCOL_VERSION,
            qualification_state="qualified",
            capability_document=document,
        ))


def test_legacy_effects_stay_marked_unobservable():
    projection = protocol.legacy_effect_projection({"effect_id": "effect-x"})
    assert projection["observability"] == "legacy_unobservable"
    assert projection["effect_conformance"] == "incomplete"


# ── Activation grant ─────────────────────────────────────────────────


async def test_grant_binding_changes_reject_at_the_agent(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    grant = queued["grant"]
    protocol.verify_activation_grant(grant, keys["registry"])
    changes = {
        "runtime_key": dataclasses.replace(
            grant.runtime_key, runtime_contract_version="9",
        ),
        "request_digest": "9" * 64,
        "context_view_digest": "8" * 64,
        "task_fence": "fence-other",
        "activation_fence": "77",
        "agent_id": "agent-other",
        "agent_protocol_version": "1",
        "audience": "other-audience",
        "expires_at": FUTURE,
        "grant_nonce": "nonce-other",
        "task_id": "task-other",
        "run_id": "run-other",
        "activation_id": "activation-other",
        "attempt": 9,
    }
    for name, value in changes.items():
        tampered = dataclasses.replace(grant, **{name: value})
        with pytest.raises(SignatureMismatchError):
            protocol.verify_activation_grant(tampered, keys["registry"])


async def test_activation_grant_cannot_authorize_an_external_effect(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    grant = queued["grant"]
    # The signature domains never cross: activation-grant bytes cannot
    # verify as an effect grant.
    record = keys["registry"].require(support.DAEMON_KEY_ID)
    from core.signing import verify_payload

    with pytest.raises(SignatureMismatchError):
        verify_payload(
            record.public_bytes,
            "bmas.effect-grant",
            grant.signing_payload(),
            grant.signature,
        )
    with pytest.raises(SigningError):
        signing_input("bmas.unknown-domain", {})


async def test_grant_dispatch_row_and_activation_commit_together(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    grant = queued["grant"]
    row = await activations.get_dispatch_row(grant.activation_grant_id)
    grant_row = await activations.get_grant_row(grant.activation_grant_id)
    activation = await activations.get_activation("activation-a", 1)
    assert activation["state"] == "dispatch_queued"
    assert row["dispatch_state"] == "queued"
    assert grant_row["grant_artifact_digest"] == digest_bytes(
        "artifact-content", queued["grant_bytes"],
    )
    assert row["grant_artifact_digest"] == grant_row["grant_artifact_digest"]
    assert activation["agent_protocol_version"] == support.PROTOCOL_VERSION


async def test_a_crashed_queue_transaction_commits_nothing(
    protocol_db, keys, store,
):
    await activations.create_activation(
        run_id=RUN_ID, activation_id="activation-crash", attempt=1,
        request_digest=support.REQUEST_DIGEST,
        context_view_digest=support.CONTEXT_DIGEST, task_fence=FENCE,
    )
    claim = await activations.claim_activation(
        run_id=RUN_ID, activation_id="activation-crash", attempt=1,
        owner="worker-a", lease_ttl_seconds=3600, task_fence=FENCE,
    )
    with armed("journal.before_commit"), pytest.raises(InjectedFaultError):
        await activations.queue_activation_dispatch(
            run_id=RUN_ID, activation_id="activation-crash", attempt=1,
            agent_id=support.AGENT_ID, audience=support.AUDIENCE,
            agent_protocol_version=support.PROTOCOL_VERSION,
            request_digest=support.REQUEST_DIGEST,
            context_view_digest=support.CONTEXT_DIGEST,
            task_fence=FENCE, lease_id=claim["lease_id"], owner="worker-a",
            reservation_id="reservation-activation",
            daemon_private_key=keys["daemon_key"],
            key_id=support.DAEMON_KEY_ID,
            key_registry=keys["registry"], artifact_store=store,
            grant_ttl_seconds=3600,
        )
    activation = await activations.get_activation("activation-crash", 1)
    assert activation["state"] == "leased"
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS rows_present FROM activation_dispatch_outbox "
            "WHERE activation_id = 'activation-crash'",
        )
        row = await cursor.fetchone()
    assert row["rows_present"] == 0


async def test_expired_signing_key_blocks_grant_creation(
    protocol_db, keys, store,
):
    keys["registry"].revoke(support.DAEMON_KEY_ID, support.EARLY_TIME)
    with pytest.raises(KeyNotValidError):
        await support.queue_dispatch(keys, store)


# ── Dispatch claim rechecks ──────────────────────────────────────────


async def claim_dispatch(queued, keys, store, **overrides):
    arguments = dict(
        grant_id=queued["grant"].activation_grant_id,
        run_id=RUN_ID,
        dispatcher="dispatcher-a",
        claim_ttl_seconds=3600,
        key_registry=keys["registry"],
        artifact_store=store,
        expected_target_agent_id=support.AGENT_ID,
        task_fence=FENCE,
    )
    arguments.update(overrides)
    return await activations.claim_activation_dispatch(**arguments)


async def test_claim_rejects_every_invalid_live_value(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    grant_id = queued["grant"].activation_grant_id

    async def assert_rejected(reason, **overrides):
        with pytest.raises(
            (activations.DispatchClaimError, journal.JournalFenceError),
        ) as error:
            await claim_dispatch(queued, keys, store, **overrides)
        if isinstance(error.value, activations.DispatchClaimError):
            assert str(error.value) == reason
        row = await activations.get_dispatch_row(grant_id)
        assert row["dispatch_state"] == "queued"
        assert row["claim_owner"] is None

    async def set_control(column, value):
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                f"UPDATE run_controls SET {column} = ? WHERE run_id = ?",
                (value, RUN_ID),
            )
            await connection.commit()

    # Task fence.
    await set_control("task_fence", "fence-moved")
    await assert_rejected("task_fence")
    await set_control("task_fence", FENCE)
    # Cancellation.
    await set_control("cancellation_state", "requested")
    await assert_rejected("cancellation")
    await set_control("cancellation_state", "active")
    # Pause control.
    await set_control("pause_state", "paused")
    await assert_rejected("paused")
    await set_control("pause_state", "active")
    # Deadline.
    await set_control("deadline_at", support.EARLY_TIME)
    await assert_rejected("deadline")
    await set_control("deadline_at", None)
    # Activation fence: expire the lease.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_leases SET expires_at = ? WHERE lease_id = ?",
            (support.EARLY_TIME, queued["claim"]["lease_id"]),
        )
        await connection.commit()
    await assert_rejected("activation_fence")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_leases SET expires_at = ? WHERE lease_id = ?",
            ("2200-01-01T00:00:00.000Z", queued["claim"]["lease_id"]),
        )
        await connection.commit()
    # Reservation.
    await budget.release("reservation-activation")
    await assert_rejected("reservation")
    await support.make_reservation("reservation-second")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activations SET reservation_id = 'reservation-second' "
            "WHERE activation_id = 'activation-a'",
        )
        await connection.commit()
    # Protocol.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activations SET agent_protocol_version = '1' "
            "WHERE activation_id = 'activation-a'",
        )
        await connection.commit()
    await assert_rejected("protocol")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activations SET agent_protocol_version = ? "
            "WHERE activation_id = 'activation-a'",
            (support.PROTOCOL_VERSION,),
        )
        await connection.commit()
    # Target.
    await assert_rejected(
        "target", expected_target_agent_id="agent-imposter",
    )
    # Audience.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET audience = 'wrong' "
            "WHERE grant_id = ?",
            (grant_id,),
        )

        await connection.commit()
    await assert_rejected("audience")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET audience = ? "
            "WHERE grant_id = ?",
            (support.AUDIENCE, grant_id),
        )
        await connection.commit()
    # Grant expiry.
    await assert_rejected("grant_expiry", database_time=FUTURE)
    # Artifact digest.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET grant_artifact_digest "
            "= ? WHERE grant_id = ?",
            ("f" * 64, grant_id),
        )
        await connection.commit()
    await assert_rejected("artifact_digest")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET grant_artifact_digest "
            "= ? WHERE grant_id = ?",
            (queued["grant_artifact_digest"], grant_id),
        )
        await connection.commit()
    # Signing-key status.
    keys["registry"].revoke(support.DAEMON_KEY_ID, support.EARLY_TIME)
    await assert_rejected("signing_key")


async def test_crash_recovery_produces_the_declared_row_states(
    protocol_db, keys, store,
):
    # Crash before the claim transaction: the row stays queued.
    queued = await support.queue_dispatch(keys, store)
    grant_id = queued["grant"].activation_grant_id
    with armed("journal.before_commit"), pytest.raises(InjectedFaultError):
        await claim_dispatch(queued, keys, store)
    row = await activations.get_dispatch_row(grant_id)
    assert row["dispatch_state"] == "queued"

    # Crash after the claim and before the send-start marker: recovery
    # returns the expired claim to queued.
    claimed = await claim_dispatch(queued, keys, store)
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET claim_expires_at = ? "
            "WHERE grant_id = ?",
            (support.EARLY_TIME, grant_id),
        )
        await connection.commit()
    outcome = await activations.recover_expired_claim(
        grant_id=grant_id, run_id=RUN_ID, task_fence=FENCE,
    )
    assert outcome == "queued"
    row = await activations.get_dispatch_row(grant_id)
    assert row["dispatch_state"] == "queued"
    assert row["claim_owner"] is None

    # Crash after the send-start marker: recovery cannot prove
    # delivery, so the row moves to delivery_unknown.
    claimed = await claim_dispatch(queued, keys, store)
    await activations.record_send_start(
        grant_id=grant_id,
        claim_owner=str(claimed["claim_owner"]),
        claim_fence=str(claimed["claim_fence"]),
    )
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET claim_expires_at = ? "
            "WHERE grant_id = ?",
            (support.EARLY_TIME, grant_id),
        )
        await connection.commit()
    outcome = await activations.recover_expired_claim(
        grant_id=grant_id, run_id=RUN_ID, task_fence=FENCE,
    )
    assert outcome == "delivery_unknown"

    # Redelivery claims the same signed bytes with the same grant
    # identifier and nonce.
    redelivered = await activations.redeliver_from_delivery_unknown(
        grant_id=grant_id, run_id=RUN_ID, dispatcher="dispatcher-b",
        claim_ttl_seconds=3600, key_registry=keys["registry"],
        artifact_store=store, expected_target_agent_id=support.AGENT_ID,
        task_fence=FENCE,
    )
    assert redelivered["grant_bytes"] == queued["grant_bytes"]
    assert redelivered["delivery_count"] == 3

    # Acknowledgement persistence commits the terminal row state; a
    # crash after persistence returns the stored result on replay.
    acknowledgement = support.build_acknowledgement(queued, keys)
    outcome = await activations.process_acknowledgement(
        text=acknowledgement.to_bytes().decode("utf-8"),
        key_registry=keys["registry"], task_fence=FENCE,
    )
    assert outcome["status"] == "accepted"
    replay = await activations.process_acknowledgement(
        text=acknowledgement.to_bytes().decode("utf-8"),
        key_registry=keys["registry"], task_fence=FENCE,
    )
    assert replay["status"] == "duplicate"
    row = await activations.get_dispatch_row(grant_id)
    assert row["dispatch_state"] == "acknowledged"


async def test_every_recovery_branch_of_the_dispatch_row(
    protocol_db, keys, store,
):
    # Cancel queued before any claim.
    queued = await support.queue_dispatch(
        keys, store, activation_id="activation-cancel",
        reservation_id="reservation-activation",
    )
    grant_id = queued["grant"].activation_grant_id
    await db.request_run_cancellation_control(RUN_ID)
    await activations.cancel_activation_dispatch(
        grant_id=grant_id, run_id=RUN_ID, reason="cancellation_live",
        task_fence=FENCE,
    )
    row = await activations.get_dispatch_row(grant_id)
    assert row["dispatch_state"] == "cancelled"
    # The terminal row rejects every later transition; a late message
    # stays a protected observation.
    with pytest.raises(UndeclaredTransitionError):
        await claim_dispatch(queued, keys, store)
    acknowledgement = support.build_acknowledgement(queued, keys)
    late = await activations.process_acknowledgement(
        text=acknowledgement.to_bytes().decode("utf-8"),
        key_registry=keys["registry"], task_fence=FENCE,
    )
    assert late["status"] == "late_observation"
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS late_rows FROM protected_observations "
            "WHERE kind = 'late_acknowledgement'",
        )
        observation = await cursor.fetchone()
    assert observation["late_rows"] == 1


async def test_dead_letter_branches(protocol_db, keys, store):
    # Dead-letter queued after grant expiry under the recovery policy.
    queued = await support.queue_dispatch(
        keys, store, activation_id="activation-dead",
        grant_ttl_seconds=-1,
    )
    grant_id = queued["grant"].activation_grant_id
    with pytest.raises(activations.ActivationServiceError):
        await activations.dead_letter_activation_dispatch(
            grant_id=grant_id, run_id=RUN_ID, reason="grant_expired",
            recovery_policy="redeliver", task_fence=FENCE,
        )
    await activations.dead_letter_activation_dispatch(
        grant_id=grant_id, run_id=RUN_ID, reason="grant_expired",
        recovery_policy="dead_letter", task_fence=FENCE,
    )
    row = await activations.get_dispatch_row(grant_id)
    assert row["dispatch_state"] == "dead_letter"
    assert row["terminal_reason"] == "grant_expired"


async def test_delivery_unknown_cancel_requires_lookup_and_no_child(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    grant_id = queued["grant"].activation_grant_id
    claimed = await claim_dispatch(queued, keys, store)
    await activations.record_send_start(
        grant_id=grant_id, claim_owner=str(claimed["claim_owner"]),
        claim_fence=str(claimed["claim_fence"]),
    )
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET claim_expires_at = ? "
            "WHERE grant_id = ?",
            (support.EARLY_TIME, grant_id),
        )
        await connection.commit()
    await activations.recover_expired_claim(
        grant_id=grant_id, run_id=RUN_ID, task_fence=FENCE,
    )
    await db.request_run_cancellation_control(RUN_ID)
    with pytest.raises(activations.ActivationServiceError):
        await activations.cancel_activation_dispatch(
            grant_id=grant_id, run_id=RUN_ID, reason="operator_cancel",
            task_fence=FENCE,
        )
    await activations.cancel_activation_dispatch(
        grant_id=grant_id, run_id=RUN_ID, reason="operator_cancel",
        agent_lookup_proves_non_acceptance=True, task_fence=FENCE,
    )
    row = await activations.get_dispatch_row(grant_id)
    assert row["dispatch_state"] == "cancelled"


async def test_replay_rebuilds_the_delivery_projection(
    protocol_db, keys, store,
):
    completed = await support.dispatch_and_accept(keys, store)
    grant_id = completed["grant"].activation_grant_id
    records = await journal.read_journal()
    state = journal.empty_projection_state()
    for record in records:
        if record.run_id == RUN_ID:
            journal.apply_record_to_state(state, record)
    rebuilt = state["activation_dispatch"][grant_id]
    row = await activations.get_dispatch_row(grant_id)
    assert rebuilt["dispatch_state"] == row["dispatch_state"]
    assert (
        state["activations"][RUN_ID]["activation-a"]
        == (await activations.get_activation("activation-a", 1))["state"]
    )


# ── Duplicate delivery against a qualified agent ─────────────────────


async def test_duplicate_delivery_returns_the_stored_acknowledgement(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    await claim_dispatch(queued, keys, store)
    agent = support.QualifiedReferenceAgent(keys)
    first = agent.handle_delivery(queued["grant_bytes"], queued)
    second = agent.handle_delivery(queued["grant_bytes"], queued)
    assert agent.executions == 1
    assert first == second
    outcome = await activations.process_acknowledgement(
        text=first.decode("utf-8"), key_registry=keys["registry"],
        task_fence=FENCE,
    )
    assert outcome["status"] == "accepted"
    replay = await activations.process_acknowledgement(
        text=second.decode("utf-8"), key_registry=keys["registry"],
        task_fence=FENCE,
    )
    assert replay["status"] == "duplicate"


async def test_same_grant_identifier_with_different_bytes_rejects(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    agent = support.QualifiedReferenceAgent(keys)
    agent.handle_delivery(queued["grant_bytes"], queued)
    with pytest.raises(protocol.AgentProtocolError):
        agent.handle_delivery(queued["grant_bytes"] + b" ", queued)
    assert agent.executions == 1


# ── Acknowledgement validation ───────────────────────────────────────


async def test_every_changed_acknowledgement_binding_rejects(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    grant = queued["grant"]
    changes = {
        "acknowledgement_id": "acknowledgement-other",
        "activation_grant_id": "activation-grant-other",
        "activation_grant_digest": "9" * 64,
        "task_id": "task-other",
        "run_id": "run-other",
        "runtime_key": dataclasses.replace(
            grant.runtime_key, runtime_contract_version="9",
        ),
        "activation_id": "activation-other",
        "attempt": 9,
        "task_fence": "fence-other",
        "activation_fence": "99",
        "agent_id": "agent-other",
        "audience": "audience-other",
        "agent_protocol_version": "1",
        "capability_digest": "9" * 64,
        "decision": "rejected",
        "decision_reason_code": "overloaded",
        "agent_execution_id": "execution-other",
        "grant_nonce": "nonce-other",
        "agent_observed_at": FUTURE,
        "key_id": "key-other",
        "signature": "AAAA",
    }
    for name, value in changes.items():
        valid = support.build_acknowledgement(queued, keys)
        tampered = dataclasses.replace(valid, **{name: value})
        with pytest.raises(
            (
                activations.AcknowledgementRejectedError,
                protocol.AcknowledgementError,
                activations.ActivationServiceError,
                SignatureMismatchError,
                UnknownKeyError,
                KeyNotValidError,
            ),
        ):
            await activations.process_acknowledgement(
                text=tampered.to_bytes().decode("utf-8"),
                key_registry=keys["registry"],
                expected_capability_digest=support.CAPABILITY_DIGEST,
                task_fence=FENCE,
            )
        activation = await activations.get_activation("activation-a", 1)
        assert activation["state"] == "dispatch_queued"


async def test_acknowledgement_parser_rejects_malformed_objects(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    acknowledgement = support.build_acknowledgement(queued, keys)
    payload = json.loads(acknowledgement.to_bytes())
    unknown = dict(payload)
    unknown["surprise"] = True
    with pytest.raises(protocol.AcknowledgementError):
        protocol.parse_acknowledgement(json.dumps(unknown))
    missing = dict(payload)
    missing.pop("grant_nonce")
    with pytest.raises(protocol.AcknowledgementError):
        protocol.parse_acknowledgement(json.dumps(missing))
    duplicated = acknowledgement.to_bytes().decode("utf-8").replace(
        '"schema_version":"1"',
        '"schema_version":"1","schema_version":"1"',
        1,
    )
    with pytest.raises(protocol.AcknowledgementError):
        protocol.parse_acknowledgement(duplicated)


async def test_agent_key_states_reject_before_any_transition(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    registry = keys["registry"]
    registry.register(SigningKeyRecord(
        key_id="agent-key-not-yet", owner_id=support.AGENT_ID,
        purpose="agent-receipt",
        public_bytes=public_bytes_of(keys["agent_key"]),
        not_before=FUTURE,
    ))
    registry.register(SigningKeyRecord(
        key_id="agent-key-expired", owner_id=support.AGENT_ID,
        purpose="agent-receipt",
        public_bytes=public_bytes_of(keys["agent_key"]),
        not_before=support.EARLY_TIME,
        not_after="2001-01-01T00:00:00.000Z",
    ))
    registry.register(SigningKeyRecord(
        key_id="agent-key-wrong-purpose", owner_id=support.AGENT_ID,
        purpose="daemon-grant",
        public_bytes=public_bytes_of(keys["agent_key"]),
        not_before=support.EARLY_TIME,
    ))
    cases = (
        ("key-unknown", UnknownKeyError),
        ("agent-key-not-yet", KeyNotValidError),
        ("agent-key-expired", KeyNotValidError),
        ("agent-key-wrong-purpose", KeyNotValidError),
    )
    for key_id, expected in cases:
        acknowledgement = support.build_acknowledgement(
            queued, keys, key_id=key_id,
            acknowledgement_id=f"acknowledgement-{key_id}",
        )
        with pytest.raises(expected):
            await activations.process_acknowledgement(
                text=acknowledgement.to_bytes().decode("utf-8"),
                key_registry=registry, task_fence=FENCE,
            )
    registry.revoke(support.AGENT_KEY_ID, support.EARLY_TIME)
    acknowledgement = support.build_acknowledgement(queued, keys)
    with pytest.raises(KeyNotValidError):
        await activations.process_acknowledgement(
            text=acknowledgement.to_bytes().decode("utf-8"),
            key_registry=registry, task_fence=FENCE,
        )
    activation = await activations.get_activation("activation-a", 1)
    assert activation["state"] == "dispatch_queued"


async def test_second_accepted_acknowledgement_must_match_stored_bytes(
    protocol_db, keys, store,
):
    completed = await support.dispatch_and_accept(keys, store)
    different = support.build_acknowledgement(
        completed, keys, acknowledgement_id="acknowledgement-second",
        agent_execution_id="execution-b",
    )
    with pytest.raises(activations.AcknowledgementRejectedError):
        await activations.process_acknowledgement(
            text=different.to_bytes().decode("utf-8"),
            key_registry=keys["registry"], task_fence=FENCE,
        )
    replayed_id = dataclasses.replace(
        support.build_acknowledgement(
            completed, keys, agent_execution_id="execution-b",
        ),
    )
    with pytest.raises(activations.AcknowledgementRejectedError):
        await activations.process_acknowledgement(
            text=replayed_id.to_bytes().decode("utf-8"),
            key_registry=keys["registry"], task_fence=FENCE,
        )


async def test_rejected_acknowledgement_dead_letters_and_releases(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    grant_id = queued["grant"].activation_grant_id
    await claim_dispatch(queued, keys, store)
    rejection = support.build_acknowledgement(
        queued, keys, decision="rejected",
        decision_reason_code="capability_missing",
    )
    outcome = await activations.process_acknowledgement(
        text=rejection.to_bytes().decode("utf-8"),
        key_registry=keys["registry"], task_fence=FENCE,
    )
    assert outcome["status"] == "rejected"
    row = await activations.get_dispatch_row(grant_id)
    assert row["dispatch_state"] == "dead_letter"
    activation = await activations.get_activation("activation-a", 1)
    assert activation["state"] == "abandoned"
    reservation = await budget.get_reservation("reservation-activation")
    assert reservation["state"] == "released"


async def test_late_acknowledgement_stays_a_protected_observation(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store, grant_ttl_seconds=-1)
    acknowledgement = support.build_acknowledgement(queued, keys)
    outcome = await activations.process_acknowledgement(
        text=acknowledgement.to_bytes().decode("utf-8"),
        key_registry=keys["registry"], task_fence=FENCE,
    )
    assert outcome["status"] == "late_observation"
    activation = await activations.get_activation("activation-a", 1)
    assert activation["state"] == "dispatch_queued"


# ── Nested effect grants ─────────────────────────────────────────────


async def test_child_effect_before_accepted_acknowledgement_denies(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    with pytest.raises(effects.EffectServiceError):
        await effects.request_child_effect_grant(
            run_id=RUN_ID,
            parent_grant_id=queued["grant"].activation_grant_id,
            kind="provider", request_digest="d" * 64,
            child_idempotency_key="child-early",
            reservation_id="reservation-effect", retry_safety="safe",
            target="litellm",
            claim_arguments=support.claim_arguments(keys, store),
            task_fence=FENCE,
        )
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS operations FROM effect_operations",
        )
        row = await cursor.fetchone()
    assert row["operations"] == 0
    reservation = await budget.get_reservation("reservation-effect")
    assert reservation["state"] == "reserved"


async def test_nested_model_and_tool_grants_have_distinct_identity(
    protocol_db, keys, store,
):
    completed = await support.dispatch_and_accept(keys, store)
    parent_grant_id = completed["grant"].activation_grant_id
    model_call = await effects.request_child_effect_grant(
        run_id=RUN_ID, parent_grant_id=parent_grant_id, kind="provider",
        request_digest="d" * 64, child_idempotency_key="child-model",
        reservation_id="reservation-effect", retry_safety="safe",
        target="litellm",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    tool_call = await effects.request_child_effect_grant(
        run_id=RUN_ID, parent_grant_id=parent_grant_id, kind="tool",
        request_digest="e" * 64, child_idempotency_key="child-tool",
        reservation_id="reservation-tool", retry_safety="safe",
        target="tool-runner",
        claim_arguments=support.claim_arguments(
            keys, store, provider=None, model=None, tool="search",
            operation="search",
        ),
        task_fence=FENCE,
    )
    assert model_call["effect_operation_id"] != tool_call[
        "effect_operation_id"
    ]
    assert model_call["effect_id"] != tool_call["effect_id"]
    assert model_call["dispatch_ref"] != tool_call["dispatch_ref"]
    for call in (model_call, tool_call):
        attempt = await effects.get_attempt(call["effect_id"])
        assert attempt["state"] == "dispatch_claimed"
        dispatch = await effects.get_effect_dispatch(call["dispatch_ref"])
        assert dispatch["dispatch_state"] == "claimed"
        assert dispatch["grant_digest"] is not None
        assert dispatch["grant_nonce"] is not None
        assert dispatch["claim_owner"] == support.AGENT_ID
        assert dispatch["dispatch_fence"] is not None
        assert dispatch["grant_expires_at"] is not None
    grant = model_call["claim"]["grant"]
    protocol.verify_effect_grant(
        grant, keys["registry"],
        expected={
            "agent_id": support.AGENT_ID,
            "audience": support.AUDIENCE,
            "request_digest": "d" * 64,
            "reservation_id": "reservation-effect",
        },
    )


async def test_grant_request_for_an_unclaimed_row_rejects(
    protocol_db, keys, store,
):
    completed = await support.dispatch_and_accept(keys, store)
    intent = await effects.create_effect_intent(
        run_id=RUN_ID, activation_id="activation-a", activation_attempt=1,
        kind="provider", request_digest="d" * 64,
        idempotency_scope="activation-a-1",
        child_idempotency_key="child-unclaimed",
        reservation_id="reservation-effect", retry_safety="safe",
        task_fence=FENCE,
    )
    assert completed["outcome"]["status"] == "accepted"
    with pytest.raises((effects.EffectServiceError, UndeclaredTransitionError)):
        await effects.claim_effect_dispatch(
            run_id=RUN_ID, effect_id=intent["effect_id"],
            **support.claim_arguments(keys, store),
            task_fence=FENCE,
        )


async def test_crash_after_claim_produces_outcome_unknown(
    protocol_db, keys, store,
):
    completed = await support.dispatch_and_accept(keys, store)
    child = await effects.request_child_effect_grant(
        run_id=RUN_ID,
        parent_grant_id=completed["grant"].activation_grant_id,
        kind="provider", request_digest="d" * 64,
        child_idempotency_key="child-crash",
        reservation_id="reservation-effect", retry_safety="safe",
        target="litellm",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    # The daemon crashed before confirmed grant delivery; after the
    # dispatch lease expires, the effect becomes outcome_unknown.
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="claim_lease_expired_before_confirmed_delivery",
        task_fence=FENCE,
    )
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "outcome_unknown"


async def test_every_changed_effect_grant_binding_rejects(
    protocol_db, keys, store,
):
    completed = await support.dispatch_and_accept(keys, store)
    child = await effects.request_child_effect_grant(
        run_id=RUN_ID,
        parent_grant_id=completed["grant"].activation_grant_id,
        kind="provider", request_digest="d" * 64,
        child_idempotency_key="child-bindings",
        reservation_id="reservation-effect", retry_safety="safe",
        target="litellm",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    grant = child["claim"]["grant"]
    changes = {
        "activation_id": "activation-other",
        "activation_attempt": 9,
        "effect_operation_id": "operation-other",
        "effect_id": "effect-other",
        "effect_attempt_number": 9,
        "dispatch_ref": "dispatch-other",
        "request_digest": "9" * 64,
        "reservation_id": "reservation-other",
        "max_authorized_amount_nanos": 10**9,
        "task_fence": "fence-other",
        "lease_ref": "lease-other",
        "provider": "provider-other",
        "model": "model-other",
        "tool": "tool-other",
        "operation": "operation-other",
        "agent_id": "agent-other",
        "audience": "audience-other",
        "expires_at": FUTURE,
        "grant_nonce": "nonce-other",
        "protocol_version": "1",
        "capability_digest": "9" * 64,
    }
    for name, value in changes.items():
        tampered = dataclasses.replace(grant, **{name: value})
        with pytest.raises(SignatureMismatchError):
            protocol.verify_effect_grant(tampered, keys["registry"])
    # The agent cannot widen scope: expectation mismatches fail.
    with pytest.raises(protocol.GrantBindingError):
        protocol.verify_effect_grant(
            grant, keys["registry"], expected={"provider": "another"},
        )
    with pytest.raises(protocol.GrantBindingError):
        protocol.verify_effect_grant(
            grant, keys["registry"], at=FUTURE,
        )


async def test_duplicate_child_requests_share_one_operation(
    protocol_db, keys, store,
):
    completed = await support.dispatch_and_accept(keys, store)
    parent_grant_id = completed["grant"].activation_grant_id
    first = await effects.request_child_effect_grant(
        run_id=RUN_ID, parent_grant_id=parent_grant_id, kind="provider",
        request_digest="d" * 64, child_idempotency_key="child-same",
        reservation_id="reservation-effect", retry_safety="safe",
        target="litellm",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    duplicate = await effects.request_child_effect_grant(
        run_id=RUN_ID, parent_grant_id=parent_grant_id, kind="provider",
        request_digest="d" * 64, child_idempotency_key="child-same",
        reservation_id="reservation-tool", retry_safety="safe",
        target="litellm", claim_arguments={}, task_fence=FENCE,
    )
    assert duplicate["duplicate"]
    assert duplicate["effect_operation_id"] == first["effect_operation_id"]
    assert duplicate["effect_id"] == first["effect_id"]
    with pytest.raises(effects.EffectConflictError):
        await effects.request_child_effect_grant(
            run_id=RUN_ID, parent_grant_id=parent_grant_id,
            kind="provider", request_digest="f" * 64,
            child_idempotency_key="child-same",
            reservation_id="reservation-tool", retry_safety="safe",
            target="litellm", claim_arguments={}, task_fence=FENCE,
        )


# ── Attempt receipts ─────────────────────────────────────────────────


async def make_claimed_child(keys, store, child_key="child-receipts"):
    completed = await support.dispatch_and_accept(keys, store)
    child = await effects.request_child_effect_grant(
        run_id=RUN_ID,
        parent_grant_id=completed["grant"].activation_grant_id,
        kind="provider", request_digest="d" * 64,
        child_idempotency_key=child_key,
        reservation_id="reservation-effect", retry_safety="safe",
        target="litellm",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    return child, child["claim"]["grant"]


async def test_receipt_stages_and_monotonic_sequence(
    protocol_db, keys, store,
):
    child, token = await make_claimed_child(keys, store)
    for sequence, stage in enumerate(protocol.RECEIPT_STAGES, start=1):
        receipt = support.build_receipt(
            child, token, keys, sequence=sequence, stage=stage,
        )
        stored = await effects.record_attempt_receipt(
            receipt=receipt, key_registry=keys["registry"],
        )
        assert stored["received_at"]
    with pytest.raises(protocol.ReceiptError):
        await effects.record_attempt_receipt(
            receipt=support.build_receipt(
                child, token, keys, sequence=99, stage="response_observed",
                receipt_id="receipt-gap",
            ),
            key_registry=keys["registry"],
        )
    with pytest.raises(protocol.ReceiptError):
        protocol.verify_attempt_receipt_signature(
            support.build_receipt(
                child, token, keys, sequence=7, stage="unknown_stage",
            ),
            keys["registry"],
        )


async def test_receipt_binding_changes_reject(protocol_db, keys, store):
    child, token = await make_claimed_child(keys, store)
    changes = {
        "effect_operation_id": "operation-other",
        "effect_id": "effect-other",
        "effect_attempt_number": 9,
        "dispatch_ref": "dispatch-other",
        "request_digest": "9" * 64,
        "provider": "provider-other",
        "agent_id": "agent-other",
    }
    for name, value in changes.items():
        receipt = support.build_receipt(
            child, token, keys, sequence=1, stage="grant_acknowledged",
            receipt_id=f"receipt-{name}", **{name: value},
        )
        with pytest.raises(
            (protocol.ReceiptError, effects.EffectServiceError),
        ):
            await effects.record_attempt_receipt(
                receipt=receipt, key_registry=keys["registry"],
            )
    tampered = dataclasses.replace(
        support.build_receipt(
            child, token, keys, sequence=1, stage="grant_acknowledged",
        ),
        signature="A" * 86,
    )
    with pytest.raises(SignatureMismatchError):
        await effects.record_attempt_receipt(
            receipt=tampered, key_registry=keys["registry"],
        )


async def test_receipt_replay_rejects_without_a_transition(
    protocol_db, keys, store,
):
    child, token = await make_claimed_child(keys, store)
    receipt = support.build_receipt(
        child, token, keys, sequence=1, stage="grant_acknowledged",
    )
    await effects.record_attempt_receipt(
        receipt=receipt, key_registry=keys["registry"],
    )
    before = len(await journal.read_journal())
    with pytest.raises(protocol.ReceiptError):
        await effects.record_attempt_receipt(
            receipt=receipt, key_registry=keys["registry"],
        )
    replayed_sequence = support.build_receipt(
        child, token, keys, sequence=1, stage="grant_acknowledged",
        receipt_id="receipt-second",
    )
    with pytest.raises(protocol.ReceiptError):
        await effects.record_attempt_receipt(
            receipt=replayed_sequence, key_registry=keys["registry"],
        )
    assert len(await journal.read_journal()) == before
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "dispatch_claimed"


# ── Key rotation ─────────────────────────────────────────────────────


async def test_key_rotation_keeps_historical_verification(
    protocol_db, keys, store,
):
    queued = await support.queue_dispatch(keys, store)
    registry: KeyRegistry = keys["registry"]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    successor = Ed25519PrivateKey.generate()
    registry.register(SigningKeyRecord(
        key_id="daemon-key-b", owner_id="daemon", purpose="daemon-grant",
        public_bytes=public_bytes_of(successor),
        not_before=support.EARLY_TIME,
    ))
    # Overlap: both keys authorize new work.
    now = "2026-08-31T00:00:00.000Z"
    assert set(registry.active_key_ids(
        owner_id="daemon", purpose="daemon-grant", at=now,
    )) == {support.DAEMON_KEY_ID, "daemon-key-b"}
    # Revoke the old key: new authority denies immediately.
    registry.revoke(support.DAEMON_KEY_ID, support.EARLY_TIME)
    with pytest.raises(KeyNotValidError):
        registry.require_new_authority(
            support.DAEMON_KEY_ID, owner_id="daemon",
            purpose="daemon-grant", at=now,
        )
    # Historical verification of the stored grant stays unchanged.
    protocol.verify_activation_grant(queued["grant"], registry)
    # New grants sign with the successor key.
    keys["daemon_key"] = successor
    replacement = await support.queue_dispatch(
        keys, store, activation_id="activation-b",
        reservation_id="reservation-effect", key_id="daemon-key-b",
    )
    assert replacement["grant"].key_id == "daemon-key-b"
    with pytest.raises(SigningError):
        registry.register(SigningKeyRecord(
            key_id="daemon-key-b", owner_id="daemon",
            purpose="daemon-grant",
            public_bytes=public_bytes_of(successor),
            not_before=support.EARLY_TIME,
        ))


# ── Qualification ────────────────────────────────────────────────────


async def run_probes(keys, store, *, expires_at=FUTURE, fail=()):
    completed = await support.dispatch_and_accept(keys, store)
    assert completed["outcome"]["status"] == "accepted"
    counter = {"value": 0}

    async def probe_runner(probe: str) -> dict:
        counter["value"] += 1
        await support.make_reservation(
            f"reservation-probe-{counter['value']}", cost=10,
        )
        intent = await effects.create_effect_intent(
            run_id=RUN_ID, activation_id="activation-a",
            activation_attempt=1, kind="provider",
            request_digest="d" * 64,
            idempotency_scope="qualification",
            child_idempotency_key=f"probe-{probe}",
            reservation_id=f"reservation-probe-{counter['value']}",
            retry_safety="safe", task_fence=FENCE,
        )
        return {"passed": probe not in fail,
                "effect_id": intent["effect_id"]}

    return await qualification.run_qualification_probes(
        provider="litellm", model="claude", adapter="litellm-adapter",
        adapter_version="3", provider_version="7",
        probe_runner=probe_runner,
        credentials_kind="dedicated-qualification",
        expires_at=expires_at,
    )


async def test_qualification_probes_run_through_the_effect_service(
    protocol_db, keys, store,
):
    record = await run_probes(keys, store)
    assert len(record["probe_effect_ids"]) == len(
        qualification.QUALIFICATION_PROBES,
    )
    admitted = await qualification.check_admission(
        provider="litellm", model="claude", adapter="litellm-adapter",
        required_capabilities=("strict_structured_output",
                               "provider_run_lookup"),
    )
    assert admitted["qualification_id"] == record["qualification_id"]
    with pytest.raises(qualification.QualificationServiceError):
        await qualification.run_qualification_probes(
            provider="litellm", model="claude", adapter="litellm-adapter",
            adapter_version="3", provider_version="7",
            probe_runner=None, credentials_kind="production",
            expires_at=FUTURE,
        )


async def test_capability_change_without_version_change_blocks(
    protocol_db, keys, store,
):
    record = await run_probes(keys, store, fail=("cancellation_acknowledgement",))
    advertised = {name: True for name in qualification.QUALIFICATION_PROBES}
    with pytest.raises(qualification.QualificationServiceError):
        qualification.verify_advertised_capabilities(
            advertised=advertised, probed=record["capabilities"],
        )
    with pytest.raises(qualification.AdmissionBlockedError):
        await qualification.check_admission(
            provider="litellm", model="claude", adapter="litellm-adapter",
            required_capabilities=("cancellation_acknowledgement",),
        )


async def test_expired_qualification_stops_new_dispatch_only(
    protocol_db, keys, store,
):
    await run_probes(keys, store, expires_at=support.EARLY_TIME)
    assert await qualification.latest_unexpired(
        provider="litellm", model="claude", adapter="litellm-adapter",
    ) is None
    with pytest.raises(qualification.AdmissionBlockedError):
        await qualification.check_admission(
            provider="litellm", model="claude", adapter="litellm-adapter",
            required_capabilities=(),
        )
    # Existing observations stay readable after expiry.
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS stored FROM provider_qualifications",
        )
        row = await cursor.fetchone()
    assert row["stored"] == 1
