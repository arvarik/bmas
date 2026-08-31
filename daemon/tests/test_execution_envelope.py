"""Foundation Stage 0F: model proposals and execution envelopes.

The model proposal never carries trusted fields. The daemon builds
every trusted envelope from verified receipts and protected
artifacts, the envelope holds exactly one result field, and crashes
around raw persistence, parsing, and sealing recover without a
duplicate proposal commit.
"""
from __future__ import annotations

import protocol_test_support as support
import pytest

import activation_service as activations
import database as db
import effect_service as effects
import execution_envelope as envelope
import runtime_journal as journal
from core.failpoints import InjectedFaultError, armed

RUN_ID = support.RUN_ID
FENCE = support.TASK_FENCE


@pytest.fixture()
async def protocol_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "envelope.db"))
    await db.init_db()
    await support.seed_run()
    await support.seed_budget()
    await support.make_reservation("reservation-activation")
    await support.make_reservation("reservation-effect")
    return tmp_path


@pytest.fixture()
def keys():
    return support.make_keys()


@pytest.fixture()
def store(tmp_path):
    return support.make_store(tmp_path)


def receipt_chain() -> envelope.VerifiedReceiptChain:
    return envelope.VerifiedReceiptChain(
        dispatch_ref="effect-dispatch-a",
        receipt_digests=("1" * 64, "2" * 64),
        provider_run_id="provider-run-a",
        usage={"provider_cost": 400},
    )


def build(**overrides):
    arguments = dict(
        trusted_status="completed",
        task_id=support.TASK_ID,
        run_id=RUN_ID,
        activation_id="activation-a",
        activation_attempt=1,
        receipt_chain=receipt_chain(),
        raw_response_artifact_digest="3" * 64,
        started_at="2026-08-31T00:00:00.000Z",
        observed_at="2026-08-31T00:00:02.000Z",
        proposal=envelope.parse_model_proposal({"answer": 42}),
    )
    arguments.update(overrides)
    return envelope.build_envelope(**arguments)


# ── Model proposals ──────────────────────────────────────────────────


def test_valid_proposals_parse_and_digest():
    proposal = envelope.parse_model_proposal(
        {"answer": 42, "confidence": 90},
    )
    assert proposal.content == {"answer": 42, "confidence": 90}
    assert len(proposal.digest()) == 64


def test_model_content_cannot_carry_trusted_fields():
    for name in envelope.FORBIDDEN_PROPOSAL_FIELDS:
        with pytest.raises(envelope.ModelProposalError):
            envelope.parse_model_proposal({"answer": 42, name: "smuggled"})


def test_model_supplied_completed_status_rejects_or_strips():
    payload = {"answer": 42, "status": "completed"}
    with pytest.raises(envelope.ModelProposalError):
        envelope.parse_model_proposal(payload, status_policy="reject")
    stripped = envelope.parse_model_proposal(payload, status_policy="ignore")
    assert "status" not in stripped.content
    with pytest.raises(envelope.ModelProposalError):
        envelope.parse_model_proposal(payload, status_policy="trust")


def test_model_supplied_result_reference_rejects():
    with pytest.raises(envelope.ModelProposalError):
        envelope.parse_model_proposal(
            {"answer": 42, "proposal_ref": "self-declared"},
        )


# ── Envelope validation ──────────────────────────────────────────────


def test_a_valid_envelope_builds_from_verified_receipts():
    sealed = build()
    assert sealed.trusted_status == "completed"
    assert sealed.proposal_ref is not None
    assert sealed.receipt_digests == ("1" * 64, "2" * 64)
    assert sealed.host_created
    envelope.validate_envelope(sealed)


def test_the_envelope_requires_trusted_identity_and_status():
    with pytest.raises(envelope.EnvelopeError):
        build(trusted_status="probably-fine")
    with pytest.raises(envelope.EnvelopeError):
        build(activation_id="")
    with pytest.raises(envelope.EnvelopeError):
        build(raw_response_artifact_digest="")


def test_exactly_one_result_field():
    with pytest.raises(envelope.EnvelopeError):
        build(proposal=None)
    with pytest.raises(envelope.EnvelopeError):
        build(parse_failure_ref="4" * 64)
    only_failure = build(proposal=None, parse_failure_ref="4" * 64)
    assert only_failure.result_fields() == ["parse_failure_ref"]
    only_reason = build(proposal=None, no_proposal_reason="cancelled")
    assert only_reason.result_fields() == ["no_proposal_reason"]
    tampered = envelope.AgentExecutionEnvelope(
        **{
            **only_reason.to_dict(),
            "receipt_digests": only_reason.receipt_digests,
            "proposal_ref": "5" * 64,
        },
    )
    with pytest.raises(envelope.EnvelopeError):
        envelope.validate_envelope(tampered)


