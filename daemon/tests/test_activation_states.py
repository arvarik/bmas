"""Foundation Stage 0F: the durable activation state machine.

Only declared transitions pass, every normal path reaches its
documented state, duplicate transitions are idempotent, one atomic
claim wins each race, waits persist their proposal digest first, and
resume rechecks every changed authority.
"""
from __future__ import annotations

import asyncio
import itertools

import protocol_test_support as support
import pytest

import activation_service as activations
import budget_service as budget
import database as db
import runtime_journal as journal
from core.activation_states import (
    ACTIVATION_DISPATCH_STATES,
    ACTIVATION_DISPATCH_TRANSITIONS,
    ACTIVATION_LIFECYCLE_CLASS,
    ACTIVATION_STATES,
    ACTIVATION_TERMINAL_STATES,
    ACTIVATION_TRANSITIONS,
    ACTIVATION_WAIT_STATES,
    ActivationStateRegistry,
    StateExtension,
    StateMachineError,
    TerminalStateError,
    UndeclaredTransitionError,
    UnknownStateError,
    validate_activation_dispatch_transition,
    validate_activation_transition,
)

RUN_ID = support.RUN_ID
FENCE = support.TASK_FENCE


@pytest.fixture()
async def protocol_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "activation.db"))
    await db.init_db()
    await support.seed_run()
    await support.seed_budget()
    await support.make_reservation("reservation-activation")
    return tmp_path


async def create_queued(activation_id: str = "activation-a") -> str:
    await activations.create_activation(
        run_id=RUN_ID,
        activation_id=activation_id,
        request_digest=support.REQUEST_DIGEST,
        context_view_digest=support.CONTEXT_DIGEST,
        task_fence=FENCE,
    )
    return activation_id


async def claim(activation_id: str, owner: str = "worker-a") -> dict:
    return await activations.claim_activation(
        run_id=RUN_ID,
        activation_id=activation_id,
        attempt=1,
        owner=owner,
        lease_ttl_seconds=3600,
        task_fence=FENCE,
    )


# ── The pure transition tables ───────────────────────────────────────


def test_only_declared_activation_transitions_pass():
    for current, target in itertools.product(
        ACTIVATION_STATES, ACTIVATION_STATES,
    ):
        declared = (current, target) in ACTIVATION_TRANSITIONS
        if declared:
            assert validate_activation_transition(current, target)
        elif current in ACTIVATION_TERMINAL_STATES:
            with pytest.raises(TerminalStateError):
                validate_activation_transition(current, target)
        else:
            with pytest.raises(UndeclaredTransitionError):
                validate_activation_transition(current, target)


def test_only_declared_dispatch_transitions_pass():
    for current, target in itertools.product(
        ACTIVATION_DISPATCH_STATES, ACTIVATION_DISPATCH_STATES,
    ):
        declared = (current, target) in ACTIVATION_DISPATCH_TRANSITIONS
        if declared:
            assert validate_activation_dispatch_transition(current, target)
        else:
            with pytest.raises(UndeclaredTransitionError):
                validate_activation_dispatch_transition(current, target)


def test_unknown_states_fail_closed():
    with pytest.raises(UnknownStateError):
        validate_activation_transition("queued", "sleeping")
    with pytest.raises(UnknownStateError):
        validate_activation_transition("sleeping", "queued")
    registry = ActivationStateRegistry()
    with pytest.raises(UnknownStateError):
        registry.validate_transition("queued", "sleeping")


def test_every_activation_state_maps_to_one_lifecycle_class():
    assert sorted(ACTIVATION_LIFECYCLE_CLASS) == sorted(ACTIVATION_STATES)


def test_patchboard_waits_use_the_shared_states():
    # PatchBoard uses the shared wait states without a private
    # extension, so both wait states are shared activation states.
    assert set(ACTIVATION_STATES) >= ACTIVATION_WAIT_STATES


def test_state_extension_registers_with_lifecycle_and_transitions():
    registry = ActivationStateRegistry()
    registry.register(
        StateExtension(
            state="external_review",
            lifecycle_class="waiting",
            transitions={
                ("proposal_recorded", "external_review"): "digest_persisted",
                ("external_review", "resume_queued"): "review_decision",
            },
        ),
    )
    assert registry.lifecycle_class("external_review") == "waiting"
    assert registry.validate_transition(
        "proposal_recorded", "external_review",
    )
    assert registry.validate_transition("external_review", "resume_queued")
    # Shared transitions stay valid through the same registry.
    assert registry.validate_transition("queued", "leased")


