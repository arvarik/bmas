"""Foundation Stage 0D: journal replay, snapshots, and integrity.

Replay from journal cursor zero rebuilds every verified projection.
Snapshots accelerate replay only after verification, a corrupted
snapshot falls back to full replay, and a corrupted journal digest
stops replay with a visible integrity failure.
"""
from __future__ import annotations

import json
import sqlite3

import journal_test_support as support
import pytest

import database as db
import runtime_journal as journal


@pytest.fixture()
async def journal_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "journal.db"))
    await db.init_db()
    return tmp_path


@pytest.mark.asyncio
async def test_replay_from_zero_rebuilds_every_projection(journal_db):
    await support.seed_full_run()
    result = await journal.replay()
    state = result.state

    # Every typed ledger and shared index rebuilds from the journal.
    assert state["runs"][support.RUN_ID]["state"] == "completed"
    assert state["admissions"][support.RUN_ID]["admission_id"] == (
        f"admission-{support.RUN_ID}"
    )
    assert state["activations"][support.RUN_ID]["activation-a"] == "completed"
    assert state["effects"][support.RUN_ID]["effect-a"] == "completed"
    assert state["checkpoints"][support.RUN_ID] == "6" * 64
    assert state["circuits"][support.RUN_ID]["decision"] == "allow"
    assert state["budgets"][support.RUN_ID] == {
        "reserved": 2000,
        "consumed": 2000,
    }
    assert state["evidence"]["claim-a"] == "verified"
    assert state["goals"]["goal-a"] == "satisfied"
    assert [c["operation"] for c in state["controls"][support.RUN_ID]] == [
        "pause", "resume",
    ]
    assert state["outcomes"][support.RUN_ID]["common_class"] == "success"
    assert state["invalidation_validity"][
        "projection:projection-current"
    ]["current"] is False
    assert result.status == "complete"

    # The durable projections agree with the replay.
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_replay_is_deterministic_and_digest_stable(journal_db):
    await support.seed_full_run()
    first = await journal.replay()
    second = await journal.replay()
    assert first.state_digest == second.state_digest
    assert first.last_cursor == second.last_cursor


@pytest.mark.asyncio
async def test_snapshots_at_several_cursors_reach_the_same_digest(
    journal_db,
):
    await support.seed_full_run()
    records = await journal.read_journal()
    full = await journal.replay()

    for prefix_length in (2, 5, 9, len(records)):
        state = journal.empty_projection_state()
        for record in records[:prefix_length]:
            state = journal.apply_record_to_state(state, record)
        partial = journal.ReplayResult(
            state=state,
            last_cursor=records[prefix_length - 1].journal_cursor,
            state_digest=journal.projection_digest(state),
            status=str(state["replay_status"]["status"]),
            used_snapshot=False,
        )
        snapshot = journal.create_snapshot(partial)
        journal.verify_snapshot(snapshot)
        accelerated = await journal.replay(snapshot=snapshot)
        assert accelerated.used_snapshot is True
        assert accelerated.state_digest == full.state_digest
        assert accelerated.last_cursor == full.last_cursor


@pytest.mark.asyncio
async def test_a_corrupted_snapshot_falls_back_to_full_replay(journal_db):
    await support.seed_full_run()
    full = await journal.replay()
    snapshot = journal.create_snapshot(full)

    tampered = json.loads(json.dumps(snapshot))
    tampered["state"]["runs"][support.RUN_ID]["state"] = "running"
    with pytest.raises(journal.SnapshotVerificationError):
        journal.verify_snapshot(tampered)
    recovered = await journal.replay(snapshot=tampered)
    assert recovered.used_snapshot is False
    assert recovered.state_digest == full.state_digest

    unknown_cursor = json.loads(json.dumps(snapshot))
    unknown_cursor["last_journal_cursor"] = 9_999
    body = {
        key: value
        for key, value in unknown_cursor.items()
        if key != "snapshot_digest"
    }
    unknown_cursor["snapshot_digest"] = journal.digest_hex(
        journal.SNAPSHOT_DIGEST_DOMAIN, body,
    )
    recovered = await journal.replay(snapshot=unknown_cursor)
    assert recovered.used_snapshot is False
    assert recovered.state_digest == full.state_digest


@pytest.mark.asyncio
async def test_a_corrupted_journal_digest_stops_replay(journal_db):
    await support.seed_full_run()
    raw = sqlite3.connect(db.DB_PATH)
    try:
        # Tampering requires dropping the immutability trigger first.
        raw.execute("DROP TRIGGER runtime_journal_immutable_update")
        raw.execute(
            "UPDATE runtime_journal SET payload = "
            "json_set(payload, '$.activation_state', 'forged') "
            "WHERE operation_type = 'activation_transition'"
        )
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(journal.JournalIntegrityError):
        await journal.replay()