def test_an_inline_proposal_never_enters_the_envelope():
    sealed = build()
    inline = envelope.AgentExecutionEnvelope(
        **{
            **sealed.to_dict(),
            "receipt_digests": sealed.receipt_digests,
            "proposal_ref": {"answer": 42},  # type: ignore[arg-type]
        },
    )
    with pytest.raises(envelope.EnvelopeError):
        envelope.validate_envelope(inline)


def test_the_builder_accepts_only_daemon_verified_receipts():
    with pytest.raises(envelope.EnvelopeError):
        build(receipt_chain={"receipt_digests": ["r1"]})


def test_an_agent_created_lookalike_stays_untrusted():
    lookalike = {
        "trusted_status": "completed",
        "receipt_digests": ["fabricated"],
        "proposal_ref": "6" * 64,
        "host_created": True,
    }
    assert envelope.classify_agent_payload(lookalike) == "untrusted_content"
    assert envelope.classify_agent_payload({"answer": 42}) == "model_output"
    fabricated = envelope.AgentExecutionEnvelope(
        **{
            **build().to_dict(),
            "receipt_digests": ("fabricated",),
            "host_created": False,
        },
    )
    with pytest.raises(envelope.EnvelopeError):
        envelope.validate_envelope(fabricated)


# ── Crash recovery without duplicate commits ─────────────────────────


async def seal_and_commit(keys, store, child):
    observed = await effects.observe_response(
        run_id=RUN_ID, effect_id=child["effect_id"],
        raw_response=b'{"answer": 42}', artifact_store=store,
        outcome="succeeded", task_fence=FENCE,
    )
    proposal = envelope.parse_model_proposal({"answer": 42})
    sealed = build(
        raw_response_artifact_digest=observed[
            "raw_response_artifact_digest"
        ],
        proposal=proposal,
    )
    await activations.transition_activation(
        run_id=RUN_ID, activation_id="activation-a", attempt=1,
        target_state="result_received",
        ledger_updates={
            "raw_result_artifact_digest": observed[
                "raw_response_artifact_digest"
            ],
            "effect_ids": [child["effect_id"]],
        },
        task_fence=FENCE,
    )
    await activations.transition_activation(
        run_id=RUN_ID, activation_id="activation-a", attempt=1,
        target_state="proposal_recorded",
        ledger_updates={"proposal_digest": proposal.digest()},
        task_fence=FENCE,
    )
    record = await activations.commit_proposal_decision(
        run_id=RUN_ID, activation_id="activation-a", attempt=1,
        decision="accepted", proposal_digest=proposal.digest(),
        request_digest=support.REQUEST_DIGEST,
        execution_envelope_digest=sealed.digest(),
        effect_id=child["effect_id"], task_fence=FENCE,
    )
    return observed, proposal, sealed, record


async def test_crashes_recover_without_a_duplicate_proposal_commit(
    protocol_db, keys, store,
):
    parent = await support.dispatch_and_accept(keys, store)
    child = await effects.request_child_effect_grant(
        run_id=RUN_ID,
        parent_grant_id=parent["grant"].activation_grant_id,
        kind="provider", request_digest="d" * 64,
        child_idempotency_key="child-envelope",
        reservation_id="reservation-effect", retry_safety="safe",
        target="litellm",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    # Crash before raw persistence: nothing persists, and the same
    # observation replays cleanly.
    with armed("effect.before_raw_persist"), pytest.raises(InjectedFaultError):
        await effects.observe_response(
            run_id=RUN_ID, effect_id=child["effect_id"],
            raw_response=b'{"answer": 42}', artifact_store=store,
            outcome="succeeded", task_fence=FENCE,
        )
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["raw_response_artifact_digest"] is None

    # Crash after raw persistence and before the observation commit:
    # the artifact exists, the state is unchanged, and the replayed
    # observation reuses the same idempotent journal record.
    with armed("effect.after_raw_persist"), pytest.raises(InjectedFaultError):
        await effects.observe_response(
            run_id=RUN_ID, effect_id=child["effect_id"],
            raw_response=b'{"answer": 42}', artifact_store=store,
            outcome="succeeded", task_fence=FENCE,
        )
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "dispatch_claimed"

    observed, proposal, sealed, record = await seal_and_commit(
        keys, store, child,
    )
    # Replays after the sealed commit stay idempotent.
    replay = await activations.commit_proposal_decision(
        run_id=RUN_ID, activation_id="activation-a", attempt=1,
        decision="accepted", proposal_digest=proposal.digest(),
        request_digest=support.REQUEST_DIGEST,
        execution_envelope_digest=sealed.digest(),
        effect_id=child["effect_id"], task_fence=FENCE,
    )
    assert replay.journal_cursor == record.journal_cursor
    decisions = [
        entry
        for entry in await journal.read_journal()
        if entry.operation_type == "proposal_decision"
    ]
    assert len(decisions) == 1
    activation = await activations.get_activation("activation-a", 1)
    assert activation["state"] == "committed"
    assert activation["execution_envelope_digest"] == sealed.digest()