def test_invalid_state_extensions_fail_closed():
    registry = ActivationStateRegistry()
    with pytest.raises(StateMachineError):
        registry.register(
            StateExtension(state="queued", lifecycle_class="pending",
                           transitions={("queued", "leased"): "x"}),
        )
    with pytest.raises(StateMachineError):
        registry.register(
            StateExtension(state="odd", lifecycle_class="unknown-class",
                           transitions={("queued", "odd"): "x"}),
        )
    with pytest.raises(StateMachineError):
        registry.register(
            StateExtension(state="odd", lifecycle_class="waiting",
                           transitions={}),
        )
    with pytest.raises(StateMachineError):
        registry.register(
            StateExtension(
                state="odd",
                lifecycle_class="waiting",
                transitions={("queued", "leased"): "not-mine"},
            ),
        )
    with pytest.raises(StateMachineError):
        registry.register(
            StateExtension(
                state="odd",
                lifecycle_class="waiting",
                transitions={("odd", "nowhere"): "x"},
            ),
        )
    registry.register(
        StateExtension(
            state="odd",
            lifecycle_class="waiting",
            transitions={("suspended", "odd"): "x"},
        ),
    )
    with pytest.raises(UndeclaredTransitionError):
        registry.validate_transition("odd", "committed")


# ── Normal paths ─────────────────────────────────────────────────────


async def test_queue_through_cancellation_before_dispatch(protocol_db):
    activation_id = await create_queued()
    await activations.transition_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        target_state="cancelled",
        evidence={"condition": "no_dispatch_obligation"},
        task_fence=FENCE,
    )
    row = await activations.get_activation(activation_id, 1)
    assert row["state"] == "cancelled"


async def test_abandonment_records_the_exact_dead_letter_reason(protocol_db):
    activation_id = await create_queued()
    await claim(activation_id)
    await activations.transition_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        target_state="abandoned",
        ledger_updates={"terminal_reason": "retry_exhausted"},
        task_fence=FENCE,
    )
    row = await activations.get_activation(activation_id, 1)
    assert row["state"] == "abandoned"
    assert row["terminal_reason"] == "retry_exhausted"


async def test_wait_paths_persist_the_proposal_digest_first(protocol_db):
    for wait_state in ("awaiting_gate", "awaiting_human"):
        activation_id = await create_queued(f"activation-{wait_state}")
        lease = await claim(activation_id)
        await drive_to_proposal_recorded(activation_id)
        await activations.enter_wait(
            run_id=RUN_ID, activation_id=activation_id, attempt=1,
            wait_state=wait_state,
            proposal_digest="d" * 64,
            wait_reason="semantic_gate",
            wait_policy_version="1",
            required_approver="approver-a",
            lease_id=lease["lease_id"],
            owner="worker-a",
            task_fence=FENCE,
        )
        row = await activations.get_activation(activation_id, 1)
        assert row["state"] == wait_state
        assert row["proposal_digest"] == "d" * 64
        assert row["wait_reason"] == "semantic_gate"
        assert row["wait_policy_version"] == "1"
        assert row["required_approver"] == "approver-a"
        lease_row = await activations.get_lease(lease["lease_id"])
        assert lease_row["released"] == 1


