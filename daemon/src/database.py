# /opt/bmas/daemon/database.py
"""
bMAS SQLite persistence layer.

Owns all SQLite interactions for task history, debate archives,
per-task cost tracking, and log archival. Separated from blackboard.py
which remains Redis-only for real-time state.

Connection pattern: Every function opens and closes its own ephemeral
connection via _connect(). This prevents WAL checkpoint starvation from
long-lived connections (e.g., SSE streams) and isolates background tasks
from request handler lifecycles. See 02-data-layer.md §2.3 for rationale.
"""

import os
import json
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import aiosqlite

logger = logging.getLogger("bmas.database")

DB_PATH = os.getenv("BMAS_DB_PATH", "/data/bmas.db")
SCHEMA_VERSION = 2


# ── Schema DDL ───────────────────────────────────────────────────────

SCHEMA_DDL = """
-- ── Core task record ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    full_input      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','completed','failed')),
    complexity      TEXT,
    model_used      TEXT,
    error_message   TEXT,
    result_summary  TEXT,
    result_json     TEXT,
    metadata        TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at      TEXT,
    completed_at    TEXT,
    duration_ms     INTEGER,
    total_cost_usd  REAL DEFAULT 0.0,
    total_tokens    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- ── Sub-tasks (DAG nodes) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sub_tasks (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    label           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','completed','failed')),
    agent_role      TEXT NOT NULL,
    depends_on      TEXT,
    result          TEXT,
    error           TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    sort_order      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_subtasks_task ON sub_tasks(task_id);

-- ── Debate entries (preserved permanently) ───────────────────────
CREATE TABLE IF NOT EXISTS debate_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL,
    agent_role      TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_debate_task ON debate_entries(task_id);
CREATE INDEX IF NOT EXISTS idx_debate_session ON debate_entries(session_id);

-- ── Per-task cost entries ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cost_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0,
    phase           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_cost_task ON cost_entries(task_id);

-- ── Task log entries (archival copy) ─────────────────────────────
CREATE TABLE IF NOT EXISTS log_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_role      TEXT NOT NULL,
    level           TEXT DEFAULT 'info',
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_logs_task ON log_entries(task_id);

-- ── Schema versioning ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


# ── Migration v2 DDL (doc 07 — additive tables/columns) ─────────────

MIGRATION_V2_DDL = """
-- ── board_entries — durable board snapshot (doc 07 §1.1) ─────────
CREATE TABLE IF NOT EXISTS board_entries (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,
    author        TEXT NOT NULL,
    author_node   TEXT,
    title         TEXT,
    body          TEXT,
    refs          TEXT,
    confidence    REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    salience      REAL DEFAULT 0.0,
    round         INTEGER,
    space         TEXT NOT NULL DEFAULT 'public',
    created_by_turn TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_board_entries_task ON board_entries(task_id);
CREATE INDEX IF NOT EXISTS idx_board_entries_salience ON board_entries(task_id, salience DESC);

-- ── board_events — append-only event log (doc 07 §1.2) ──────────
CREATE TABLE IF NOT EXISTS board_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    round         INTEGER,
    turn_id       TEXT,
    actor         TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    entry_id      TEXT,
    payload       TEXT NOT NULL,
    redis_stream_id TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_board_events_task ON board_events(task_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_board_events_task_seq ON board_events(task_id, seq);

-- ── agent_traces — durable agent activity (doc 07 §1.3) ─────────
CREATE TABLE IF NOT EXISTS agent_traces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    turn_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL,
    node          TEXT,
    type          TEXT NOT NULL,
    data          TEXT,
    model         TEXT,
    tokens_in     INTEGER DEFAULT 0,
    tokens_out    INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0.0,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_traces_task ON agent_traces(task_id, turn_id, seq);

-- ── turns — one row per KS activation (doc 07 §1.4) ─────────────
CREATE TABLE IF NOT EXISTS turns (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    round_no      INTEGER NOT NULL,
    role          TEXT NOT NULL,
    node          TEXT,
    model         TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    entries_added INTEGER DEFAULT 0,
    started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at  TEXT,
    cost_usd      REAL DEFAULT 0.0,
    joules_estimate REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_turns_task ON turns(task_id, round_no);

-- ── task_files — uploaded inputs (doc 07 §1.5, doc 17 §3) ───────
CREATE TABLE IF NOT EXISTS task_files (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    mime          TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    extracted_chars INTEGER DEFAULT 0,
    summary_entry TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_task_files_task ON task_files(task_id);

-- ── artifacts — agent-created outputs (doc 07 §1.6, doc 17 §6) ──
CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    turn_id       TEXT,
    author        TEXT,
    rel_path      TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    mime          TEXT,
    bytes         INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_task_path_v ON artifacts(task_id, rel_path, version);
"""

# Column additions are ALTER TABLE statements that must run one at a time
MIGRATION_V2_ALTER_TASKS = [
    "ALTER TABLE tasks ADD COLUMN variant          TEXT DEFAULT 'legacy_pipeline'",
    "ALTER TABLE tasks ADD COLUMN rounds_used      INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN terminated_by    TEXT",
    "ALTER TABLE tasks ADD COLUMN answer_source    TEXT",
    "ALTER TABLE tasks ADD COLUMN phase            TEXT",
    "ALTER TABLE tasks ADD COLUMN output_dir       TEXT",
    "ALTER TABLE tasks ADD COLUMN joules_estimate  REAL DEFAULT 0.0",
]

MIGRATION_V2_ALTER_COST_ENTRIES = [
    "ALTER TABLE cost_entries ADD COLUMN node_id        TEXT",
    "ALTER TABLE cost_entries ADD COLUMN turn_id        TEXT",
    "ALTER TABLE cost_entries ADD COLUMN provider       TEXT",
    "ALTER TABLE cost_entries ADD COLUMN price_source   TEXT",
    "ALTER TABLE cost_entries ADD COLUMN joules_estimate REAL DEFAULT 0.0",
]


# ── Connection Infrastructure ────────────────────────────────────────

@asynccontextmanager
async def _connect():
    """Ephemeral async SQLite connection with WAL mode and foreign keys.

    Every CRUD function must use this context manager to open and close
    its own connection. Do NOT use a shared/singleton connection.
    See module docstring for rationale.
    """
    db = await aiosqlite.connect(DB_PATH, timeout=15.0)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def check_sqlite_health() -> bool:
    """Quick health probe for the /health endpoint.

    Opens an ephemeral connection, runs SELECT 1, closes immediately.
    Uses a shorter timeout (5s) since this runs on every health check.
    """
    try:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            await db.execute("SELECT 1")
            return True
    except Exception:
        return False


# ── Schema Management ────────────────────────────────────────────────

async def _migrate_to_v2(db: aiosqlite.Connection) -> None:
    """Migration v1 → v2: additive tables/columns (doc 07).

    Creates 6 new tables (board_entries, board_events, agent_traces,
    turns, task_files, artifacts) and adds columns to tasks and
    cost_entries. All additive — no destructive changes.
    """
    # New tables + indexes
    await db.executescript(MIGRATION_V2_DDL)
    # executescript commits; restore row_factory
    db.row_factory = aiosqlite.Row

    # ALTER TABLE statements — one at a time, each idempotent via
    # column-existence check (SQLite has no IF NOT EXISTS for ADD COLUMN)
    for stmt in MIGRATION_V2_ALTER_TASKS:
        col_name = stmt.split("ADD COLUMN")[1].strip().split()[0]
        cursor = await db.execute("PRAGMA table_info(tasks)")
        existing = [row[1] for row in await cursor.fetchall()]
        if col_name not in existing:
            await db.execute(stmt)

    for stmt in MIGRATION_V2_ALTER_COST_ENTRIES:
        col_name = stmt.split("ADD COLUMN")[1].strip().split()[0]
        cursor = await db.execute("PRAGMA table_info(cost_entries)")
        existing = [row[1] for row in await cursor.fetchall()]
        if col_name not in existing:
            await db.execute(stmt)

    await db.commit()
    logger.info("Migration v2 applied: 6 new tables, 12 new columns")


async def _migrate(db: aiosqlite.Connection, version: int) -> None:
    """Dispatch to the migration function for the given version."""
    migrations = {
        2: _migrate_to_v2,
    }
    fn = migrations.get(version)
    if fn is None:
        raise RuntimeError(f"No migration defined for version {version}")
    await fn(db)
    await db.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (version,),
    )
    await db.commit()


async def init_db() -> None:
    """Initialize SQLite database: validate infrastructure, create schema,
    check migrations, and recover orphaned tasks.

    Raises RuntimeError on failure — this intentionally crashes the daemon
    at startup with a clear diagnostic.
    """
    db_dir = os.path.dirname(DB_PATH)

    # Validate volume mount exists and is writable
    if not os.path.isdir(db_dir):
        raise RuntimeError(
            f"Database directory does not exist: {db_dir}. "
            f"Is the daemon-data volume mounted at /data?"
        )
    if not os.access(db_dir, os.W_OK):
        raise RuntimeError(
            f"Database directory is not writable: {db_dir}. "
            f"Check volume mount permissions."
        )

    try:
        async with _connect() as db:
            # Run schema DDL (IF NOT EXISTS makes this idempotent)
            await db.executescript(SCHEMA_DDL)

            # executescript commits and may reset connection state,
            # so re-set row_factory for subsequent queries
            db.row_factory = aiosqlite.Row

            # Ensure schema_version row exists
            cursor = await db.execute(
                "SELECT MAX(version) as v FROM schema_version"
            )
            row = await cursor.fetchone()
            current_version = row["v"] if row and row["v"] is not None else 0

            if current_version < SCHEMA_VERSION:
                # Fresh installs: SCHEMA_DDL already establishes v1.
                # Record that, then run only the v2+ migrations.
                if current_version == 0:
                    await db.execute(
                        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                        (1,),
                    )
                    await db.commit()
                    current_version = 1
                    logger.info("Schema v1 initialized from DDL")

                # Run migrations sequentially from current to target
                for v in range(current_version + 1, SCHEMA_VERSION + 1):
                    await _migrate(db, v)
                    logger.info(f"Applied migration to version {v}")

                logger.info(f"Schema initialized at version {SCHEMA_VERSION}")
            else:
                logger.info(f"Schema version {current_version} — up to date")

            # Zombie task recovery: mark orphaned tasks as failed
            orphaned = await db.execute(
                "UPDATE tasks SET status = 'failed', "
                "error_message = 'Daemon restarted unexpectedly', "
                "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE status IN ('pending', 'running')"
            )
            await db.commit()
            if orphaned.rowcount > 0:
                logger.warning(
                    f"Recovered {orphaned.rowcount} orphaned task(s) from unclean shutdown"
                )

        db_size = os.path.getsize(DB_PATH)
        logger.info(f"SQLite ready: {DB_PATH} ({db_size} bytes)")

    except Exception as e:
        raise RuntimeError(
            f"SQLite initialization failed: {e}. "
            f"Check that /data is a valid, writable volume mount."
        ) from e


# ── Task CRUD ────────────────────────────────────────────────────────

async def create_task(task_id: str, label: str, full_input: str) -> None:
    """Create a new task record with status='pending'."""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO tasks (id, label, full_input, status) VALUES (?, ?, ?, 'pending')",
            (task_id, label, full_input),
        )
        await db.commit()


async def update_task_status(
    task_id: str,
    status: str | None = None,
    complexity: str | None = None,
    model_used: str | None = None,
) -> None:
    """Update task fields. Only non-None arguments are written."""
    updates: list[str] = []
    params: list = []

    if status is not None:
        updates.append("status = ?")
        params.append(status)
        if status == "running":
            updates.append("started_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    if complexity is not None:
        updates.append("complexity = ?")
        params.append(complexity)
    if model_used is not None:
        updates.append("model_used = ?")
        params.append(model_used)

    if not updates:
        return

    params.append(task_id)
    async with _connect() as db:
        await db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await db.commit()


async def complete_task(
    task_id: str, result_summary: str, result_json: str
) -> None:
    """Mark a task as completed with its result."""
    async with _connect() as db:
        # Fetch started_at to compute duration
        cursor = await db.execute(
            "SELECT started_at FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        duration_ms = None
        if row and row["started_at"]:
            try:
                started = datetime.fromisoformat(row["started_at"])
                now = datetime.now(timezone.utc)
                duration_ms = int((now - started).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        await db.execute(
            "UPDATE tasks SET "
            "status = 'completed', "
            "result_summary = ?, "
            "result_json = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "duration_ms = ? "
            "WHERE id = ?",
            (result_summary, result_json, duration_ms, task_id),
        )
        await db.commit()


async def fail_task(task_id: str, error_message: str) -> None:
    """Mark a task as failed with an error message."""
    async with _connect() as db:
        await db.execute(
            "UPDATE tasks SET "
            "status = 'failed', "
            "error_message = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (error_message, task_id),
        )
        await db.commit()


async def get_task(task_id: str) -> dict | None:
    """Fetch a single task by ID. Returns None if not found."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_tasks(
    limit: int = 50, offset: int = 0, status: str | None = None
) -> list[dict]:
    """List tasks newest-first with pagination and optional status filter."""
    async with _connect() as conn:
        if status:
            rows = await conn.execute_fetchall(
                "SELECT * FROM tasks WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            rows = await conn.execute_fetchall(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [dict(r) for r in rows]


async def count_tasks(status: str | None = None) -> int:
    """Count total tasks, optionally filtered by status."""
    async with _connect() as conn:
        if status:
            cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE status = ?",
                (status,),
            )
        else:
            cursor = await conn.execute("SELECT COUNT(*) as cnt FROM tasks")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0


# ── Sub-task CRUD ────────────────────────────────────────────────────

async def upsert_sub_tasks(task_id: str, sub_tasks: list[dict]) -> None:
    """Insert or replace sub-task records for a task.

    Accepts the dict shape from the orchestrator:
    {id, label, status, agent_role, depends_on: list}
    """
    async with _connect() as db:
        for i, st in enumerate(sub_tasks):
            depends_on = st.get("depends_on")
            if isinstance(depends_on, list):
                depends_on = json.dumps(depends_on)

            await db.execute(
                "INSERT OR REPLACE INTO sub_tasks "
                "(id, task_id, label, status, agent_role, depends_on, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    st["id"],
                    task_id,
                    st.get("label", ""),
                    st.get("status", "pending"),
                    st.get("agent_role", "unknown"),
                    depends_on,
                    i,
                ),
            )
        await db.commit()


async def get_sub_tasks(task_id: str) -> list[dict]:
    """Fetch all sub-tasks for a task, ordered by sort_order."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM sub_tasks WHERE task_id = ? ORDER BY sort_order",
            (task_id,),
        )
        result = []
        for r in rows:
            d = dict(r)
            # Parse depends_on back to list
            if d.get("depends_on"):
                try:
                    d["depends_on"] = json.loads(d["depends_on"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result


# ── Debate CRUD ──────────────────────────────────────────────────────

async def insert_debate_entry(
    task_id: str, session_id: str, agent_role: str, content: str
) -> None:
    """Insert a debate entry (permanent archive — Redis copy is ephemeral)."""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO debate_entries (task_id, session_id, agent_role, content) "
            "VALUES (?, ?, ?, ?)",
            (task_id, session_id, agent_role, content),
        )
        await db.commit()


async def get_debate(task_id: str) -> list[dict]:
    """Fetch all debate entries for a task, ordered chronologically."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM debate_entries WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        return [dict(r) for r in rows]


# ── Cost CRUD ────────────────────────────────────────────────────────

async def insert_cost_entry(
    task_id: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    phase: str | None = None,
) -> None:
    """Insert a per-call cost entry for a task."""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO cost_entries "
            "(task_id, model, input_tokens, output_tokens, cost_usd, phase) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, model, input_tokens, output_tokens, cost_usd, phase),
        )
        await db.commit()


async def get_task_cost(task_id: str) -> list[dict]:
    """Fetch all cost entries for a task."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM cost_entries WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        return [dict(r) for r in rows]


async def update_task_cost_totals(task_id: str) -> None:
    """Roll up cost_entries into the tasks row (total_cost_usd, total_tokens)."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT "
            "  COALESCE(SUM(cost_usd), 0.0) as total_cost, "
            "  COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens "
            "FROM cost_entries WHERE task_id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE tasks SET total_cost_usd = ?, total_tokens = ? WHERE id = ?",
                (row["total_cost"], row["total_tokens"], task_id),
            )
            await db.commit()


