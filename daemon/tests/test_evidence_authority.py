"""Foundation Stage 0G: the evidence authority.

Verifier policies require declared combinations, independence counts
distinct groups, expiry and revocation invalidate derived support
through every level of a derived chain, dependents receive
revalidation markers, and the complete prior decision history stays
readable.
"""
from __future__ import annotations

import protocol_test_support as support
import pytest

import database as db
import evidence_service as evidence
import goal_service as goals
import runtime_journal as journal
from core.failpoints import clear as clear_failpoints

RUN = support.RUN_ID
FENCE = support.TASK_FENCE
FUTURE = "2100-01-01T00:00:00.000Z"
EARLY = "2000-01-01T00:00:00.000Z"


@pytest.fixture()
async def evidence_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "evidence.db"))
    clear_failpoints()
    await db.init_db()
    await support.seed_run()
    return tmp_path


async def register(claim_id: str, policy_id: str, derived=()):
    await evidence.register_claim(
        run_id=RUN,
        claim_id=claim_id,
        statement_digest="1" * 64,
        policy=evidence.REGISTERED_POLICIES[policy_id],
        derived_from=tuple(derived),
        task_fence=FENCE,
    )


async def decide(
    claim_id: str,
    verifier_id: str,
    *,
    group: str,
    capability: str = "model_judge",
    verdict: str = "supported",
    human: bool = False,
    expires_at: str | None = None,
) -> dict:
    return await evidence.record_decision(
        run_id=RUN,
        claim_id=claim_id,
        verifier_id=verifier_id,
        verifier_capability=capability,
        verifier_version="1",
        independence_group=group,
        verdict=verdict,
        confidence=90,
        human_approval=human,
        expires_at=expires_at,
        task_fence=FENCE,
    )


# ── Verifier combinations ────────────────────────────────────────────


async def test_one_deterministic_verifier_policy(evidence_db):
    await register("claim-det", "deterministic-single")
    outcome = await decide(
        "claim-det", "checker",
        group="deterministic", capability="deterministic",
    )
    assert outcome["supported"]


async def test_two_independent_model_families_policy(evidence_db):
    await register("claim-pair", "independent-model-families")
    first = await decide("claim-pair", "judge-a", group="family-one")
    assert not first["supported"]
    second = await decide("claim-pair", "judge-b", group="family-two")
    assert second["supported"]


async def test_verifier_plus_human_policy(evidence_db):
    await register("claim-human", "verifier-plus-human")
    first = await decide("claim-human", "judge-a", group="family-one")
    assert not first["supported"]
    second = await decide(
        "claim-human", "approver",
        group="human", capability="model_judge", human=True,
    )
    assert second["supported"]


async def test_same_independence_group_counts_once(evidence_db):
    # One verifier can never always make a claim supported: two
    # decisions from one group stay one independent voice.
    await register("claim-same", "independent-model-families")
    await decide("claim-same", "judge-a", group="family-one")
    outcome = await decide("claim-same", "judge-a-clone", group="family-one")
    assert not outcome["supported"]
    claim = await evidence.get_claim("claim-same")
    assert claim["state"] == "unsupported"


async def test_refuted_and_inconclusive_never_support(evidence_db):
    await register("claim-verdicts", "deterministic-single")
    await decide(
        "claim-verdicts", "checker",
        group="deterministic", capability="deterministic",
        verdict="refuted",
    )
    await decide(
        "claim-verdicts", "checker-b",
        group="deterministic", capability="deterministic",
        verdict="inconclusive",
    )
    claim = await evidence.get_claim("claim-verdicts")
    assert not claim["supported"]
    with pytest.raises(evidence.EvidenceServiceError):
        await decide(
            "claim-verdicts", "checker-c",
            group="deterministic", verdict="probably",
        )


# ── Expiry and revocation ────────────────────────────────────────────


async def test_expired_decision_invalidates_derived_support(evidence_db):
    await register("claim-expiry", "deterministic-single")
    await decide(
        "claim-expiry", "checker",
        group="deterministic", capability="deterministic",
        expires_at=FUTURE,
    )
    claim = await evidence.get_claim("claim-expiry")
    assert claim["supported"]
    # Re-evaluate after the expiry time passes.
    outcome = await evidence.reevaluate_claim(
        run_id=RUN,
        claim_id="claim-expiry",
        reason="expired",
        task_fence=FENCE,
        database_time="2100-01-02T00:00:00.000Z",
    )
    assert "claim-expiry" in outcome["invalidated"]
    claim = await evidence.get_claim("claim-expiry")
    assert claim["state"] == "invalidated"
    assert claim["revalidation_required"] == 1


