"""Foundation Stage 0G: goal concurrency, merges, and rollback.

Optimistic versions give one commit and one conflict, only the
declared transition table passes, duplicate goals merge through the
deterministic rule with the earliest identifier canonical, a merge
can never weaken completion evidence, dependency cycles reject before
persistence, and revoked completion evidence rolls a completed goal
back to blocked as one journaled transaction.
"""
from __future__ import annotations

import asyncio
import itertools

import protocol_test_support as support
import pytest

import database as db
import evidence_service as evidence
import goal_service as goals
import runtime_journal as journal
import typed_indexes as indexes

RUN = support.RUN_ID
FENCE = support.TASK_FENCE


@pytest.fixture()
async def goal_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "goals.db"))
    await db.init_db()
    await support.seed_run()
    return tmp_path


async def create(goal_id: str, **overrides):
    arguments = dict(
        run_id=RUN, goal_id=goal_id, owner="worker-a", task_fence=FENCE,
    )
    arguments.update(overrides)
    return await goals.create_goal(**arguments)


async def support_claim(claim_id: str):
    await evidence.register_claim(
        run_id=RUN, claim_id=claim_id, statement_digest="1" * 64,
        policy=evidence.REGISTERED_POLICIES["deterministic-single"],
        task_fence=FENCE,
    )
    return await evidence.record_decision(
        run_id=RUN, claim_id=claim_id, verifier_id="checker",
        verifier_capability="deterministic", verifier_version="1",
        independence_group="deterministic", verdict="supported",
        confidence=100, task_fence=FENCE,
    )


# ── The transition table ─────────────────────────────────────────────


def test_only_declared_goal_transitions_pass():
    for current, target in itertools.product(
        goals.GOAL_STATES, goals.GOAL_STATES,
    ):
        declared = (current, target) in goals.GOAL_TRANSITIONS
        if declared:
            assert goals.validate_goal_transition(current, target)
        else:
            with pytest.raises(goals.GoalServiceError):
                goals.validate_goal_transition(current, target)
    with pytest.raises(goals.GoalServiceError):
        goals.validate_goal_transition("proposed", "daydreaming")


async def test_goal_row_stores_the_complete_identity(goal_db):
    await create(
        "goal-full",
        parent_goal_id=None,
        dependency_ids=(),
        transition_policy="shared",
        completion_evidence=("claim-x",),
        merge_key="deliver",
    )
    row = await goals.get_goal("goal-full")
    assert row["version"] == 1
    assert row["owner"] == "worker-a"
    assert row["runtime_id"] == "classic"
    assert row["runtime_contract_version"] == "1"
    assert row["merge_key"] == "deliver"
    assert row["state"] == "proposed"


# ── Optimistic concurrency ───────────────────────────────────────────


async def test_equal_versions_give_one_commit_and_one_conflict(goal_db):
    await create("goal-race")
    results = await asyncio.gather(
        goals.update_goal(
            run_id=RUN, goal_id="goal-race", expected_version=1,
            changes={"owner": "worker-b"}, task_fence=FENCE,
        ),
        goals.update_goal(
            run_id=RUN, goal_id="goal-race", expected_version=1,
            changes={"owner": "worker-c"}, task_fence=FENCE,
        ),
        return_exceptions=True,
    )
    commits = [entry for entry in results if isinstance(entry, dict)]
    conflicts = [
        entry for entry in results
        if isinstance(entry, goals.GoalConflictError)
    ]
    assert len(commits) == 1
    assert len(conflicts) == 1
    row = await goals.get_goal("goal-race")
    assert row["version"] == 2


async def test_a_stale_version_conflicts(goal_db):
    await create("goal-stale")
    await goals.update_goal(
        run_id=RUN, goal_id="goal-stale", expected_version=1,
        changes={"owner": "worker-b"}, task_fence=FENCE,
    )
    with pytest.raises(goals.GoalConflictError):
        await goals.update_goal(
            run_id=RUN, goal_id="goal-stale", expected_version=1,
            changes={"owner": "worker-c"}, task_fence=FENCE,
        )
    with pytest.raises(goals.GoalConflictError):
        await goals.transition_goal(
            run_id=RUN, goal_id="goal-stale", target_state="active",
            expected_version=1, task_fence=FENCE,
        )


# ── Cycles ───────────────────────────────────────────────────────────