async def get_task_cost_summary(task_id: str) -> dict:
    """Aggregated cost breakdown by model and by phase.

    Returns: {
        total_cost_usd, total_tokens,
        by_model: [{model, input_tokens, output_tokens, cost_usd}],
        by_phase: [{phase, cost_usd, tokens}]
    }
    """
    async with _connect() as conn:
        # By model
        model_rows = await conn.execute_fetchall(
            "SELECT model, "
            "  SUM(input_tokens) as input_tokens, "
            "  SUM(output_tokens) as output_tokens, "
            "  SUM(cost_usd) as cost_usd "
            "FROM cost_entries WHERE task_id = ? GROUP BY model",
            (task_id,),
        )
        # By phase
        phase_rows = await conn.execute_fetchall(
            "SELECT phase, "
            "  SUM(cost_usd) as cost_usd, "
            "  SUM(input_tokens + output_tokens) as tokens "
            "FROM cost_entries WHERE task_id = ? GROUP BY phase",
            (task_id,),
        )
        # Totals
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) as total_cost, "
            "  COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens "
            "FROM cost_entries WHERE task_id = ?",
            (task_id,),
        )
        totals = await cursor.fetchone()

        return {
            "total_cost_usd": totals["total_cost"] if totals else 0.0,
            "total_tokens": totals["total_tokens"] if totals else 0,
            "by_model": [dict(r) for r in model_rows],
            "by_phase": [dict(r) for r in phase_rows],
        }


