"""Foundation Stage 0F: external-effect operations and attempts.

Every attempt follows the declared state machine, every retry creates
a new identity chain, crash recovery lands in one declared state,
uncertain effects stay visible, and unsafe retries need the separated
approval.
"""
from __future__ import annotations

import itertools

import protocol_test_support as support
import pytest

import activation_service as activations
import agent_protocol as protocol
import budget_service as budget
import database as db
import effect_service as effects
import execution_envelope as envelope
import runtime_journal as journal
from core.activation_states import (
    EFFECT_ATTEMPT_STATES,
    EFFECT_TERMINAL_STATES,
    EFFECT_TRANSITIONS,
    StateMachineError,
    TerminalStateError,
    UndeclaredTransitionError,
    validate_effect_transition,
)
from core.asset_store import DataClass, ProhibitedContentError
from core.failpoints import InjectedFaultError, armed

RUN_ID = support.RUN_ID
FENCE = support.TASK_FENCE
FUTURE = "2100-01-01T00:00:00.000Z"

SAFE_ADAPTER = effects.AdapterCapabilities(
    adapter_id="litellm-adapter",
    adapter_version="3",
    idempotency_key_scope="provider-operation-key",
    idempotency_retention="30d",
    provider_run_lookup=True,
    result_retrieval=True,
    cancellation_semantics="acknowledged",
    compensation_support="none",
    provider_receipt_support=True,
    usage_finalization="late",
    retry_safety="safe",
)

CONDITIONAL_ADAPTER = effects.AdapterCapabilities(
    adapter_id="payments-adapter",
    adapter_version="2",
    idempotency_key_scope="provider-operation-key",
    idempotency_retention="7d",
    provider_run_lookup=True,
    result_retrieval=True,
    cancellation_semantics="best_effort",
    compensation_support="limited",
    provider_receipt_support=True,
    usage_finalization="immediate",
    retry_safety="conditional",
)

UNSAFE_ADAPTER = effects.AdapterCapabilities(
    adapter_id="wire-transfer-adapter",
    adapter_version="1",
    idempotency_key_scope="none",
    idempotency_retention="none",
    provider_run_lookup=False,
    result_retrieval=False,
    cancellation_semantics="none",
    compensation_support="none",
    provider_receipt_support=False,
    usage_finalization="immediate",
    retry_safety="unsafe",
)


@pytest.fixture()
async def protocol_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "effects.db"))
    await db.init_db()
    await support.seed_run()
    await support.seed_budget()
    await support.make_reservation("reservation-activation")
    await support.make_reservation("reservation-effect")
    await support.make_reservation("reservation-retry")
    return tmp_path


@pytest.fixture()
def keys():
    return support.make_keys()


@pytest.fixture()
def store(tmp_path):
    return support.make_store(tmp_path)


async def dispatched_parent(keys, store):
    return await support.dispatch_and_accept(keys, store)


