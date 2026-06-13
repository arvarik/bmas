"""Tests for the SQLite v2 migration (doc 07).

Verifies that the migration creates all 6 new tables, adds all new
columns to tasks and cost_entries, and that the schema is idempotent.
"""
import os
import sys

import aiosqlite
import pytest
import pytest_asyncio

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import (
    SCHEMA_DDL,
    SCHEMA_VERSION,
    _migrate,
    _migrate_to_v2,
    init_db,
)

# ── Fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    """Create a fresh v1 database and return its path."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("database.DB_PATH", db_path)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA_DDL)
        db.row_factory = aiosqlite.Row
        # Mark as v1
        await db.execute(
            "INSERT INTO schema_version (version) VALUES (1)"
        )
        await db.commit()

    return db_path


@pytest_asyncio.fixture
async def v2_db(fresh_db):
    """Create a database migrated to v2."""
    async with aiosqlite.connect(fresh_db) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row
        await _migrate(db, 2)
    return fresh_db


# ── Helpers ──────────────────────────────────────────────────────────

async def _get_tables(db_path: str) -> set[str]:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def _get_columns(db_path: str, table: str) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return [row[1] for row in rows]


async def _get_indexes(db_path: str) -> set[str]:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def _get_schema_version(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT MAX(version) as v FROM schema_version"
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0


# ── Tests: Schema Version ───────────────────────────────────────────

class TestSchemaVersion:

    def test_schema_version_is_2(self):
        assert SCHEMA_VERSION == 2

    @pytest.mark.asyncio
    async def test_fresh_db_is_v1(self, fresh_db):
        v = await _get_schema_version(fresh_db)
        assert v == 1

    @pytest.mark.asyncio
    async def test_migrated_db_is_v2(self, v2_db):
        v = await _get_schema_version(v2_db)
        assert v == 2


# ── Tests: New Tables ────────────────────────────────────────────────

class TestNewTables:

    V2_TABLES = {
        "board_entries",
        "board_events",
        "agent_traces",
        "turns",
        "task_files",
        "artifacts",
    }

    @pytest.mark.asyncio
    async def test_v2_tables_created(self, v2_db):
        tables = await _get_tables(v2_db)
        for t in self.V2_TABLES:
            assert t in tables, f"Missing table: {t}"

    @pytest.mark.asyncio
    async def test_v1_tables_preserved(self, v2_db):
        tables = await _get_tables(v2_db)
        for t in ("tasks", "sub_tasks", "debate_entries", "cost_entries",
                   "log_entries", "schema_version"):
            assert t in tables, f"Missing legacy table: {t}"


# ── Tests: board_entries columns ─────────────────────────────────────

class TestBoardEntries:

    EXPECTED_COLUMNS = [
        "id", "task_id", "type", "author", "author_node", "title",
        "body", "refs", "confidence", "status", "salience", "round",
        "space", "created_by_turn", "created_at", "updated_at",
    ]

    @pytest.mark.asyncio
    async def test_columns(self, v2_db):
        cols = await _get_columns(v2_db, "board_entries")
        for c in self.EXPECTED_COLUMNS:
            assert c in cols, f"Missing column: board_entries.{c}"


# ── Tests: board_events columns ──────────────────────────────────────

class TestBoardEvents:

    EXPECTED_COLUMNS = [
        "id", "task_id", "seq", "round", "turn_id", "actor",
        "event_type", "entry_id", "payload", "redis_stream_id",
        "created_at",
    ]

    @pytest.mark.asyncio
    async def test_columns(self, v2_db):
        cols = await _get_columns(v2_db, "board_events")
        for c in self.EXPECTED_COLUMNS:
            assert c in cols, f"Missing column: board_events.{c}"


# ── Tests: tasks column additions ────────────────────────────────────

class TestTasksColumns:

    NEW_COLUMNS = [
        "variant", "rounds_used", "terminated_by", "answer_source",
        "phase", "output_dir", "joules_estimate",
    ]

    @pytest.mark.asyncio
    async def test_new_columns_exist(self, v2_db):
        cols = await _get_columns(v2_db, "tasks")
        for c in self.NEW_COLUMNS:
            assert c in cols, f"Missing column: tasks.{c}"

    @pytest.mark.asyncio
    async def test_variant_default(self, v2_db):
        """New tasks should get variant='traditional' by default."""
        async with aiosqlite.connect(v2_db) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                "INSERT INTO tasks (id, label, full_input) VALUES ('t1', 'test', 'test input')"
            )
            await db.commit()
            cursor = await db.execute("SELECT variant FROM tasks WHERE id='t1'")
            row = await cursor.fetchone()
            assert row[0] == "traditional"


# ── Tests: cost_entries column additions ─────────────────────────────

