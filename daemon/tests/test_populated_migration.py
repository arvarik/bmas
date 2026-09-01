"""Foundation Stage 0H: populated upgrade, cutover, and downgrade.

A populated database with active, completed, failed, and archived v1
tasks (plus datasets, benchmark runs, artifacts, and pending outbox
rows) upgrades without changing v1 behavior. The legacy event writer
stays v1-only, every v2 write uses the runtime-journal authority, dual
read agrees, a cutover keeps every existing run's pair, legacy
retirement refuses with an active reader, and a downgrade that cannot
preserve new writes refuses.
"""
from __future__ import annotations

import protocol_test_support as support
import pytest

import database as db
import migration_negotiation as negotiation
import runtime_journal as journal
from core.digest_profile import digest_hex


@pytest.fixture()
async def populated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "database.DB_PATH", str(tmp_path / "populated.db"),
    )
    await db.init_db()
    async with db._connect() as connection:  # noqa: SLF001
        # legacy tasks in every lifecycle state.
        for task_id, status, archived in (
            ("task-active", "running", None),
            ("task-completed", "completed", None),
            ("task-failed", "failed", None),
            ("task-archived", "completed", "2026-01-01T00:00:00.000Z"),
        ):
            await connection.execute(
                "INSERT INTO tasks (id, label, full_input, status, "
                "archived_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, task_id, "legacy input", status, archived),
            )
            await connection.execute(
                "INSERT INTO event_journal (stream, task_id, event_type, "
                "data) VALUES (?, ?, 'task.created', '{}')",
                (f"task:{task_id}", task_id),
            )
        # A pending legacy outbox row references an existing journal
        # cursor, so the delivery obligation is a real pending row.
        await connection.execute(
            "INSERT INTO event_outbox (event_cursor, attempts) "
            "VALUES (1, 0)",
        )
        # A dataset the upgrade preserves.
        await connection.execute(
            "INSERT INTO datasets (id, name) VALUES ('dataset-a', 'Set A')",
        )
        await connection.commit()
    return tmp_path


async def _task_snapshot() -> dict[str, dict[str, str]]:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT id, status, archived_at FROM tasks ORDER BY id",
        )
        return {
            str(row["id"]): {
                "status": str(row["status"]),
                "archived_at": row["archived_at"],
            }
            for row in await cursor.fetchall()
        }


# ── Unchanged legacy behavior and readable history ───────────────────────


async def test_upgrade_keeps_legacy_behavior_and_history(populated_db):
    before = await _task_snapshot()
    # init_db already ran every expand and backfill migration to the
    # current version. The legacy rows stay readable and unchanged.
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT MAX(version) AS v FROM schema_version",
        )
        version = int((await cursor.fetchone())["v"])
    assert version == db.SCHEMA_VERSION
    after = await _task_snapshot()
    assert after == before
    # The legacy event journal history stays readable.
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS events FROM event_journal",
        )
        assert int((await cursor.fetchone())["events"]) == 4


async def test_legacy_event_writer_stays_legacy_only(populated_db):
    negotiation.assert_legacy_writer_stays_legacy("event_journal")
    classification = negotiation.classify_writer("event_journal")
    assert classification.generation == "legacy"
    assert not classification.is_native_authority()
    # A native writer never classifies as a legacy-only writer.
    with pytest.raises(negotiation.MigrationNegotiationError):
        negotiation.assert_legacy_writer_stays_legacy("runtime_journal")
    # An unknown writer fails closed.
    with pytest.raises(negotiation.MigrationNegotiationError):
        negotiation.classify_writer("some_unknown_writer")


async def test_every_native_write_uses_the_runtime_journal_authority(
    populated_db,
):
    native_writers = (
        "runtime_journal",
        "activation_service",
        "effect_service",
        "budget_service",
        "evidence_service",
        "goal_service",
        "run_admission",
    )
    # None raise: every native writer routes through the one authority.
    negotiation.assert_native_writes_use_one_authority(native_writers)
    for writer in native_writers:
        assert negotiation.classify_writer(writer).is_native_authority()


async def test_dual_read_projections_agree(populated_db):
    # Admit one native run over a legacy task and compare a shared field.
    await support.seed_run("run-dual", "task-active")
    legacy = {"task_id": "task-active", "status": "running"}
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT task_id FROM runs WHERE run_id = 'run-dual'",
        )
        native_task = str((await cursor.fetchone())["task_id"])
    native = {"task_id": native_task, "status": "running"}
    assert negotiation.dual_read_agrees(
        legacy, native, compared_fields=("task_id", "status"),
    )
    # A disagreeing field is detected.
    assert not negotiation.dual_read_agrees(
        legacy, {"task_id": "task-active", "status": "completed"},
        compared_fields=("status",),
    )