# ── Log CRUD ─────────────────────────────────────────────────────────

async def insert_log_entry(
    task_id: str, agent_role: str, level: str, message: str
) -> None:
    """Insert a log entry (permanent archive — Redis streams are ephemeral)."""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO log_entries (task_id, agent_role, level, message) "
            "VALUES (?, ?, ?, ?)",
            (task_id, agent_role, level, message),
        )
        await db.commit()


async def get_task_logs(
    task_id: str, limit: int = 200, offset: int = 0
) -> list[dict]:
    """Fetch log entries for a task with pagination."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM log_entries WHERE task_id = ? "
            "ORDER BY id LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        )
        return [dict(r) for r in rows]


async def count_task_logs(task_id: str) -> int:
    """Count total log entries for a task."""
    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) as cnt FROM log_entries WHERE task_id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0


# ── Agent Traces CRUD (Phase 1, doc 07 §3) ───────────────────────────

async def insert_agent_traces(rows: list[dict]) -> None:
    """Batch-insert agent trace events into agent_traces table.

    Each row must contain: task_id, turn_id, seq, role, type, data.
    Optional: node, model, tokens_in, tokens_out, cost_usd.
    """
    if not rows:
        return
    async with _connect() as db:
        for row in rows:
            data_json = row.get("data")
            if isinstance(data_json, dict):
                data_json = json.dumps(data_json)
            await db.execute(
                "INSERT INTO agent_traces "
                "(task_id, turn_id, seq, role, node, type, data, model, "
                "tokens_in, tokens_out, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["task_id"],
                    row["turn_id"],
                    row["seq"],
                    row["role"],
                    row.get("node"),
                    row["type"],
                    data_json,
                    row.get("model"),
                    row.get("tokens_in", 0),
                    row.get("tokens_out", 0),
                    row.get("cost_usd", 0.0),
                ),
            )
        await db.commit()


async def get_turn_traces(task_id: str, turn_id: str) -> list[dict]:
    """Fetch all trace events for a specific turn, ordered by seq."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM agent_traces WHERE task_id = ? AND turn_id = ? "
            "ORDER BY seq",
            (task_id, turn_id),
        )
        result = []
        for r in rows:
            d = dict(r)
            if d.get("data"):
                try:
                    d["data"] = json.loads(d["data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result


async def get_task_traces(
    task_id: str, limit: int = 200, offset: int = 0
) -> list[dict]:
    """Fetch trace events for a task (paginated), ordered by turn_id + seq."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM agent_traces WHERE task_id = ? "
            "ORDER BY turn_id, seq LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        )
        result = []
        for r in rows:
            d = dict(r)
            if d.get("data"):
                try:
                    d["data"] = json.loads(d["data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result


# ── Turns CRUD (Phase 1, doc 07 §3) ──────────────────────────────────

async def create_turn(turn: dict) -> None:
    """Create a new turn record (one row per KS activation)."""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO turns "
            "(id, task_id, round_no, role, node, model, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                turn["id"],
                turn["task_id"],
                turn.get("round_no", 1),
                turn["role"],
                turn.get("node"),
                turn.get("model"),
                turn.get("status", "running"),
            ),
        )
        await db.commit()


async def complete_turn(
    turn_id: str,
    status: str,
    entries_added: int,
    cost_usd: float,
    joules_estimate: float = 0.0,
) -> None:
    """Mark a turn as completed/failed/declined with cost info."""
    async with _connect() as db:
        await db.execute(
            "UPDATE turns SET "
            "status = ?, entries_added = ?, cost_usd = ?, "
            "joules_estimate = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (status, entries_added, cost_usd, joules_estimate, turn_id),
        )
        await db.commit()


async def get_turns(task_id: str) -> list[dict]:
    """Fetch all turns for a task, ordered by round_no."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM turns WHERE task_id = ? ORDER BY round_no, started_at",
            (task_id,),
        )
        return [dict(r) for r in rows]


# ── Extended Cost Entry (Phase 1, doc 07 §1.8) ───────────────────────

async def insert_cost_entry_v2(
    task_id: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    phase: str | None = None,
    node_id: str | None = None,
    turn_id: str | None = None,
    provider: str | None = None,
    price_source: str | None = None,
    joules_estimate: float = 0.0,
) -> None:
    """Insert a per-call cost entry with Phase 1 extended columns.

    Uses the v2 columns added by Phase 0 migration (node_id, turn_id,
    provider, price_source, joules_estimate).
    """
    async with _connect() as db:
        await db.execute(
            "INSERT INTO cost_entries "
            "(task_id, model, input_tokens, output_tokens, cost_usd, phase, "
            "node_id, turn_id, provider, price_source, joules_estimate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, model, input_tokens, output_tokens, cost_usd, phase,
                node_id, turn_id, provider, price_source, joules_estimate,
            ),
        )
        await db.commit()


