"""Foundation Stage 0D: unit-of-work atomicity under injected crashes.

A fault before or after every durable write leaves one valid result:
no record commits, or the complete transaction commits once. No fault
can create a projection without its journal cursor, and no fault can
create an outbox row without its journal transaction.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import journal_test_support as support
import pytest

import database as db
import runtime_journal as journal
from core import failpoints
from core.failpoints import InjectedFaultError

JOURNAL_TABLES = (
    "runtime_journal",
    "runs",
    "runtime_admissions",
    "run_queue",
    "journal_delivery",
    "journal_outbox",
    "activation_dispatch_outbox",
    "effect_dispatch_outbox",
    "task_tombstones",
)

PRE_COMMIT_FAILPOINTS = tuple(
    name for name in journal.JOURNAL_FAILPOINTS
    if name != "journal.after_commit"
)


@pytest.fixture(autouse=True)
def clean_failpoints():
    failpoints.clear()
    yield
    failpoints.clear()


@pytest.fixture()
async def journal_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "journal.db"))
    await db.init_db()
    return tmp_path


async def table_counts() -> dict[str, int]:
    counts = {}
    async with db._connect() as connection:  # noqa: SLF001
        for table in JOURNAL_TABLES:
            cursor = await connection.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            counts[table] = int(row[0])
    return counts


@pytest.mark.asyncio
@pytest.mark.parametrize("failpoint_name", PRE_COMMIT_FAILPOINTS)
@pytest.mark.parametrize(
    "operation_index", range(len(support.full_run_operations())),
)
async def test_a_crash_before_commit_leaves_no_record(
    journal_db, failpoint_name, operation_index,
):
    operations = support.full_run_operations()
    for operation in operations[:operation_index]:
        await journal.commit_operation(operation)
    before = await table_counts()

    failpoints.arm(failpoint_name)
    with pytest.raises(InjectedFaultError):
        await journal.commit_operation(operations[operation_index])

    # No record of any kind committed.
    assert await table_counts() == before

    # The same operation then commits completely exactly once.
    record = await journal.commit_operation(operations[operation_index])
    after = await table_counts()
    assert after["runtime_journal"] == before["runtime_journal"] + 1
    assert record.journal_cursor > 0


@pytest.mark.asyncio
async def test_a_crash_after_commit_keeps_the_complete_transaction(
    journal_db,
):
    failpoints.arm("journal.after_commit")
    with pytest.raises(InjectedFaultError):
        await journal.commit_operation(support.admission_operation())
    counts = await table_counts()
    assert counts["runtime_journal"] == 1
    assert counts["runs"] == 1
    assert counts["runtime_admissions"] == 1
    assert counts["run_queue"] == 1
    assert counts["journal_outbox"] == 1
    # A retry after the crash returns the stored record idempotently.
    record = await journal.commit_operation(support.admission_operation())
    assert record.run_sequence == 0
    assert (await table_counts())["runtime_journal"] == 1


@pytest.mark.asyncio
async def test_no_projection_exists_without_its_journal_cursor(journal_db):
    await support.seed_full_run()
    async with db._connect() as connection:  # noqa: SLF001
        for table in (
            "runs", "runtime_admissions", "run_queue", "journal_outbox",
            "activation_dispatch_outbox", "effect_dispatch_outbox",
        ):
            cursor = await connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE journal_cursor "
                "NOT IN (SELECT journal_cursor FROM runtime_journal)"
            )
            row = await cursor.fetchone()
            assert int(row[0]) == 0, table


@pytest.mark.asyncio
async def test_the_full_proposal_write_set_commits_together(journal_db):
    await journal.commit_operation(support.admission_operation())
    await journal.commit_operation(
        support.proposal_operation(
            activation_dispatch_id="activation-a",
            effect_dispatch_id="effect-a",
        )
    )
    counts = await table_counts()
    assert counts["activation_dispatch_outbox"] == 1
    assert counts["effect_dispatch_outbox"] == 1
    assert counts["journal_outbox"] == 2


@pytest.mark.asyncio
async def test_idempotent_repeat_and_conflicting_reuse(journal_db):
    first = await journal.commit_operation(support.admission_operation())
    repeat = await journal.commit_operation(support.admission_operation())
    assert repeat.transaction_id == first.transaction_id
    changed = support.admission_operation()
    changed = support.admission_operation(
        payload={**changed.payload, "specification_digest": "9" * 64},
    )
    with pytest.raises(journal.JournalConflictError):
        await journal.commit_operation(changed)
    assert (await table_counts())["runtime_journal"] == 1


@pytest.mark.asyncio
async def test_the_task_fence_validates_inside_the_transaction(journal_db):
    await journal.commit_operation(support.admission_operation())
    await db.create_run_control(
        support.RUN_ID, support.TASK_ID, "fence-live",
    )
    with pytest.raises(journal.JournalFenceError):
        await journal.commit_operation(
            support.activation_operation(task_fence="fence-stale"),
        )
    record = await journal.commit_operation(
        support.activation_operation(task_fence="fence-live"),
    )
    assert record.run_sequence == 1


@pytest.mark.asyncio
async def test_optimistic_projection_versions_validate(journal_db):
    await journal.commit_operation(support.admission_operation())
    with pytest.raises(journal.JournalError, match="optimistic"):
        await journal.commit_operation(
            support.activation_operation(expected_projection_version=7),
        )
    assert await journal.commit_operation(
        support.activation_operation(expected_projection_version=1),
    )


@pytest.mark.asyncio
async def test_the_run_must_belong_to_the_stated_task(journal_db):
    await journal.commit_operation(support.admission_operation())
    with pytest.raises(journal.JournalError, match="stated task"):
        await journal.commit_operation(
            support.activation_operation(task_id="task-other"),
        )
    with pytest.raises(journal.JournalError, match="one immutable admission"):
        await journal.commit_operation(
            support.admission_operation(
                idempotency_token="admission-second",
            ),
        )
    with pytest.raises(journal.JournalError, match="admission"):
        await journal.commit_operation(
            support.activation_operation(run_id="run-unseen"),
        )


@pytest.mark.asyncio
async def test_exactly_one_terminal_outcome_per_run(journal_db):
    await support.seed_full_run()
    with pytest.raises(journal.JournalError, match="post-terminal"):
        await journal.commit_operation(
            support.activation_operation(state="late"),
        )
    duplicate = support.terminal_operation(
        idempotency_token="terminal-second",
    )
    with pytest.raises(journal.JournalError, match="exactly one"):
        await journal.commit_operation(duplicate)


@pytest.mark.asyncio
async def test_an_invalidation_names_the_stored_outcome(journal_db):
    await support.seed_full_run(run_id="run-check")
    wrong = support.invalidation_operation(run_id="run-check")
    wrong = support.invalidation_operation(
        run_id="run-check",
        idempotency_token="invalidation-wrong",
        payload={**wrong.payload, "outcome_digest": "0" * 64},
    )
    with pytest.raises(journal.JournalError, match="wrong outcome"):
        await journal.commit_operation(wrong)


@pytest.mark.asyncio
async def test_journal_rows_are_immutable(journal_db):
    record = await journal.commit_operation(support.admission_operation())
    raw = sqlite3.connect(db.DB_PATH)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            raw.execute(
                "UPDATE runtime_journal SET payload = '{}' "
                "WHERE journal_cursor = ?",
                (record.journal_cursor,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            raw.execute(
                "DELETE FROM runtime_journal WHERE journal_cursor = ?",
                (record.journal_cursor,),
            )
    finally:
        raw.close()


@pytest.mark.asyncio
async def test_no_authority_event_is_dual_written(journal_db):
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute("SELECT COUNT(*) FROM event_journal")
        legacy_before = int((await cursor.fetchone())[0])
    await support.seed_full_run()
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute("SELECT COUNT(*) FROM event_journal")
        legacy_after = int((await cursor.fetchone())[0])
    assert legacy_after == legacy_before
    source = Path(journal.__file__).read_text(encoding="utf-8")
    assert "event_journal" not in source


@pytest.mark.asyncio
async def test_task_deletion_cannot_cascade_into_the_journal(journal_db):
    await db.create_task_with_meta(
        support.TASK_ID,
        "journal task",
        "journal task",
        "classic",
        {},
        runtime_contract_version="1",
    )
    await support.seed_full_run()
    counts_before = await table_counts()
    assert await db.delete_task(support.TASK_ID) is True
    counts_after = await table_counts()
    assert counts_after["runtime_journal"] == counts_before["runtime_journal"]
    assert counts_after["runs"] == counts_before["runs"]
    await journal.record_task_tombstone(
        support.TASK_ID, erasure_state="recorded",
    )
    assert (await table_counts())["task_tombstones"] == 1


@pytest.mark.asyncio
async def test_mutable_delivery_state_cannot_change_an_authority_row(
    journal_db,
):
    record = await journal.commit_operation(support.admission_operation())
    stored_before = await journal.read_journal()
    await journal.publish_journal_delivery(record.journal_cursor)
    retried = await journal.publish_journal_delivery(record.journal_cursor)
    assert retried["attempts"] == 2
    assert retried["delivery_state"] == "published"
    assert await journal.read_journal() == stored_before