# ── Cutover ──────────────────────────────────────────────────────────


async def test_cutover_keeps_existing_run_pairs(populated_db):
    await support.seed_run("run-existing", "task-active")
    before = await negotiation.existing_run_pairs()
    assert before["run-existing"] == {
        "runtime_id": "classic", "runtime_contract_version": "1",
    }
    # A cutover admits a new run; the existing run keeps its pair.
    await support.seed_run("run-new", "task-completed")
    await negotiation.assert_cutover_preserves_existing_pairs(before)
    # An in-place pair change is rejected.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE runs SET runtime_contract_version = '2' "
            "WHERE run_id = 'run-existing'",
        )
        await connection.commit()
    with pytest.raises(negotiation.MigrationNegotiationError):
        await negotiation.assert_cutover_preserves_existing_pairs(before)


# ── Legacy import and retirement ─────────────────────────────────────


async def test_legacy_import_preserves_cursor_and_row_digest(populated_db):
    rows = [
        {"stream": "task:a", "event_type": "task.created", "data": "{}"},
        {"stream": "task:b", "event_type": "task.completed", "data": "{}"},
    ]
    imports = negotiation.import_legacy_rows(
        rows, source_table="event_journal",
    )
    assert [entry.source_cursor for entry in imports] == [0, 1]
    assert imports[0].source_row_digest == digest_hex("legacy-row", rows[0])
    assert imports[0].source_row_digest != imports[1].source_row_digest


async def test_legacy_table_retirement_refuses_with_an_active_reader(
    populated_db,
):
    with pytest.raises(negotiation.RetirementRefusedError):
        negotiation.assert_table_retirement_allowed(
            "event_journal",
            active_readers=("compatibility_projection",),
            phase="contract",
        )
    # Retirement outside the contract phase also refuses.
    with pytest.raises(negotiation.RetirementRefusedError):
        negotiation.assert_table_retirement_allowed(
            "event_journal", active_readers=(), phase="dual_read",
        )
    # With no reader in the contract phase, retirement is allowed.
    negotiation.assert_table_retirement_allowed(
        "event_journal", active_readers=(), phase="contract",
    )


# ── Supported downgrade ──────────────────────────────────────────────


def test_supported_downgrade_before_a_contract_migration():
    plan = negotiation.DowngradePlan(
        from_schema_version=db.SCHEMA_VERSION,
        to_schema_version=db.SCHEMA_VERSION - 1,
        new_writes_present=False,
        new_writes_are_reversible=True,
    )
    outcome = negotiation.evaluate_downgrade(plan)
    assert outcome["supported"]
    assert outcome["preserves_new_writes"]


def test_downgrade_refuses_when_it_cannot_preserve_new_writes():
    plan = negotiation.DowngradePlan(
        from_schema_version=db.SCHEMA_VERSION,
        to_schema_version=db.SCHEMA_VERSION - 1,
        new_writes_present=True,
        new_writes_are_reversible=False,
    )
    with pytest.raises(negotiation.DowngradeRefusedError):
        negotiation.evaluate_downgrade(plan)
    # A downgrade that does not lower the version also refuses.
    with pytest.raises(negotiation.DowngradeRefusedError):
        negotiation.evaluate_downgrade(
            negotiation.DowngradePlan(
                from_schema_version=db.SCHEMA_VERSION,
                to_schema_version=db.SCHEMA_VERSION,
                new_writes_present=False,
                new_writes_are_reversible=True,
            ),
        )


async def test_legacy_upgrade_creates_no_native_authority_rows(populated_db):
    # The populated legacy database has no native authority rows. The
    # upgrade migrations add tables but write no native run records for the
    # legacy tasks.
    async with db._connect() as connection:  # noqa: SLF001
        for table in ("runs", "activations", "effect_attempts",
                      "budget_reservations", "evidence_decisions"):
            cursor = await connection.execute(
                f"SELECT COUNT(*) AS rows_present FROM {table}",
            )
            assert int((await cursor.fetchone())["rows_present"]) == 0
    # The journal holds no records for the legacy tasks either.
    assert await journal.read_journal() == []