async def test_dependency_cycles_reject_before_persistence(goal_db):
    await create("goal-one")
    await create("goal-two", dependency_ids=("goal-one",))
    with pytest.raises(goals.GoalCycleError):
        await create("goal-cycle", dependency_ids=("goal-cycle",))
    with pytest.raises(goals.GoalCycleError):
        await goals.update_goal(
            run_id=RUN, goal_id="goal-one", expected_version=1,
            changes={"dependency_ids": ["goal-two"]}, task_fence=FENCE,
        )
    with pytest.raises(goals.GoalServiceError):
        await goals.get_goal("goal-cycle")
    # A parent edge participates in cycle detection.
    with pytest.raises(goals.GoalCycleError):
        await create("goal-three", parent_goal_id="goal-three")


# ── Deterministic merges ─────────────────────────────────────────────


async def test_duplicates_merge_with_the_earliest_identifier_canonical(
    goal_db,
):
    await create(
        "goal-early", merge_key="ship",
        completion_evidence=("claim-a",),
        database_time="2026-08-31T01:00:00.000Z",
    )
    await create(
        "goal-later", merge_key="ship",
        completion_evidence=("claim-b",),
        database_time="2026-08-31T02:00:00.000Z",
    )
    merged = await goals.merge_duplicate_goals(
        run_id=RUN, merge_key="ship", task_fence=FENCE,
    )
    assert merged["canonical_goal_id"] == "goal-early"
    assert merged["aliases"] == ["goal-later"]
    # The canonical keeps the union of completion evidence.
    assert merged["completion_evidence"] == ["claim-a", "claim-b"]
    alias = await goals.get_goal("goal-later")
    assert alias["state"] == "merged"
    assert alias["alias_of"] == "goal-early"
    canonical = await goals.get_goal("goal-early")
    assert canonical["state"] == "proposed"
    # The merge is deterministic: a repeat has nothing left to merge.
    with pytest.raises(goals.GoalMergeError):
        await goals.merge_duplicate_goals(
            run_id=RUN, merge_key="ship", task_fence=FENCE,
        )


async def test_a_merge_that_weakens_completion_evidence_rejects(goal_db):
    await create(
        "goal-strong", merge_key="deliver",
        completion_evidence=("claim-a", "claim-b"),
        database_time="2026-08-31T01:00:00.000Z",
    )
    await create(
        "goal-weak", merge_key="deliver",
        completion_evidence=("claim-a",),
        database_time="2026-08-31T02:00:00.000Z",
    )
    with pytest.raises(goals.GoalMergeError):
        await goals.merge_duplicate_goals(
            run_id=RUN, merge_key="deliver",
            proposed_completion_evidence=("claim-a",),
            task_fence=FENCE,
        )
    # The union or a superset passes.
    merged = await goals.merge_duplicate_goals(
        run_id=RUN, merge_key="deliver",
        proposed_completion_evidence=("claim-a", "claim-b", "claim-c"),
        task_fence=FENCE,
    )
    assert merged["completion_evidence"] == ["claim-a", "claim-b", "claim-c"]


# ── Completion evidence and rollback ─────────────────────────────────


async def test_completion_requires_supported_evidence(goal_db):
    await create("goal-gate", completion_evidence=("claim-need",))
    await goals.transition_goal(
        run_id=RUN, goal_id="goal-gate", target_state="active",
        expected_version=1, task_fence=FENCE,
    )
    with pytest.raises(goals.GoalServiceError):
        await goals.transition_goal(
            run_id=RUN, goal_id="goal-gate", target_state="completed",
            expected_version=2, task_fence=FENCE,
        )
    await support_claim("claim-need")
    await goals.transition_goal(
        run_id=RUN, goal_id="goal-gate", target_state="completed",
        expected_version=2, task_fence=FENCE,
    )
    assert (await goals.get_goal("goal-gate"))["state"] == "completed"


async def test_revoked_evidence_rolls_back_as_a_journaled_transaction(
    goal_db,
):
    decided = await support_claim("claim-rollback")
    await create("goal-rollback", completion_evidence=("claim-rollback",))
    await goals.transition_goal(
        run_id=RUN, goal_id="goal-rollback", target_state="active",
        expected_version=1, task_fence=FENCE,
    )
    await goals.transition_goal(
        run_id=RUN, goal_id="goal-rollback", target_state="completed",
        expected_version=2, task_fence=FENCE,
    )
    cursor_before = len(await journal.read_journal())
    await evidence.revoke_decision(
        run_id=RUN, decision_id=decided["decision_id"],
        reason="retracted", task_fence=FENCE,
    )
    row = await goals.get_goal("goal-rollback")
    assert row["state"] == "blocked"
    # The rollback is a new journal transaction, not an edit.
    rollback_records = [
        record
        for record in (await journal.read_journal())[cursor_before:]
        if record.operation_type == "goal_update"
        and record.payload.get("rollback_reason") is not None
    ]
    assert len(rollback_records) == 1
    # The durable goal index still equals journal replay.
    await indexes.assert_indexes_match_journal(RUN)