async def drive_to_proposal_recorded(activation_id: str) -> None:
    """Use the validation-resume-free direct path for wait tests."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activations SET state = 'proposal_recorded' "
            "WHERE activation_id = ? AND attempt = 1",
            (activation_id,),
        )
        await connection.commit()


async def test_wait_without_persisted_evidence_fails(protocol_db):
    activation_id = await create_queued()
    await claim(activation_id)
    await drive_to_proposal_recorded(activation_id)
    with pytest.raises(activations.ActivationServiceError):
        await activations.transition_activation(
            run_id=RUN_ID, activation_id=activation_id, attempt=1,
            target_state="awaiting_human",
            task_fence=FENCE,
        )


async def test_suspension_and_resume_through_a_new_lease(protocol_db):
    activation_id = await create_queued()
    await claim(activation_id)
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activations SET state = 'dispatched', "
            "proposal_digest = ? WHERE activation_id = ?",
            ("d" * 64, activation_id),
        )
        await connection.commit()
    await activations.transition_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        target_state="suspended",
        evidence={"condition": "effect_outcome_unknown"},
        task_fence=FENCE,
    )
    await activations.transition_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        target_state="resume_queued",
        evidence={"decision_id": "decision-a", "reason": "reconciled"},
        task_fence=FENCE,
    )
    resumed = await activations.validation_resume(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        owner="worker-b", lease_ttl_seconds=3600, task_fence=FENCE,
    )
    row = await activations.get_activation(activation_id, 1)
    assert row["state"] == "proposal_recorded"
    assert resumed["claim"]["lease_fence"] == 2


# ── Idempotency and races ────────────────────────────────────────────


async def test_duplicate_transitions_are_idempotent(protocol_db):
    activation_id = await create_queued()
    first = await activations.transition_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        target_state="cancelled",
        idempotency_token="cancel-once",
        task_fence=FENCE,
    )
    repeat = await activations.transition_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        target_state="cancelled",
        idempotency_token="cancel-once",
        task_fence=FENCE,
    )
    assert repeat.journal_cursor == first.journal_cursor


async def test_reused_token_with_a_different_request_conflicts(protocol_db):
    activation_id = await create_queued()
    await activations.transition_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        target_state="cancelled",
        idempotency_token="decide-once",
        task_fence=FENCE,
    )
    with pytest.raises(journal.JournalConflictError):
        await activations.transition_activation(
            run_id=RUN_ID, activation_id=activation_id, attempt=1,
            target_state="abandoned",
            idempotency_token="decide-once",
            task_fence=FENCE,
        )


async def test_one_atomic_claim_wins_the_race(protocol_db):
    activation_id = await create_queued()
    results = await asyncio.gather(
        claim(activation_id, "worker-a"),
        claim(activation_id, "worker-b"),
        return_exceptions=True,
    )
    winners = [entry for entry in results if isinstance(entry, dict)]
    losers = [entry for entry in results if isinstance(entry, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0]["lease_fence"] == 1
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS leases, MAX(lease_fence) AS top "
            "FROM activation_leases WHERE activation_id = ?",
            (activation_id,),
        )
        row = await cursor.fetchone()
    assert row["leases"] == 1
    assert row["top"] == 1


async def test_lease_renewal_rejects_every_wrong_identity(protocol_db):
    activation_id = await create_queued()
    lease = await claim(activation_id)
    assert await activations.renew_activation_lease(
        lease_id=lease["lease_id"], owner="worker-a",
        lease_fence=lease["lease_fence"], run_id=RUN_ID,
        task_fence=FENCE, ttl_seconds=3600,
    )
    assert not await activations.renew_activation_lease(
        lease_id=lease["lease_id"], owner="worker-wrong",
        lease_fence=lease["lease_fence"], run_id=RUN_ID,
        task_fence=FENCE, ttl_seconds=3600,
    )
    assert not await activations.renew_activation_lease(
        lease_id=lease["lease_id"], owner="worker-a",
        lease_fence=lease["lease_fence"], run_id=RUN_ID,
        task_fence="fence-wrong", ttl_seconds=3600,
    )
    assert not await activations.renew_activation_lease(
        lease_id=lease["lease_id"], owner="worker-a",
        lease_fence=lease["lease_fence"] + 7, run_id=RUN_ID,
        task_fence=FENCE, ttl_seconds=3600,
    )


async def test_expired_lease_requeues_with_a_higher_next_fence(protocol_db):
    activation_id = await create_queued()
    await activations.claim_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        owner="worker-a", lease_ttl_seconds=-1, task_fence=FENCE,
    )
    await activations.requeue_expired_lease(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        task_fence=FENCE,
    )
    row = await activations.get_activation(activation_id, 1)
    assert row["state"] == "queued"
    second = await claim(activation_id, "worker-b")
    assert second["lease_fence"] == 2


async def test_live_lease_cannot_requeue(protocol_db):
    activation_id = await create_queued()
    await claim(activation_id)
    with pytest.raises(activations.ActivationServiceError):
        await activations.requeue_expired_lease(
            run_id=RUN_ID, activation_id=activation_id, attempt=1,
            task_fence=FENCE,
        )


async def test_expired_lease_after_dispatch_starts_no_replacement(
    protocol_db, tmp_path,
):
    keys = support.make_keys()
    store = support.make_store(tmp_path)
    queued = await support.queue_dispatch(keys, store)
    # Expire the lease under the dispatch.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_leases SET expires_at = ? WHERE lease_id = ?",
            ("2000-01-01T00:00:00.000Z", queued["claim"]["lease_id"]),
        )
        await connection.commit()
    with pytest.raises((activations.ActivationServiceError,
                        StateMachineError)):
        await activations.requeue_expired_lease(
            run_id=RUN_ID, activation_id=queued["activation_id"], attempt=1,
            task_fence=FENCE,
        )
    row = await activations.get_activation(queued["activation_id"], 1)
    assert row["state"] == "dispatch_queued"


# ── Resume revalidation ──────────────────────────────────────────────


async def test_resume_rechecks_every_changed_authority(protocol_db):
    run_row = await activations.run_identity(RUN_ID)
    assert run_row  # The run exists before the recheck matrix runs.
    registered = {"proposal": "1", "checkpoint": "2"}

    async def revalidate(**overrides):
        arguments = dict(
            run_id=RUN_ID,
            expected_projection_version=await projection_version(),
            schema_versions=dict(registered),
            registered_schema_versions=registered,
            reservation_id="reservation-activation",
        )
        arguments.update(overrides)
        await activations.revalidate_for_resume(**arguments)

    await revalidate()

    with pytest.raises(activations.ResumeRevalidationError) as state_error:
        await revalidate(expected_projection_version=99)
    assert "state_version" in state_error.value.failed_authorities

    with pytest.raises(activations.ResumeRevalidationError) as schema_error:
        await revalidate(schema_versions={"proposal": "0", "checkpoint": "2"})
    assert "schema:proposal" in schema_error.value.failed_authorities

    with pytest.raises(activations.ResumeRevalidationError) as invariant:
        await revalidate(invariant_checks={"board_shape": False})
    assert "invariant:board_shape" in invariant.value.failed_authorities

    await budget.release("reservation-activation")
    with pytest.raises(activations.ResumeRevalidationError) as budget_error:
        await revalidate()
    assert "budget" in budget_error.value.failed_authorities
    await support.make_reservation("reservation-fresh")

    await db.set_run_deadline(RUN_ID, "2000-01-01T00:00:00.000Z", "strict")
    with pytest.raises(activations.ResumeRevalidationError) as deadline:
        await revalidate(reservation_id="reservation-fresh")
    assert "deadline" in deadline.value.failed_authorities
    await db.set_run_deadline(RUN_ID, "2100-01-01T00:00:00.000Z", "strict")

    await db.request_run_cancellation_control(RUN_ID)
    with pytest.raises(activations.ResumeRevalidationError) as cancel:
        await revalidate(reservation_id="reservation-fresh")
    assert "cancellation" in cancel.value.failed_authorities


async def projection_version() -> int:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT projection_version FROM runs WHERE run_id = ?",
            (RUN_ID,),
        )
        row = await cursor.fetchone()
        return int(row["projection_version"])


async def test_validation_resume_requires_the_persisted_digest(protocol_db):
    activation_id = await create_queued()
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activations SET state = 'resume_queued' "
            "WHERE activation_id = ?",
            (activation_id,),
        )
        await connection.commit()
    with pytest.raises(activations.ActivationServiceError):
        await activations.validation_resume(
            run_id=RUN_ID, activation_id=activation_id, attempt=1,
            owner="worker-a", lease_ttl_seconds=3600, task_fence=FENCE,
        )


async def test_activation_retry_persists_delay_and_bounded_jitter(
    protocol_db,
):
    activation_id = await create_queued()
    await activations.transition_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=1,
        target_state="abandoned",
        ledger_updates={"terminal_reason": "provider_fault"},
        task_fence=FENCE,
    )
    with pytest.raises(activations.ActivationServiceError):
        await activations.create_activation(
            run_id=RUN_ID, activation_id=activation_id, attempt=2,
            retry_of_attempt=1, task_fence=FENCE,
        )
    with pytest.raises(activations.ActivationServiceError):
        await activations.create_activation(
            run_id=RUN_ID, activation_id=activation_id, attempt=2,
            retry_of_attempt=1, retry_delay_ms=1000, retry_jitter_ms=2000,
            task_fence=FENCE,
        )
    await activations.create_activation(
        run_id=RUN_ID, activation_id=activation_id, attempt=2,
        retry_of_attempt=1, retry_delay_ms=1000, retry_jitter_ms=250,
        task_fence=FENCE,
    )
    retry = await activations.get_activation(activation_id, 2)
    assert retry["state"] == "queued"
    assert retry["retry_of_attempt"] == 1
    assert retry["retry_delay_ms"] == 1000
    assert retry["retry_jitter_ms"] == 250
    # The terminal predecessor attempt never reopens.
    first = await activations.get_activation(activation_id, 1)
    assert first["state"] == "abandoned"
