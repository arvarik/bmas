"""Foundation Stage 0G: shared typed indexes over opaque payloads.

The host never parses runtime payload semantics, every shared index
update passes its host schema, no index update carries runtime-owned
state, and every index projection equals its journal replay. The
trace envelope carries the declared fields and no payload body.
"""
from __future__ import annotations

import dataclasses

import protocol_test_support as support
import pytest

import database as db
import evidence_service as evidence
import goal_service as goals
import typed_indexes as indexes

RUN = support.RUN_ID
FENCE = support.TASK_FENCE


@pytest.fixture()
async def index_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "indexes.db"))
    await db.init_db()
    await support.seed_run()
    return tmp_path


# ── Opaque runtime payloads ──────────────────────────────────────────


def test_the_host_never_parses_the_runtime_payload():
    payload = indexes.OpaqueRuntimePayload(
        payload_text='{"claims": {"claim-x": "supported"}, '
        '"scheduling": "next"}',
    )
    # The host exposes exact bytes, one digest, and one size — no
    # parsed field, even when the payload looks like host JSON.
    assert len(payload.digest()) == 64
    assert payload.byte_size() == len(payload.payload_text.encode())
    field_names = {spec.name for spec in dataclasses.fields(payload)}
    assert field_names == {"payload_text"}


async def test_a_misleading_payload_never_reaches_an_index(index_db):
    # A runtime payload that resembles a claim update changes no
    # shared index; only a validated index proposal does.
    payload = indexes.OpaqueRuntimePayload(
        payload_text='{"claim_id": "claim-fake", '
        '"evidence_state": "supported"}',
    )
    assert payload.digest()
    live = await indexes.read_shared_indexes(RUN)
    assert live["claims_evidence"]["claims"] == {}


# ── The host index contracts ─────────────────────────────────────────


def test_every_index_kind_validates_its_contract():
    valid = {
        "claims_evidence": {"claim_id": "claim-a",
                            "evidence_state": "proposed"},
        "goals": {"goal_id": "goal-a", "goal_state": "proposed"},
        "budget": {"reservation_id": "reservation-a",
                   "consumed_usd_millionths": 10},
        "assets_artifacts": {"asset_id": "asset-a",
                             "content_digest": "1" * 64},
        "activations_effects": {"activation_id": "activation-a",
                                "activation_state": "queued"},
        "traces_controls": {"event_type": "activation_transition",
                            "payload_schema": "1"},
    }
    for kind, record in valid.items():
        indexes.validate_index_update(kind, record)
    with pytest.raises(indexes.IndexContractError):
        indexes.validate_index_update("private_index", {"x": 1})
    with pytest.raises(indexes.IndexContractError):
        indexes.validate_index_update("goals", {"goal_id": "goal-a"})


def test_no_index_update_carries_runtime_owned_state():
    for owned_field in sorted(indexes.RUNTIME_OWNED_FIELDS):
        with pytest.raises(indexes.IndexContractError):
            indexes.validate_index_update(
                "goals",
                {
                    "goal_id": "goal-a",
                    "goal_state": "proposed",
                    owned_field: {"schema": "runtime-private"},
                },
            )


# ── Journal replay equality ──────────────────────────────────────────


async def test_indexes_never_become_a_second_authority(index_db):
    await evidence.register_claim(
        run_id=RUN, claim_id="claim-a", statement_digest="1" * 64,
        policy=evidence.REGISTERED_POLICIES["deterministic-single"],
        task_fence=FENCE,
    )
    await evidence.record_decision(
        run_id=RUN, claim_id="claim-a", verifier_id="checker",
        verifier_capability="deterministic", verifier_version="1",
        independence_group="deterministic", verdict="supported",
        confidence=100, task_fence=FENCE,
    )
    await goals.create_goal(
        run_id=RUN, goal_id="goal-a", owner="worker-a",
        completion_evidence=("claim-a",), task_fence=FENCE,
    )
    await goals.transition_goal(
        run_id=RUN, goal_id="goal-a", target_state="active",
        expected_version=1, task_fence=FENCE,
    )
    await indexes.assert_indexes_match_journal(RUN)

    # A direct index edit outside the unit of work is a divergence
    # and fails closed on the next replay comparison.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE claim_index SET supported = 0, state = 'unsupported' "
            "WHERE claim_id = 'claim-a'",
        )
        await connection.commit()
    with pytest.raises(indexes.IndexContractError):
        await indexes.assert_indexes_match_journal(RUN)


# ── The trace envelope ───────────────────────────────────────────────


async def test_trace_envelopes_carry_the_declared_fields(index_db):
    await goals.create_goal(
        run_id=RUN, goal_id="goal-trace", owner="worker-a",
        task_fence=FENCE,
    )
    envelopes = await indexes.trace_projection(RUN)
    assert envelopes, "the run journal produces trace envelopes"
    last = envelopes[-1]
    assert last.schema_version == indexes.TRACE_ENVELOPE_SCHEMA_VERSION
    assert last.run_id == RUN
    assert last.task_id == support.TASK_ID
    assert last.runtime_id == "classic"
    assert last.runtime_contract_version == "1"
    assert last.event_type == "goal_update"
    assert last.producer == "daemon"
    assert last.authority_type == "host"
    assert last.data_classification == "internal"
    assert last.redaction_policy_version == "1"
    assert last.trusted_timestamp
    assert last.journal_cursor > 0
    field_names = {spec.name for spec in dataclasses.fields(last)}
    # The envelope carries references and metadata, never a payload
    # body field.
    assert "payload" not in field_names
    assert "payload_schema" in field_names
    assert "protected_artifact_refs" in field_names


async def test_the_trace_projection_is_not_a_second_authority(index_db):
    await goals.create_goal(
        run_id=RUN, goal_id="goal-rebuild", owner="worker-a",
        task_fence=FENCE,
    )
    first = await indexes.trace_projection(RUN)
    second = await indexes.trace_projection(RUN)
    # The projection rebuilds identically from the journal each time;
    # nothing appends outside a journal transaction.
    assert first == second
    cursors = [envelope.journal_cursor for envelope in first]
    assert cursors == sorted(cursors)