async def test_revocation_keeps_the_complete_decision_history(evidence_db):
    await register("claim-history", "deterministic-single")
    first = await decide(
        "claim-history", "checker",
        group="deterministic", capability="deterministic",
    )
    await evidence.revoke_decision(
        run_id=RUN,
        decision_id=first["decision_id"],
        reason="verifier defect",
        task_fence=FENCE,
    )
    history = await evidence.list_decisions("claim-history")
    assert len(history) == 1
    assert history[0]["revoked"] == 1
    assert history[0]["revocation_reason"] == "verifier defect"
    assert history[0]["verdict"] == "supported"
    with pytest.raises(
        (evidence.EvidenceServiceError, journal.JournalConflictError),
    ):
        await evidence.revoke_decision(
            run_id=RUN,
            decision_id=first["decision_id"],
            reason="again",
            task_fence=FENCE,
        )


async def test_decision_history_rows_stay_immutable(evidence_db):
    import sqlite3

    await register("claim-immutable", "deterministic-single")
    decided = await decide(
        "claim-immutable", "checker",
        group="deterministic", capability="deterministic",
    )
    with pytest.raises(sqlite3.IntegrityError):
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                "UPDATE evidence_decisions SET verdict = 'refuted' "
                "WHERE decision_id = ?",
                (decided["decision_id"],),
            )
            await connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                "DELETE FROM evidence_decisions WHERE decision_id = ?",
                (decided["decision_id"],),
            )
            await connection.commit()


async def test_revocation_propagates_through_a_two_level_chain(
    evidence_db,
):
    await register("claim-root", "deterministic-single")
    root = await decide(
        "claim-root", "checker",
        group="deterministic", capability="deterministic",
    )
    await register("claim-derived", "deterministic-single",
                   derived=("claim-root",))
    await register("claim-derived-deeper", "deterministic-single",
                   derived=("claim-derived",))
    outcome = await evidence.revoke_decision(
        run_id=RUN,
        decision_id=root["decision_id"],
        reason="source retracted",
        dependent_action_ids=("activation-a#1",),
        task_fence=FENCE,
    )
    assert sorted(outcome["invalidated"]) == [
        "claim-derived", "claim-derived-deeper", "claim-root",
    ]
    for claim_id in outcome["invalidated"]:
        claim = await evidence.get_claim(claim_id)
        assert claim["state"] == "invalidated"
        assert claim["revalidation_required"] == 1
    markers = await evidence.list_revalidation_markers(target_kind="claim")
    marked = {marker["target_id"] for marker in markers}
    assert marked == {"claim-root", "claim-derived", "claim-derived-deeper"}
    action_markers = await evidence.list_revalidation_markers(
        target_kind="action",
    )
    assert [marker["target_id"] for marker in action_markers] == [
        "activation-a#1",
    ]


async def test_dependent_goals_receive_rollback_and_markers(evidence_db):
    await register("claim-goal", "deterministic-single")
    decided = await decide(
        "claim-goal", "checker",
        group="deterministic", capability="deterministic",
    )
    await goals.create_goal(
        run_id=RUN, goal_id="goal-complete", owner="worker-a",
        completion_evidence=("claim-goal",), task_fence=FENCE,
    )
    await goals.transition_goal(
        run_id=RUN, goal_id="goal-complete", target_state="active",
        expected_version=1, task_fence=FENCE,
    )
    await goals.transition_goal(
        run_id=RUN, goal_id="goal-complete", target_state="completed",
        expected_version=2, task_fence=FENCE,
    )
    await goals.create_goal(
        run_id=RUN, goal_id="goal-open", owner="worker-a",
        completion_evidence=("claim-goal",), task_fence=FENCE,
    )
    outcome = await evidence.revoke_decision(
        run_id=RUN,
        decision_id=decided["decision_id"],
        reason="retracted",
        task_fence=FENCE,
    )
    assert outcome["goal_rollbacks"] == ["goal-complete"]
    rolled = await goals.get_goal("goal-complete")
    assert rolled["state"] == "blocked"
    assert rolled["revalidation_required"] == 1
    open_goal = await goals.get_goal("goal-open")
    assert open_goal["revalidation_required"] == 1
    goal_markers = await evidence.list_revalidation_markers(
        target_kind="goal",
    )
    assert {marker["target_id"] for marker in goal_markers} == {
        "goal-complete", "goal-open",
    }