@pytest.mark.asyncio
async def test_a_rejected_proposal_changes_only_shared_projections(
    journal_db,
):
    await journal.commit_operation(support.admission_operation())
    await journal.commit_operation(
        support.proposal_operation(decision="accepted"),
    )
    accepted = await journal.replay()

    await journal.commit_operation(
        support.proposal_operation(decision="rejected"),
    )
    rejected = await journal.replay()

    # The runtime state and checkpoint stay untouched by the rejection.
    assert rejected.state["runtime_state"] == accepted.state["runtime_state"]
    assert rejected.state["checkpoints"] == accepted.state["checkpoints"]
    assert rejected.state["budgets"] == accepted.state["budgets"]
    # The declared shared projections did change.
    assert len(rejected.state["traces"][support.RUN_ID]) == (
        len(accepted.state["traces"][support.RUN_ID]) + 1
    )


@pytest.mark.asyncio
async def test_the_legacy_journal_contrast_holds(journal_db):
    async with db._connect() as connection:  # noqa: SLF001
        legacy_fk = await connection.execute_fetchall(
            "PRAGMA foreign_key_list(event_journal)"
        )
        legacy_columns = {
            row[1]
            for row in await connection.execute_fetchall(
                "PRAGMA table_info(event_journal)"
            )
        }
        journal_fk = await connection.execute_fetchall(
            "PRAGMA foreign_key_list(runtime_journal)"
        )
        journal_columns = {
            row[1]
            for row in await connection.execute_fetchall(
                "PRAGMA table_info(runtime_journal)"
            )
        }
    # The legacy table cascades from tasks and mutates published_at.
    assert any(
        row[2] == "tasks" and str(row[6]).upper() == "CASCADE"
        for row in legacy_fk
    )
    assert "published_at" in legacy_columns
    # The immutable journal has neither a cascade nor a delivery field.
    assert list(journal_fk) == []
    assert "published_at" not in journal_columns


@pytest.mark.asyncio
async def test_an_erased_replay_critical_artifact_marks_replay_partial(
    journal_db,
):
    await journal.commit_operation(support.admission_operation())
    await journal.commit_operation(
        support.evidence_operation(
            payload={
                "claim_id": "claim-erased",
                "evidence_state": "recorded",
                "replay_critical_artifacts": [
                    {"content_digest": "7" * 64, "erased": True},
                ],
            },
        )
    )
    result = await journal.replay()
    assert result.status == "partial"
    assert result.state["replay_status"]["redactions"][0]["reason"] == (
        "redacted_by_policy"
    )


@pytest.mark.asyncio
async def test_chain_compaction_starts_a_new_epoch_with_a_manifest(
    journal_db,
):
    await support.seed_full_run()
    with pytest.raises(journal.JournalError, match="two distinct"):
        await journal.compact_chain(
            support.RUN_ID,
            approver_ids=("operator-lead", "operator-lead"),
            erasure_manifest={"reason": "policy erasure"},
        )
    record = await journal.compact_chain(
        support.RUN_ID,
        approver_ids=("operator-lead", "privacy-officer"),
        erasure_manifest={"reason": "policy erasure", "fields": ["payload"]},
    )
    assert record.chain_epoch == 2
    assert record.authority_type == "privileged_migration"
    assert sorted(record.payload["approver_ids"]) == [
        "operator-lead", "privacy-officer",
    ]
    # The old epoch is gone, and replay reports the policy redaction.
    remaining = await journal.read_journal()
    assert [entry.chain_epoch for entry in remaining] == [2]
    journal.verify_chain(remaining)
    result = await journal.replay()
    assert result.status == "partial"
    assert result.state["replay_status"]["redactions"][0]["reason"] == (
        "redacted_by_policy"
    )
    assert result.state["runs"][support.RUN_ID]["state"] == "completed"


@pytest.mark.asyncio
async def test_two_runs_keep_independent_chains(journal_db):
    await support.seed_full_run("run-first")
    await support.seed_full_run("run-second")
    records = await journal.read_journal()
    journal.verify_chain(records)
    result = await journal.replay()
    assert result.state["runs"]["run-first"]["state"] == "completed"
    assert result.state["runs"]["run-second"]["state"] == "completed"
    await journal.verify_durable_projections()
