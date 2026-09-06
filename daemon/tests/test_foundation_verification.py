"""Verification tests the evaluation handoff gate listed as missing.

Concurrent writers on one run keep one ordered digest chain. A full
disk at any journal write leaves no partial record, and the retry
commits once. The scheduled restore test measures recovery time and
recovery point against the declared objectives and records the
outcome for the backup health queue. A repeatability pair records two
executions of the same case and claims no repeatability.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import types

import journal_test_support as support
import pytest
import pytest_asyncio
from test_settlement_evidence_and_sizing import _finish_task
from test_unit_of_work_atomicity import table_counts

import database as db
import edge_access
import recovery_center
import restore_test
import runtime_journal as journal
from benchmarks import admission, replay_bundle, repository
from benchmarks.provenance import content_checksum
from core import failpoints


@pytest.fixture(autouse=True)
def clean_failpoints():
    failpoints.clear()
    yield
    failpoints.clear()


@pytest_asyncio.fixture
async def journal_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "journal.db"))
    await db.init_db()
    return tmp_path


@pytest.mark.asyncio
async def test_concurrent_writers_keep_one_ordered_chain(journal_db):
    await journal.commit_operation(support.admission_operation())
    operations = [
        support.evidence_operation(
            idempotency_token=f"evidence-race-{index}",
            payload={"claim_id": f"claim-{index}", "evidence_state": "verified"},
        )
        for index in range(12)
    ]
    records = await asyncio.gather(
        *(journal.commit_operation(operation) for operation in operations),
    )
    assert sorted(record.run_sequence for record in records) == list(range(1, 13))
    assert len({record.journal_cursor for record in records}) == 12
    chain = await journal.read_journal(run_id=support.RUN_ID)
    assert [record.run_sequence for record in chain] == list(range(13))
    journal.verify_chain(chain)
    for previous, current in zip(chain, chain[1:], strict=False):
        assert current.previous_digest == previous.transaction_digest


@pytest.mark.asyncio
@pytest.mark.parametrize("failpoint_name", [
    "journal.before_journal_insert",
    "journal.before_projection_write",
    "journal.before_outbox_write",
    "journal.before_commit",
])
async def test_a_full_disk_leaves_no_partial_record(journal_db, monkeypatch, failpoint_name):
    await journal.commit_operation(support.admission_operation())
    before = await table_counts()
    real_failpoint = journal.failpoint
    fired = {"count": 0}

    def full_disk(name: str) -> None:
        if name == failpoint_name and fired["count"] == 0:
            fired["count"] += 1
            raise sqlite3.OperationalError("database or disk is full")
        real_failpoint(name)

    monkeypatch.setattr(journal, "failpoint", full_disk)
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        await journal.commit_operation(support.evidence_operation())
    assert fired["count"] == 1
    assert await table_counts() == before
    # The retry after the disk recovers commits the operation exactly once.
    record = await journal.commit_operation(support.evidence_operation())
    assert record.run_sequence == 1
    assert (await table_counts())["runtime_journal"] == before["runtime_journal"] + 1
    journal.verify_chain(await journal.read_journal())


@pytest.mark.asyncio
async def test_the_scheduled_restore_test_measures_recovery_objectives(journal_db, tmp_path):
    await support.seed_full_run()
    result = await restore_test.run_restore_test(tmp_path / "backups", application_commit="test-commit")
    assert result["state"] == "succeeded", result
    assert result["findings"] == []
    assert result["measured"]["recovery_point_lag"] == 0
    assert result["measured"]["recovery_point_cursor"] == result["measured"]["live_cursor"] == 12
    assert result["measured"]["replay_status"] == "complete"
    assert result["measured"]["recovery_seconds"] <= restore_test.RESTORE_OBJECTIVES["recovery_time_seconds_max"]
    assert not (tmp_path / "backups" / "restore-tests" / result["backup_id"]).exists()
    healthy = await recovery_center.list_queue("backup_health", principal=edge_access.LOCAL_OPERATOR)
    assert healthy == []
    # An objective the restore cannot meet fails the test and lands in the queue.
    strict = await restore_test.run_restore_test(
        tmp_path / "backups", objectives={"recovery_time_seconds_max": 0.0},
    )
    assert strict["state"] == "failed"
    assert strict["findings"] == ["recovery_time_exceeded"]
    unhealthy = await recovery_center.list_queue("backup_health", principal=edge_access.LOCAL_OPERATOR)
    assert [item["item_id"] for item in unhealthy] == [strict["outcome"]["backup_id"]]
    assert unhealthy[0]["evidence"]["kind"] == "restore_test"
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT details FROM backup_records WHERE backup_id = ?", (strict["outcome"]["backup_id"],),
        )
        row = await cursor.fetchone()
    details = json.loads(row["details"])
    assert details["objectives"]["recovery_time_seconds_max"] == 0.0
    assert details["measured"]["recovery_point_lag"] == 0
    # The loop runs one iteration and stops.
    await restore_test.restore_test_loop(interval_seconds=0, backup_root=tmp_path / "backups", iterations=1)
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute("SELECT COUNT(*) FROM backup_records WHERE kind = 'restore_test'")
        row = await cursor.fetchone()
    assert int(row[0]) == 3


async def _pair_run() -> list[dict]:
    """One run with two repetitions of one case under one seed."""
    await db.create_dataset_version(
        dataset_id="dataset-pair", version_id="version-pair", name="Pair", description="",
        source_uri=None, license_name=None, author=None, dataset_metadata={},
        checksum="pair-checksum", schema={"version": "1"}, source_filename="pair.jsonl",
        source_mime="application/x-ndjson", source_checksum="pair-source", source_path="/tmp/pair.jsonl",
        version_metadata={},
        items=[{"id": "item-pair", "item_key": "case-pair", "input": "What is 20 plus 22?",
                "expected_output": "42", "subject": "math", "split": "test", "tags": [], "metadata": {}}],
    )
    envelope = {"runtime_id": "classic", "effective_configuration": {"model_routing": {"medium": "model-a"}}}
    await repository.create_test_revision(
        test_id="test-pair", revision_id="revision-pair", name="pair", description="",
        dataset_version_id="version-pair", configuration={"repetitions": 2, "seed": 7},
        arms=[{"id": "arm-pair", "name": "Classic", "slug": "classic", "runtime_id": "classic",
               "configuration": envelope, "configuration_checksum": content_checksum(envelope)}],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )
    await repository.create_run(run_id="run-pair", revision_id="revision-pair", idempotency_key=None)
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT attempt.id, attempt.repeat_index, attempt.random_seed, trial.dataset_item_id "
            "FROM benchmark_attempts attempt JOIN benchmark_trials trial ON trial.id = attempt.trial_id "
            "ORDER BY attempt.repeat_index",
        )
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_a_repeatability_pair_records_two_executions_without_a_claim(journal_db):
    attempts = await _pair_run()
    assert len(attempts) == 2
    assert {row["dataset_item_id"] for row in attempts} == {"item-pair"}
    assert [row["repeat_index"] for row in attempts] == [1, 2]
    await _finish_task(attempts[0]["id"], "task-pair-1", cost=0.03, tokens=900)
    await _finish_task(attempts[1]["id"], "task-pair-2", cost=0.03, tokens=900)
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("UPDATE tasks SET result_summary = '41' WHERE id = 'task-pair-2'")
        await connection.commit()
    first = await admission.capture_settled_evidence(attempts[0]["id"])
    second = await admission.capture_settled_evidence(attempts[1]["id"])
    assert first is not None and second is not None
    # Both executions record the same case and their own seed evidence.
    assert first["record"]["case_reference"] == second["record"]["case_reference"]
    assert first["record"]["seed_evidence"] and second["record"]["seed_evidence"]
    assert first["record"]["final_output_digest"] != second["record"]["final_output_digest"]
    # Neither bundle claims repeatability, and an execution repeat never starts by itself.
    for bundle in (first, second):
        assert "repeatability" not in bundle["record"]
        assert "repeatable" not in json.dumps(bundle["record"]["versions"]).lower()
    requirements = replay_bundle.execution_repeat_requirements(types.SimpleNamespace(import_id="import-pair"))
    assert requirements["started"] is False
    assert "new_run_plan" in requirements["requires"]