class TestCostEntriesColumns:

    NEW_COLUMNS = ["node_id", "turn_id", "provider", "price_source", "joules_estimate"]

    @pytest.mark.asyncio
    async def test_new_columns_exist(self, v2_db):
        cols = await _get_columns(v2_db, "cost_entries")
        for c in self.NEW_COLUMNS:
            assert c in cols, f"Missing column: cost_entries.{c}"


# ── Tests: Indexes ───────────────────────────────────────────────────

class TestIndexes:

    EXPECTED_INDEXES = [
        "idx_board_entries_task",
        "idx_board_entries_salience",
        "idx_board_events_task",
        "uq_board_events_task_seq",
        "idx_agent_traces_task",
        "idx_turns_task",
        "idx_task_files_task",
        "idx_artifacts_task",
        "uq_artifacts_task_path_v",
    ]

    @pytest.mark.asyncio
    async def test_indexes_exist(self, v2_db):
        indexes = await _get_indexes(v2_db)
        for idx in self.EXPECTED_INDEXES:
            assert idx in indexes, f"Missing index: {idx}"


# ── Tests: Idempotency ──────────────────────────────────────────────

class TestIdempotency:

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, v2_db):
        """Running migration twice should not error."""
        async with aiosqlite.connect(v2_db) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            # Second run — should be idempotent
            await _migrate_to_v2(db)

        v = await _get_schema_version(v2_db)
        assert v == 2


# ── Tests: FK Cascade ────────────────────────────────────────────────

class TestForeignKeyCascade:

    @pytest.mark.asyncio
    async def test_task_deletion_cascades(self, v2_db):
        """Deleting a task cascades to all v2 tables."""
        async with aiosqlite.connect(v2_db) as db:
            await db.execute("PRAGMA foreign_keys=ON")

            # Insert a task
            await db.execute(
                "INSERT INTO tasks (id, label, full_input) VALUES ('t-del', 'test', 'input')"
            )
            # Insert into each v2 table
            await db.execute(
                "INSERT INTO board_entries (id, task_id, type, author, status) "
                "VALUES ('e1', 't-del', 'finding', 'critic', 'open')"
            )
            await db.execute(
                "INSERT INTO board_events (task_id, seq, actor, event_type, payload) "
                "VALUES ('t-del', 1, 'critic', 'entry_added', '{}')"
            )
            await db.execute(
                "INSERT INTO agent_traces (task_id, turn_id, seq, role, type) "
                "VALUES ('t-del', 'turn-1', 1, 'critic', 'turn_start')"
            )
            await db.execute(
                "INSERT INTO turns (id, task_id, round_no, role) "
                "VALUES ('turn-1', 't-del', 1, 'critic')"
            )
            await db.execute(
                "INSERT INTO task_files (id, task_id, name, mime, bytes, sha256, stored_path) "
                "VALUES ('f1', 't-del', 'test.pdf', 'application/pdf', 1024, 'abc123', '/data/uploads/t-del/test.pdf')"
            )
            await db.execute(
                "INSERT INTO artifacts (id, task_id, rel_path, stored_path, bytes, sha256) "
                "VALUES ('a1', 't-del', 'src/main.py', '/data/output/test/src/main.py', 512, 'def456')"
            )
            await db.commit()

            # Delete the task
            await db.execute("DELETE FROM tasks WHERE id='t-del'")
            await db.commit()

            # Verify all children are gone
            for table in ("board_entries", "board_events", "agent_traces",
                          "turns", "task_files", "artifacts"):
                cursor = await db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE task_id='t-del'"
                )
                row = await cursor.fetchone()
                assert row[0] == 0, f"Orphan rows in {table} after task deletion"


# ── Tests: init_db (full path) ───────────────────────────────────────

class TestInitDb:

    @pytest.mark.asyncio
    async def test_init_db_fresh(self, tmp_path, monkeypatch):
        """init_db on a fresh directory creates v2 schema."""
        db_path = str(tmp_path / "fresh.db")
        monkeypatch.setattr("database.DB_PATH", db_path)
        await init_db()

        v = await _get_schema_version(db_path)
        assert v == 2
        tables = await _get_tables(db_path)
        assert "board_entries" in tables

    @pytest.mark.asyncio
    async def test_init_db_upgrade_v1_to_v2(self, fresh_db, monkeypatch):
        """init_db upgrades a v1 database to v2."""
        monkeypatch.setattr("database.DB_PATH", fresh_db)
        await init_db()

        v = await _get_schema_version(fresh_db)
        assert v == 2
        tables = await _get_tables(fresh_db)
        assert "board_entries" in tables

    @pytest.mark.asyncio
    async def test_init_db_idempotent(self, v2_db, monkeypatch):
        """init_db on an already-v2 database is a no-op."""
        monkeypatch.setattr("database.DB_PATH", v2_db)
        await init_db()

        v = await _get_schema_version(v2_db)
        assert v == 2