# ── Board CRUD (Phase 2, doc 07 §3) ─────────────────────────────────

async def upsert_board_entry(entry: dict) -> None:
    """Insert or update a board entry in the durable snapshot.

    Uses INSERT OR REPLACE so that re-folding from events produces
    the same result as incremental updates.
    """
    refs = entry.get("refs", [])
    if isinstance(refs, list):
        refs = json.dumps(refs)
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO board_entries "
            "(id, task_id, type, author, author_node, title, body, refs, "
            "confidence, status, salience, round, space, created_by_turn, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["id"],
                entry["task_id"],
                entry["type"],
                entry["author"],
                entry.get("author_node"),
                entry.get("title"),
                entry.get("body"),
                refs,
                entry.get("confidence", 0.5),
                entry.get("status", "open"),
                entry.get("salience", 0.0),
                entry.get("round", 0),
                entry.get("space", "public"),
                entry.get("created_by_turn"),
                entry.get("created_at", ""),
                entry.get("updated_at", ""),
            ),
        )
        await db.commit()


async def get_board_entries(task_id: str) -> list[dict]:
    """Fetch all board entries for a task, ordered by id."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM board_entries WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        result = []
        for r in rows:
            d = dict(r)
            if d.get("refs"):
                try:
                    d["refs"] = json.loads(d["refs"])
                except (json.JSONDecodeError, TypeError):
                    d["refs"] = []
            else:
                d["refs"] = []
            result.append(d)
        return result


async def insert_board_event(
    task_id: str,
    seq: int,
    round_no: int | None,
    turn_id: str | None,
    actor: str,
    event_type: str,
    entry_id: str | None,
    payload: dict | str,
    redis_stream_id: str | None = None,
) -> None:
    """Insert a board event into the durable event log.

    This is the SQLite-first write (doc 04 §5.1 durability contract).
    The caller must handle Redis separately.
    """
    payload_str = payload if isinstance(payload, str) else json.dumps(payload)
    async with _connect() as db:
        await db.execute(
            "INSERT INTO board_events "
            "(task_id, seq, round, turn_id, actor, event_type, "
            "entry_id, payload, redis_stream_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, seq, round_no, turn_id, actor,
                event_type, entry_id, payload_str, redis_stream_id,
            ),
        )
        await db.commit()


async def get_board_events(
    task_id: str, until_seq: int | None = None
) -> list[dict]:
    """Fetch board events for a task, ordered by seq (replay).

    If until_seq is provided, returns events up to and including that seq.
    """
    async with _connect() as db:
        if until_seq is not None:
            rows = await db.execute_fetchall(
                "SELECT * FROM board_events "
                "WHERE task_id = ? AND seq <= ? ORDER BY seq",
                (task_id, until_seq),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM board_events "
                "WHERE task_id = ? ORDER BY seq",
                (task_id,),
            )
        result = []
        for r in rows:
            d = dict(r)
            if d.get("payload"):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result


async def update_board_entry_status(
    task_id: str, entry_id: str, status: str
) -> None:
    """Update the status of a board entry."""
    async with _connect() as db:
        await db.execute(
            "UPDATE board_entries SET status = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND task_id = ?",
            (status, entry_id, task_id),
        )
        await db.commit()


async def update_board_entry_salience(
    task_id: str, entry_id: str, salience: float
) -> None:
    """Update the salience score of a board entry."""
    async with _connect() as db:
        await db.execute(
            "UPDATE board_entries SET salience = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND task_id = ?",
            (salience, entry_id, task_id),
        )
        await db.commit()


# ── Task Files CRUD (doc 17 §3) ─────────────────────────────────────

async def insert_task_file(
    file_id: str,
    task_id: str,
    name: str,
    mime: str,
    size_bytes: int,
    sha256: str,
    stored_path: str,
    extracted_chars: int = 0,
) -> None:
    """Insert a task_files row after successful upload."""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO task_files (id, task_id, name, mime, bytes, sha256, stored_path, extracted_chars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, task_id, name, mime, size_bytes, sha256, stored_path, extracted_chars),
        )
        await db.commit()


async def get_task_files(task_id: str) -> list[dict]:
    """Return all files for a task, ordered by created_at."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, task_id, name, mime, bytes, sha256, stored_path, extracted_chars, created_at "
            "FROM task_files WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_task_file(file_id: str) -> dict | None:
    """Return a single file row by ID."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM task_files WHERE id = ?", (file_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Artifacts CRUD (doc 17 §6) ──────────────────────────────────────

async def insert_artifact(
    artifact_id: str,
    task_id: str,
    turn_id: str | None,
    author: str | None,
    rel_path: str,
    stored_path: str,
    mime: str | None,
    size_bytes: int,
    sha256: str,
    version: int = 1,
) -> None:
    """Insert an artifact row after successful ingest."""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO artifacts (id, task_id, turn_id, author, rel_path, stored_path, mime, bytes, sha256, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, task_id, turn_id, author, rel_path, stored_path, mime, size_bytes, sha256, version),
        )
        await db.commit()


async def get_artifacts(task_id: str) -> list[dict]:
    """Return all artifacts for a task, ordered by created_at."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, task_id, turn_id, author, rel_path, stored_path, mime, bytes, sha256, version, created_at "
            "FROM artifacts WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_artifact(artifact_id: str) -> dict | None:
    """Return a single artifact row by ID."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_artifact_max_version(task_id: str, rel_path: str) -> int:
    """Return the current highest version number for a given (task_id, rel_path)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT MAX(version) FROM artifacts WHERE task_id = ? AND rel_path = ?",
            (task_id, rel_path),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else 0


async def get_task_artifacts_total_bytes(task_id: str) -> int:
    """Return total bytes of all artifacts for a task (quota enforcement)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM artifacts WHERE task_id = ?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