async def make_child(
    keys, store, *, child_key="child-a",
    reservation_id="reservation-effect", parent=None,
):
    parent = parent or await dispatched_parent(keys, store)
    child = await effects.request_child_effect_grant(
        run_id=RUN_ID,
        parent_grant_id=parent["grant"].activation_grant_id,
        kind="provider", request_digest="d" * 64,
        child_idempotency_key=child_key,
        reservation_id=reservation_id, retry_safety="safe",
        target="litellm", provider_operation_key="operation-key-1",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    return parent, child


# ── The pure transition table ────────────────────────────────────────


def test_only_declared_effect_transitions_pass():
    for current, target in itertools.product(
        EFFECT_ATTEMPT_STATES, EFFECT_ATTEMPT_STATES,
    ):
        declared = (current, target) in EFFECT_TRANSITIONS
        if declared:
            assert validate_effect_transition(current, target)
        elif current in EFFECT_TERMINAL_STATES:
            with pytest.raises(TerminalStateError):
                validate_effect_transition(current, target)
        else:
            with pytest.raises(UndeclaredTransitionError):
                validate_effect_transition(current, target)


def test_no_return_to_a_pre_transport_state():
    for target in ("intent", "approved", "dispatch_queued"):
        for current in ("outcome_unknown", "observed"):
            with pytest.raises(UndeclaredTransitionError):
                validate_effect_transition(current, target)
    # The one recovery: a provably unstarted claim returns to the
    # queue. It is not a transport retry.
    assert validate_effect_transition("dispatch_claimed", "dispatch_queued")


# ── Attempt identity and retries ─────────────────────────────────────


async def test_two_retry_attempts_have_distinct_identity_chains(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    retry = await effects.retry_effect(
        run_id=RUN_ID, predecessor_effect_id=child["effect_id"],
        reservation_id="reservation-retry",
        adapter_capabilities=SAFE_ADAPTER, requested_by="worker-a",
        task_fence=FENCE,
    )
    assert retry["effect_operation_id"] == child["effect_operation_id"]
    assert retry["effect_id"] != child["effect_id"]
    assert retry["effect_attempt_number"] == 2
    assert retry["dispatch_ref"] != child["dispatch_ref"]
    first = await effects.get_attempt(child["effect_id"])
    second = await effects.get_attempt(retry["effect_id"])
    assert first["reservation_id"] != second["reservation_id"]
    # A safe retry shares the request digest and provider operation key.
    assert second["request_digest"] == first["request_digest"]
    assert second["provider_operation_key"] == first[
        "provider_operation_key"
    ]
    assert second["retry_of_effect_id"] == child["effect_id"]


async def test_a_changed_request_needs_a_new_effect_operation(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    with pytest.raises(effects.EffectConflictError):
        await effects.create_effect_intent(
            run_id=RUN_ID, activation_id="activation-a",
            activation_attempt=1, kind="provider",
            request_digest="9" * 64,
            idempotency_scope="unused",
            child_idempotency_key="unused",
            reservation_id="reservation-retry", retry_safety="safe",
            retry_of_effect_id=child["effect_id"],
            task_fence=FENCE,
        )


async def test_execution_without_approved_effect_and_dispatch_rejects(
    protocol_db, keys, store,
):
    await dispatched_parent(keys, store)
    for index, kind in enumerate(effects.EFFECT_KINDS):
        await support.make_reservation(f"reservation-kind-{index}", cost=10)
        intent = await effects.create_effect_intent(
            run_id=RUN_ID, activation_id="activation-a",
            activation_attempt=1, kind=kind,
            request_digest="d" * 64,
            idempotency_scope="kind-check",
            child_idempotency_key=f"kind-{kind}",
            reservation_id=f"reservation-kind-{index}",
            retry_safety="safe", task_fence=FENCE,
        )
        with pytest.raises(effects.EffectServiceError):
            await effects.validate_before_transport(
                dispatch_ref=intent["dispatch_ref"],
                dispatcher=support.AGENT_ID,
            )


# ── Crash recovery ───────────────────────────────────────────────────


async def test_crashed_transactions_leave_the_prior_declared_state(
    protocol_db, keys, store,
):
    await dispatched_parent(keys, store)
    # Crash inside the intent transaction: nothing persists.
    with armed("journal.before_commit"), pytest.raises(InjectedFaultError):
        await effects.create_effect_intent(
            run_id=RUN_ID, activation_id="activation-a",
            activation_attempt=1, kind="provider",
            request_digest="d" * 64,
            idempotency_scope="crash-check",
            child_idempotency_key="crash-intent",
            reservation_id="reservation-effect", retry_safety="safe",
            task_fence=FENCE,
        )
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS operations FROM effect_operations",
        )
        row = await cursor.fetchone()
    assert row["operations"] == 0

    intent = await effects.create_effect_intent(
        run_id=RUN_ID, activation_id="activation-a", activation_attempt=1,
        kind="provider", request_digest="d" * 64,
        idempotency_scope="crash-check", child_idempotency_key="crash-a",
        reservation_id="reservation-effect", retry_safety="safe",
        task_fence=FENCE,
    )
    effect_id = intent["effect_id"]

    # Crash inside approval: the attempt stays intent.
    with armed("journal.before_commit"), pytest.raises(InjectedFaultError):
        await effects.approve_effect(
            run_id=RUN_ID, effect_id=effect_id, task_fence=FENCE,
        )
    assert (await effects.get_attempt(effect_id))["state"] == "intent"
    await effects.approve_effect(
        run_id=RUN_ID, effect_id=effect_id, task_fence=FENCE,
    )

    # Crash inside the outbox transaction: the attempt stays approved
    # and no outbox row exists.
    with armed("journal.before_commit"), pytest.raises(InjectedFaultError):
        await effects.queue_effect_dispatch(
            run_id=RUN_ID, effect_id=effect_id, target="litellm",
            task_fence=FENCE,
        )
    assert (await effects.get_attempt(effect_id))["state"] == "approved"
    with pytest.raises(effects.EffectServiceError):
        await effects.get_effect_dispatch(intent["dispatch_ref"])
    await effects.queue_effect_dispatch(
        run_id=RUN_ID, effect_id=effect_id, target="litellm",
        task_fence=FENCE,
    )

    # Crash inside the claim transaction: the row stays queued.
    with armed("journal.before_commit"), pytest.raises(InjectedFaultError):
        await effects.claim_effect_dispatch(
            run_id=RUN_ID, effect_id=effect_id,
            **support.claim_arguments(keys, store), task_fence=FENCE,
        )
    assert (await effects.get_attempt(effect_id))["state"] == (
        "dispatch_queued"
    )
    claim = await effects.claim_effect_dispatch(
        run_id=RUN_ID, effect_id=effect_id,
        **support.claim_arguments(keys, store), task_fence=FENCE,
    )

    # After the transport-start marker, a crash can only produce
    # outcome_unknown; recovery cannot prove remote acceptance.
    await effects.record_transport_start(
        dispatch_ref=intent["dispatch_ref"], dispatcher=support.AGENT_ID,
    )
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=effect_id,
        reason="crash_after_transport_start", task_fence=FENCE,
    )
    assert (await effects.get_attempt(effect_id))["state"] == (
        "outcome_unknown"
    )
    assert claim["grant_bytes"]


async def test_unstarted_claim_returns_to_the_queue(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    dispatch = await effects.get_effect_dispatch(child["dispatch_ref"])
    assert dispatch["transport_started_at"] is None
    await effects.unclaim_unstarted_dispatch(
        run_id=RUN_ID, effect_id=child["effect_id"],
        dispatcher=support.AGENT_ID, task_fence=FENCE,
    )
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "dispatch_queued"
    dispatch = await effects.get_effect_dispatch(child["dispatch_ref"])
    assert dispatch["dispatch_state"] == "queued"
    assert dispatch["claim_owner"] is None


async def test_started_claim_cannot_return_to_the_queue(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.record_transport_start(
        dispatch_ref=child["dispatch_ref"], dispatcher=support.AGENT_ID,
    )
    with pytest.raises(effects.EffectServiceError):
        await effects.unclaim_unstarted_dispatch(
            run_id=RUN_ID, effect_id=child["effect_id"],
            dispatcher=support.AGENT_ID, task_fence=FENCE,
        )
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="claim_expired_after_start", task_fence=FENCE,
    )
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "outcome_unknown"


async def test_predecessor_never_requeues_after_transport(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    before = len(await journal.read_journal())
    with pytest.raises((StateMachineError, effects.EffectServiceError)):
        await effects.unclaim_unstarted_dispatch(
            run_id=RUN_ID, effect_id=child["effect_id"],
            dispatcher=support.AGENT_ID, task_fence=FENCE,
        )
    assert len(await journal.read_journal()) == before
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "outcome_unknown"


# ── Cancellation ─────────────────────────────────────────────────────


async def test_cancel_approved_effect_before_dispatch(
    protocol_db, keys, store,
):
    await dispatched_parent(keys, store)
    intent = await effects.create_effect_intent(
        run_id=RUN_ID, activation_id="activation-a", activation_attempt=1,
        kind="tool", request_digest="d" * 64,
        idempotency_scope="cancel-check", child_idempotency_key="cancel-a",
        reservation_id="reservation-effect", retry_safety="safe",
        task_fence=FENCE,
    )
    await effects.approve_effect(
        run_id=RUN_ID, effect_id=intent["effect_id"], task_fence=FENCE,
    )
    await effects.cancel_effect(
        run_id=RUN_ID, effect_id=intent["effect_id"],
        reason="task_cancelled", task_fence=FENCE,
    )
    attempt = await effects.get_attempt(intent["effect_id"])
    assert attempt["state"] == "cancelled"
    assert attempt["terminal_reason"] == "task_cancelled"


async def test_cancel_queued_dispatch_before_claim(
    protocol_db, keys, store,
):
    await dispatched_parent(keys, store)
    intent = await effects.create_effect_intent(
        run_id=RUN_ID, activation_id="activation-a", activation_attempt=1,
        kind="provider", request_digest="d" * 64,
        idempotency_scope="cancel-check", child_idempotency_key="cancel-b",
        reservation_id="reservation-effect", retry_safety="safe",
        task_fence=FENCE,
    )
    await effects.approve_effect(
        run_id=RUN_ID, effect_id=intent["effect_id"], task_fence=FENCE,
    )
    await effects.queue_effect_dispatch(
        run_id=RUN_ID, effect_id=intent["effect_id"], target="litellm",
        task_fence=FENCE,
    )
    await effects.cancel_effect(
        run_id=RUN_ID, effect_id=intent["effect_id"],
        reason="cancelled_before_claim", task_fence=FENCE,
    )
    attempt = await effects.get_attempt(intent["effect_id"])
    assert attempt["state"] == "cancelled"
    dispatch = await effects.get_effect_dispatch(intent["dispatch_ref"])
    assert dispatch["dispatch_state"] == "cancelled"


async def test_claimed_dispatch_cannot_cancel_as_unstarted(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    with pytest.raises((StateMachineError, effects.EffectServiceError)):
        await effects.cancel_effect(
            run_id=RUN_ID, effect_id=child["effect_id"],
            reason="late_cancel", task_fence=FENCE,
        )


# ── Suspension and the abandonment guard ─────────────────────────────


async def test_activation_cannot_abandon_with_an_unknown_effect(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    await activations.transition_activation(
        run_id=RUN_ID, activation_id="activation-a", attempt=1,
        target_state="suspended",
        evidence={"condition": "effect_outcome_unknown"},
        task_fence=FENCE,
    )
    with pytest.raises(activations.ActivationServiceError):
        await activations.transition_activation(
            run_id=RUN_ID, activation_id="activation-a", attempt=1,
            target_state="abandoned", task_fence=FENCE,
        )
    row = await activations.get_activation("activation-a", 1)
    assert row["state"] == "suspended"
    # Reconciliation proves the outcome; abandonment then passes.
    await effects.observe_via_lookup(
        run_id=RUN_ID, effect_id=child["effect_id"],
        lookup_evidence="provider-lookup", outcome="failed",
        task_fence=FENCE,
    )
    await effects.reconcile_effect(
        run_id=RUN_ID, effect_id=child["effect_id"], usage=None,
        task_fence=FENCE,
    )
    await activations.transition_activation(
        run_id=RUN_ID, activation_id="activation-a", attempt=1,
        target_state="abandoned",
        ledger_updates={"terminal_reason": "effect_failed"},
        task_fence=FENCE,
    )


# ── Retry safety levels ──────────────────────────────────────────────


async def test_conditional_retry_requires_lookup_proof(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    with pytest.raises(effects.RetryPolicyError):
        await effects.retry_effect(
            run_id=RUN_ID, predecessor_effect_id=child["effect_id"],
            reservation_id="reservation-retry",
            adapter_capabilities=CONDITIONAL_ADAPTER,
            requested_by="worker-a", task_fence=FENCE,
        )
    retry = await effects.retry_effect(
        run_id=RUN_ID, predecessor_effect_id=child["effect_id"],
        reservation_id="reservation-retry",
        adapter_capabilities=CONDITIONAL_ADAPTER,
        requested_by="worker-a", lookup_proves_non_acceptance=True,
        task_fence=FENCE,
    )
    attempt = await effects.get_attempt(retry["effect_id"])
    assert attempt["lookup_evidence"] == "lookup_proves_non_acceptance"


async def test_unsafe_retry_requires_the_separated_approval(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    with pytest.raises(effects.RetryPolicyError):
        await effects.retry_effect(
            run_id=RUN_ID, predecessor_effect_id=child["effect_id"],
            reservation_id="reservation-retry",
            adapter_capabilities=UNSAFE_ADAPTER,
            requested_by="worker-a", task_fence=FENCE,
        )
    with pytest.raises(effects.RetryPolicyError):
        await effects.approve_unsafe_retry(
            run_id=RUN_ID,
            effect_operation_id=child["effect_operation_id"],
            retry_of_effect_id=child["effect_id"],
            requested_by="worker-a", approved_by="worker-a",
            reason="self-approval", task_fence=FENCE,
        )
    approval = await effects.approve_unsafe_retry(
        run_id=RUN_ID,
        effect_operation_id=child["effect_operation_id"],
        retry_of_effect_id=child["effect_id"],
        requested_by="worker-a", approved_by="operator-b",
        reason="verified irrecoverable timeout", task_fence=FENCE,
    )
    # The control decision journals before the new attempt exists.
    control_records = [
        record
        for record in await journal.read_journal()
        if record.operation_type == "human_control"
        and record.payload.get("operation") == "approve_unsafe_retry"
    ]
    assert len(control_records) == 1
    retry = await effects.retry_effect(
        run_id=RUN_ID, predecessor_effect_id=child["effect_id"],
        reservation_id="reservation-retry",
        adapter_capabilities=UNSAFE_ADAPTER,
        requested_by="worker-a", approval_id=approval["approval_id"],
        task_fence=FENCE,
    )
    attempt = await effects.get_attempt(retry["effect_id"])
    assert attempt["approval_id"] == approval["approval_id"]
    # The requester alone can never satisfy the approval.
    with pytest.raises(effects.RetryPolicyError):
        await effects.retry_effect(
            run_id=RUN_ID, predecessor_effect_id=child["effect_id"],
            reservation_id="reservation-retry",
            adapter_capabilities=UNSAFE_ADAPTER,
            requested_by="operator-b",
            approval_id=approval["approval_id"], task_fence=FENCE,
        )


# ── Late results, authority, and usage ───────────────────────────────


async def test_late_result_reconciles_without_replacing_authority(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    retry = await effects.retry_effect(
        run_id=RUN_ID, predecessor_effect_id=child["effect_id"],
        reservation_id="reservation-retry",
        adapter_capabilities=SAFE_ADAPTER, requested_by="worker-a",
        task_fence=FENCE,
    )
    await effects.approve_effect(
        run_id=RUN_ID, effect_id=retry["effect_id"], task_fence=FENCE,
    )
    await effects.queue_effect_dispatch(
        run_id=RUN_ID, effect_id=retry["effect_id"], target="litellm",
        task_fence=FENCE,
    )
    await effects.claim_effect_dispatch(
        run_id=RUN_ID, effect_id=retry["effect_id"],
        **support.claim_arguments(keys, store), task_fence=FENCE,
    )
    await effects.observe_response(
        run_id=RUN_ID, effect_id=retry["effect_id"],
        raw_response=b'{"answer": 42}', artifact_store=store,
        outcome="succeeded", task_fence=FENCE,
    )
    await effects.reconcile_effect(
        run_id=RUN_ID, effect_id=retry["effect_id"],
        usage={"provider_cost": 300}, task_fence=FENCE,
    )
    operation = await effects.get_operation(child["effect_operation_id"])
    assert operation["authoritative_result_effect_id"] == retry["effect_id"]

    # The predecessor's late result persists, reconciles its own
    # attempt, and cannot replace the authoritative result.
    journal_before = [
        record
        for record in await journal.read_journal()
        if record.operation_type == "proposal_decision"
    ]
    late = await effects.record_late_result(
        run_id=RUN_ID, effect_id=child["effect_id"],
        dispatch_ref=child["dispatch_ref"],
        raw_response=b'{"answer": 41}', artifact_store=store,
        outcome="succeeded", task_fence=FENCE,
    )
    assert late["superseded"]
    assert not late["proposal_accepted"]
    assert store.has_object(late["raw_response_artifact_digest"])
    predecessor = await effects.get_attempt(child["effect_id"])
    assert predecessor["state"] == "reconciled"
    operation = await effects.get_operation(child["effect_operation_id"])
    assert operation["authoritative_result_effect_id"] == retry["effect_id"]
    journal_after = [
        record
        for record in await journal.read_journal()
        if record.operation_type == "proposal_decision"
    ]
    assert journal_after == journal_before

    # The set-once trigger rejects a direct authority replacement.
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                "UPDATE effect_operations SET "
                "authoritative_result_effect_id = ? "
                "WHERE effect_operation_id = ?",
                (child["effect_id"], child["effect_operation_id"]),
            )
            await connection.commit()


async def test_usage_reconciles_each_attempt_without_double_release(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    retry = await effects.retry_effect(
        run_id=RUN_ID, predecessor_effect_id=child["effect_id"],
        reservation_id="reservation-retry",
        adapter_capabilities=SAFE_ADAPTER, requested_by="worker-a",
        task_fence=FENCE,
    )
    await effects.observe_via_lookup(
        run_id=RUN_ID, effect_id=child["effect_id"],
        lookup_evidence="provider-lookup", outcome="succeeded",
        task_fence=FENCE,
    )
    await effects.reconcile_effect(
        run_id=RUN_ID, effect_id=child["effect_id"],
        usage={"provider_cost": 700}, task_fence=FENCE,
    )
    first = await budget.get_reservation("reservation-effect")
    assert first["state"] == "consumed"
    assert first["consumed_amount_nanos"] == 700
    assert first["released_amount_nanos"] == 300
    # Late usage for the retry reconciles its own reservation.
    late = await effects.record_late_usage(
        effect_id=retry["effect_id"], usage={"provider_cost": 250},
    )
    assert late["reservation_id"] == "reservation-retry"
    assert late["consumed_amount_nanos"] == 250
    # A repeated release of the first reservation fails closed.
    with pytest.raises(budget.BudgetStateError):
        await budget.release("reservation-effect")


async def test_unknown_effect_never_appears_as_zero_cost(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    await effects.mark_outcome_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    reservation = await budget.get_reservation("reservation-effect")
    assert reservation["state"] == "reserved"
    await effects.operator_reconcile_unknown(
        run_id=RUN_ID, effect_id=child["effect_id"],
        operator_id="operator-b", reason="provider gives no lookup",
        task_fence=FENCE,
    )
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "reconciled"
    assert attempt["reconciliation_reason"] == "operator:operator-b"
    reservation = await budget.get_reservation("reservation-effect")
    assert reservation["state"] == "consumed"
    assert reservation["consumption_kind"] == "estimated"
    assert reservation["consumed_amount_nanos"] == 1_000


# ── Proposal eligibility ─────────────────────────────────────────────


async def prepare_proposal(keys, store):
    parent, child = await make_child(keys, store)
    observed = await effects.observe_response(
        run_id=RUN_ID, effect_id=child["effect_id"],
        raw_response=b'{"answer": 42}', artifact_store=store,
        outcome="succeeded", task_fence=FENCE,
    )
    proposal = envelope.parse_model_proposal({"answer": 42})
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
    return parent, child, proposal, observed


async def test_each_eligible_proposal_commits_exactly_one_decision(
    protocol_db, keys, store,
):
    parent, child, proposal, observed = await prepare_proposal(keys, store)
    first = await activations.commit_proposal_decision(
        run_id=RUN_ID, activation_id="activation-a", attempt=1,
        decision="accepted", proposal_digest=proposal.digest(),
        request_digest=support.REQUEST_DIGEST,
        execution_envelope_digest="e" * 64,
        effect_id=child["effect_id"], task_fence=FENCE,
    )
    repeat = await activations.commit_proposal_decision(
        run_id=RUN_ID, activation_id="activation-a", attempt=1,
        decision="accepted", proposal_digest=proposal.digest(),
        request_digest=support.REQUEST_DIGEST,
        execution_envelope_digest="e" * 64,
        effect_id=child["effect_id"], task_fence=FENCE,
    )
    assert repeat.journal_cursor == first.journal_cursor
    decisions = [
        record
        for record in await journal.read_journal()
        if record.operation_type == "proposal_decision"
    ]
    assert len(decisions) == 1


async def test_every_ineligible_proposal_commits_no_decision(
    protocol_db, keys, store,
):
    parent, child, proposal, observed = await prepare_proposal(keys, store)

    async def assert_ineligible(reason, **overrides):
        arguments = dict(
            run_id=RUN_ID, activation_id="activation-a", attempt=1,
            proposal_digest=proposal.digest(),
            request_digest=support.REQUEST_DIGEST,
            effect_id=child["effect_id"],
        )
        arguments.update(overrides)
        with pytest.raises(
            activations.ProposalEligibilityError,
        ) as error:
            await activations.validate_proposal_eligibility(**arguments)
        assert str(error.value) == reason

    await assert_ineligible("proposal_parse", proposal_digest="9" * 64)
    await assert_ineligible("request_match", request_digest="9" * 64)
    await assert_ineligible("effect_reference", effect_id="effect-none")

    async def set_control(column, value):
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                f"UPDATE run_controls SET {column} = ? WHERE run_id = ?",
                (value, RUN_ID),
            )
            await connection.commit()

    await set_control("task_fence", "fence-moved")
    await assert_ineligible("task_fence")
    await set_control("task_fence", FENCE)
    await set_control("cancellation_state", "requested")
    await assert_ineligible("cancellation")
    await set_control("cancellation_state", "active")
    await set_control("deadline_at", support.EARLY_TIME)
    await assert_ineligible("deadline")
    await set_control("deadline_at", None)
    await budget.release("reservation-activation")
    await assert_ineligible("budget")
    # No decision committed anywhere in the matrix.
    decisions = [
        record
        for record in await journal.read_journal()
        if record.operation_type == "proposal_decision"
    ]
    assert decisions == []
    # The protected observation stays available for audit.
    assert store.has_object(observed["raw_response_artifact_digest"])


async def test_missing_protected_observation_blocks_the_proposal(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    with pytest.raises(activations.ProposalEligibilityError) as error:
        await activations.validate_proposal_eligibility(
            run_id=RUN_ID, activation_id="activation-a", attempt=1,
            proposal_digest="d" * 64,
            request_digest=support.REQUEST_DIGEST,
        )
    assert str(error.value) == "protected_observation"


# ── Raw persistence and filtering ────────────────────────────────────


async def test_prohibited_data_never_reaches_raw_persistence(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    with pytest.raises(
        (ProhibitedContentError, effects.EffectServiceError),
    ):
        await effects.observe_response(
            run_id=RUN_ID, effect_id=child["effect_id"],
            raw_response=b"prohibited-bytes", artifact_store=store,
            outcome="succeeded", data_class=DataClass.PROHIBITED,
            task_fence=FENCE,
        )
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "dispatch_claimed"
    assert attempt["raw_response_artifact_digest"] is None


async def test_redaction_runs_before_the_artifact_commit(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)

    def redactor(raw: bytes) -> bytes:
        return raw.replace(b"secret-token", b"[redacted]")

    observed = await effects.observe_response(
        run_id=RUN_ID, effect_id=child["effect_id"],
        raw_response=b'{"answer": "secret-token"}', artifact_store=store,
        outcome="succeeded", redactor=redactor, task_fence=FENCE,
    )
    stored = store.read_object(observed["raw_response_artifact_digest"])
    assert b"secret-token" not in stored["payload"]
    assert b"[redacted]" in stored["payload"]


async def test_parse_failure_keeps_the_protected_raw_artifact(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    observed = await effects.observe_response(
        run_id=RUN_ID, effect_id=child["effect_id"],
        raw_response=b"not json at all", artifact_store=store,
        outcome="succeeded", task_fence=FENCE,
    )
    with pytest.raises(envelope.ModelProposalError):
        envelope.parse_model_proposal("not a dict")  # type: ignore[arg-type]
    assert store.has_object(observed["raw_response_artifact_digest"])
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "observed"
    assert attempt["raw_response_artifact_digest"] == observed[
        "raw_response_artifact_digest"
    ]


async def test_duplicate_provider_result_is_idempotent(
    protocol_db, keys, store,
):
    parent, child = await make_child(keys, store)
    first = await effects.observe_response(
        run_id=RUN_ID, effect_id=child["effect_id"],
        raw_response=b'{"answer": 42}', artifact_store=store,
        outcome="succeeded", task_fence=FENCE,
    )
    duplicate = await effects.observe_response(
        run_id=RUN_ID, effect_id=child["effect_id"],
        raw_response=b'{"answer": 42}', artifact_store=store,
        outcome="succeeded", task_fence=FENCE,
    )
    assert duplicate["record"].journal_cursor == first[
        "record"
    ].journal_cursor


async def test_adapter_capability_records_are_versioned(protocol_db):
    registry = effects.AdapterRegistry()
    registry.register(SAFE_ADAPTER)
    registry.register(CONDITIONAL_ADAPTER)
    registry.register(UNSAFE_ADAPTER)
    record = registry.require("payments-adapter", "2")
    assert record.compensation_support == "limited"
    assert record.cancellation_semantics == "best_effort"
    with pytest.raises(effects.EffectServiceError):
        registry.require("payments-adapter", "1")
    with pytest.raises(effects.EffectServiceError):
        effects.AdapterCapabilities(
            adapter_id="broken", adapter_version="1",
            idempotency_key_scope="none", idempotency_retention="none",
            provider_run_lookup=False, result_retrieval=False,
            cancellation_semantics="none", compensation_support="none",
            provider_receipt_support=False, usage_finalization="none",
            retry_safety="sometimes",
        )


async def test_effect_grant_cannot_widen_scope(protocol_db, keys, store):
    parent, child = await make_child(keys, store)
    token = child["claim"]["grant"]
    assert token.max_authorized_amount_nanos == 1_000
    with pytest.raises(protocol.GrantBindingError):
        protocol.verify_effect_grant(
            token, keys["registry"],
            expected={"max_authorized_amount_nanos": 10**9},
        )
