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

import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

import aiosqlite

logger = logging.getLogger("bmas.database")

DB_PATH = os.getenv("BMAS_DB_PATH", "/data/bmas.db")
SCHEMA_VERSION = 7


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read one bounded integer without importing the shared config module."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


MAX_EVENT_PAYLOAD_BYTES = _bounded_env_int(
    "BMAS_EVENT_PAYLOAD_MAX_BYTES", 1_048_576, 1_024, 16_777_216
)
MAX_OUTBOX_BACKLOG = _bounded_env_int(
    "BMAS_EVENT_OUTBOX_MAX", 10_000, 100, 1_000_000
)
OUTBOX_OVERLOAD_THRESHOLD = _bounded_env_int(
    "BMAS_EVENT_OUTBOX_OVERLOAD", 5_000, 10, MAX_OUTBOX_BACKLOG
)
TURN_TERMINAL_STATUSES = {"completed", "declined", "failed", "timeout"}


class LeaseFenceError(RuntimeError):
    """A SQLite write used a task lease token that no longer owns the task."""


class EventPayloadTooLarge(ValueError):
    """An event payload exceeds the durable journal limit."""


class EventIdempotencyConflict(RuntimeError):
    """An event idempotency key identifies different event content."""


async def _assert_task_lease(
    connection: aiosqlite.Connection,
    task_id: str,
    lease_token: str | None,
) -> None:
    """Validate a task lease inside the caller's SQLite transaction."""
    if lease_token is None:
        return
    cursor = await connection.execute(
        "SELECT 1 FROM tasks WHERE id = ? AND lease_token = ? ",
        (task_id, lease_token),
    )
    if await cursor.fetchone() is None:
        raise LeaseFenceError(f"Task lease no longer owns SQLite writes: {task_id}")


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
    total_tokens    INTEGER DEFAULT 0,
    archived_at     TEXT
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
    fields          TEXT,
    node            TEXT,
    turn_id         TEXT,
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
    "ALTER TABLE tasks ADD COLUMN variant          TEXT DEFAULT 'classic'",
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


# Migration v3 makes the classic blackboard resumable. The event log stays
# authoritative. The metadata row stores the current control state required
# to continue after a daemon restart.
MIGRATION_V3_BOARD_META_DDL = """
CREATE TABLE IF NOT EXISTS board_meta (
    task_id       TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    data          TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_traces_turn_seq
ON agent_traces(task_id, turn_id, seq);
"""

MIGRATION_V4_COST_IDEMPOTENCY_DDL = """
DELETE FROM cost_entries
WHERE turn_id IS NOT NULL
  AND id NOT IN (
    SELECT MIN(id)
    FROM cost_entries
    WHERE turn_id IS NOT NULL
    GROUP BY task_id, turn_id, COALESCE(phase, '')
  );

CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_turn_phase_unique
ON cost_entries(task_id, turn_id, COALESCE(phase, ''))
WHERE turn_id IS NOT NULL;
"""

MIGRATION_V5_TASK_LEASE_DDL = """
CREATE INDEX IF NOT EXISTS idx_tasks_lease_token ON tasks(lease_token);
"""


MIGRATION_V6_EVENT_DELIVERY_DDL = """
CREATE TABLE IF NOT EXISTS event_journal (
    cursor          INTEGER PRIMARY KEY AUTOINCREMENT,
    stream          TEXT NOT NULL,
    task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    data            TEXT NOT NULL,
    idempotency_key TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    published_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_journal_stream_cursor
ON event_journal(stream, cursor);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_journal_idempotency
ON event_journal(stream, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS event_outbox (
    event_cursor    INTEGER PRIMARY KEY
                    REFERENCES event_journal(cursor) ON DELETE CASCADE,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    queued_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_attempt_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_outbox_queued
ON event_outbox(queued_at, event_cursor);
"""


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
    # Performance tuning — safe with WAL mode (SQLite docs §3.3):
    # synchronous=NORMAL: skip fsync on every commit, only on checkpoint
    await db.execute("PRAGMA synchronous=NORMAL")
    # 32 MB page cache (default is 2 MB) — reduces disk reads on hot tables
    await db.execute("PRAGMA cache_size=-32000")
    # 128 MB memory-mapped I/O — eliminates pread() syscalls on reads
    await db.execute("PRAGMA mmap_size=134217728")
    # Keep temp tables (GROUP BY, ORDER BY spills) in RAM
    await db.execute("PRAGMA temp_store=MEMORY")
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


async def _migrate_to_v3(db: aiosqlite.Connection) -> None:
    """Migration v2 to v3: durable classic-board control state.

    The old board snapshot used a global entry identifier as its primary key.
    Every task starts at ``e-1``, so that key could overwrite another task.
    Version 3 changes the primary key to ``(task_id, id)``.
    """
    cursor = await db.execute("PRAGMA table_info(board_entries)")
    board_columns = await cursor.fetchall()
    composite_pk = {
        row[1] for row in board_columns if int(row[5] or 0) > 0
    } == {"task_id", "id"}

    if not composite_pk:
        await db.executescript(
            """
            CREATE TABLE board_entries_v3 (
                id              TEXT NOT NULL,
                task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                type            TEXT NOT NULL,
                author          TEXT NOT NULL,
                author_node     TEXT,
                title           TEXT,
                body            TEXT,
                refs            TEXT,
                confidence      REAL,
                status          TEXT NOT NULL DEFAULT 'open',
                salience        REAL DEFAULT 0.0,
                round           INTEGER,
                space           TEXT NOT NULL DEFAULT 'public',
                created_by_turn TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (task_id, id)
            );
            INSERT OR REPLACE INTO board_entries_v3
            SELECT id, task_id, type, author, author_node, title, body, refs,
                   confidence, status, salience, round, space, created_by_turn,
                   created_at, updated_at
            FROM board_entries;
            DROP TABLE board_entries;
            ALTER TABLE board_entries_v3 RENAME TO board_entries;
            CREATE INDEX idx_board_entries_task ON board_entries(task_id);
            CREATE INDEX idx_board_entries_salience
            ON board_entries(task_id, salience DESC);
            """
        )
        db.row_factory = aiosqlite.Row

    # Remove duplicate trace deliveries before the unique index is created.
    await db.execute(
        "DELETE FROM agent_traces WHERE id NOT IN ("
        "SELECT MIN(id) FROM agent_traces GROUP BY task_id, turn_id, seq)"
    )
    await db.executescript(MIGRATION_V3_BOARD_META_DDL)
    db.row_factory = aiosqlite.Row

    cursor = await db.execute("PRAGMA table_info(tasks)")
    task_columns = {row[1] for row in await cursor.fetchall()}
    for column, ddl in (
        ("run_state", "ALTER TABLE tasks ADD COLUMN run_state TEXT DEFAULT 'queued'"),
        ("resume_count", "ALTER TABLE tasks ADD COLUMN resume_count INTEGER DEFAULT 0"),
        ("last_heartbeat_at", "ALTER TABLE tasks ADD COLUMN last_heartbeat_at TEXT"),
    ):
        if column not in task_columns:
            await db.execute(ddl)

    await db.commit()
    logger.info("Migration v3 applied: resumable board state and scoped entry keys")


async def _migrate_to_v4(db: aiosqlite.Connection) -> None:
    """Make retry cost records idempotent for each turn and phase."""
    await db.executescript(MIGRATION_V4_COST_IDEMPOTENCY_DDL)
    await db.commit()
    logger.info("Migration v4 applied: idempotent turn cost records")


async def _migrate_to_v5(db: aiosqlite.Connection) -> None:
    """Add the SQLite lease token that fences task lifecycle writes."""
    cursor = await db.execute("PRAGMA table_info(tasks)")
    task_columns = {row[1] for row in await cursor.fetchall()}
    if "lease_token" not in task_columns:
        await db.execute("ALTER TABLE tasks ADD COLUMN lease_token TEXT")
    await db.executescript(MIGRATION_V5_TASK_LEASE_DDL)
    db.row_factory = aiosqlite.Row
    await db.commit()
    logger.info("Migration v5 applied: fenced task lifecycle writes")


async def _migrate_to_v6(db: aiosqlite.Connection) -> None:
    """Add the durable event journal, bounded outbox, and lifecycle telemetry."""
    await db.executescript(MIGRATION_V6_EVENT_DELIVERY_DDL)
    db.row_factory = aiosqlite.Row

    cursor = await db.execute("PRAGMA table_info(tasks)")
    task_columns = {row[1] for row in await cursor.fetchall()}
    for column, ddl in (
        (
            "effective_actions",
            "ALTER TABLE tasks ADD COLUMN effective_actions INTEGER DEFAULT 0",
        ),
        (
            "state_revision",
            "ALTER TABLE tasks ADD COLUMN state_revision INTEGER DEFAULT 0",
        ),
        ("checkpoint_at", "ALTER TABLE tasks ADD COLUMN checkpoint_at TEXT"),
    ):
        if column not in task_columns:
            await db.execute(ddl)

    await db.commit()
    logger.info("Migration v6 applied: durable events and lifecycle telemetry")


async def _migrate_to_v7(db: aiosqlite.Connection) -> None:
    """Add reversible task archiving."""
    cursor = await db.execute("PRAGMA table_info(tasks)")
    task_columns = {row[1] for row in await cursor.fetchall()}
    if "archived_at" not in task_columns:
        await db.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_archived ON tasks(archived_at)"
    )
    await db.commit()
    logger.info("Migration v7 applied: reversible task archiving")


async def _migrate(db: aiosqlite.Connection, version: int) -> None:
    """Dispatch to the migration function for the given version."""
    migrations = {
        2: _migrate_to_v2,
        3: _migrate_to_v3,
        4: _migrate_to_v4,
        5: _migrate_to_v5,
        6: _migrate_to_v6,
        7: _migrate_to_v7,
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

            # Structured-logging columns on log_entries (additive, idempotent).
            # These carry per-agent structured metadata, the originating node
            # endpoint, and the correlating turn id. Added outside the version
            # bump so existing v2 databases pick them up on the next startup
            # without a schema-version migration.
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("PRAGMA table_info(log_entries)")
            log_cols = {row[1] for row in await cursor.fetchall()}
            for col, ddl in (
                ("fields", "ALTER TABLE log_entries ADD COLUMN fields TEXT"),
                ("node", "ALTER TABLE log_entries ADD COLUMN node TEXT"),
                ("turn_id", "ALTER TABLE log_entries ADD COLUMN turn_id TEXT"),
            ):
                if col not in log_cols:
                    await db.execute(ddl)
            await db.commit()

            # Execution-graph enrichment columns on turns (additive,
            # idempotent). These let the Graph tab show the full actor
            # identity, the Control Unit's per-round routing rationale, and
            # the inferred phase for each turn (doc 05 §1). Added outside the
            # version bump so existing v2 databases pick them up on the next
            # startup without a schema-version migration.
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("PRAGMA table_info(turns)")
            turn_cols = {row[1] for row in await cursor.fetchall()}
            for col, ddl in (
                ("actor", "ALTER TABLE turns ADD COLUMN actor TEXT"),
                ("rationale", "ALTER TABLE turns ADD COLUMN rationale TEXT"),
                ("phase", "ALTER TABLE turns ADD COLUMN phase TEXT"),
            ):
                if col not in turn_cols:
                    await db.execute(ddl)
            await db.commit()

            # The recovery scanner reads unfinished rows after startup. Do not
            # change their lease tokens here. Another daemon can still own them.

        db_size = os.path.getsize(DB_PATH)
        logger.info(f"SQLite ready: {DB_PATH} ({db_size} bytes)")

    except Exception as e:
        raise RuntimeError(
            f"SQLite initialization failed: {e}. "
            f"Check that /data is a valid, writable volume mount."
        ) from e


# ── Task CRUD ────────────────────────────────────────────────────────

async def create_task(
    task_id: str, label: str, full_input: str,
    variant: str = "classic",
) -> None:
    """Create a new task record with status='pending'.

    Always writes the active variant at creation time so the row never
    sits at the old schema default between INSERT and the
    triage update_task_status call.
    """
    async with _connect() as db:
        await db.execute(
            "INSERT INTO tasks (id, label, full_input, status, variant)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (task_id, label, full_input, variant),
        )
        await db.commit()


async def create_task_with_meta(
    task_id: str,
    label: str,
    full_input: str,
    variant: str,
    metadata: dict,
    *,
    run_state: str = "queued",
) -> None:
    """Create a task and its recovery metadata in one transaction."""
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await connection.execute(
                "INSERT INTO tasks "
                "(id, label, full_input, status, variant, run_state) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (task_id, label, full_input, variant, run_state),
            )
            await connection.execute(
                "INSERT INTO board_meta (task_id, data, updated_at) "
                "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (task_id, json.dumps(metadata)),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise


async def delete_task(task_id: str) -> bool:
    """Delete one task and its cascading database records."""
    async with _connect() as db:
        cursor = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
        return cursor.rowcount == 1


async def update_task_status(
    task_id: str,
    status: str | None = None,
    complexity: str | None = None,
    model_used: str | None = None,
    variant: str | None = None,
    lease_token: str | None = None,
) -> bool:
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
    if variant is not None:
        updates.append("variant = ?")
        params.append(variant)

    if not updates:
        return True

    params.append(task_id)
    where = "id = ?"
    if lease_token is not None:
        where += " AND lease_token = ?"
        params.append(lease_token)
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE {where}",
            params,
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_task_phase(
    task_id: str,
    phase: str,
    lease_token: str | None = None,
) -> bool:
    """Set the authoritative task phase with optional lease fencing."""
    where = "id = ?"
    params = [phase, task_id]
    if lease_token is not None:
        where += " AND lease_token = ?"
        params.append(lease_token)
    async with _connect() as connection:
        cursor = await connection.execute(
            f"UPDATE tasks SET phase = ? WHERE {where}",
            params,
        )
        await connection.commit()
        return cursor.rowcount == 1


async def claim_task_lease(task_id: str, lease_token: str) -> bool:
    """Claim the SQLite lifecycle lease after Redis grants its lease."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE tasks SET lease_token = ?, run_state = 'running', "
            "resume_count = COALESCE(resume_count, 0) "
            "+ CASE WHEN status = 'running' THEN 1 ELSE 0 END, "
            "last_heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (lease_token, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def release_task_lease(task_id: str, lease_token: str) -> bool:
    """Clear the SQLite lease only when the caller still owns it."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE tasks SET lease_token = NULL WHERE id = ? AND lease_token = ?",
            (task_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1


async def owns_task_lease(task_id: str, lease_token: str) -> bool:
    """Return true when one unfinished task still owns its lease token."""
    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND lease_token = ? "
            "AND status IN ('pending', 'running')",
            (task_id, lease_token),
        )
        return await cursor.fetchone() is not None


async def complete_task(
    task_id: str,
    result_summary: str,
    result_json: str,
    lease_token: str | None = None,
) -> bool:
    """Mark a task as completed with its result.

    Extracts ``rounds_completed``, ``terminated_by``, and ``answer_source``
    from *result_json* and persists them to the dedicated v2 columns so
    they are queryable without parsing the JSON blob.
    """
    # Parse terminal metadata from result JSON
    rounds_used = None
    terminated_by = None
    answer_source = None
    if result_json:
        try:
            result_data = json.loads(result_json)
            rounds_used = result_data.get("rounds_completed")
            terminated_by = result_data.get("terminated_by")
            answer_source = result_data.get("answer_source")
        except (json.JSONDecodeError, TypeError):
            pass

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
                now = datetime.now(UTC)
                duration_ms = int((now - started).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        where = "id = ? AND status IN ('pending', 'running')"
        params: list = [
            result_summary,
            result_json,
            duration_ms,
            rounds_used,
            terminated_by,
            answer_source,
            task_id,
        ]
        if lease_token is not None:
            where += " AND lease_token = ?"
            params.append(lease_token)
        cursor = await db.execute(
            "UPDATE tasks SET "
            "status = 'completed', "
            "run_state = 'succeeded', "
            "result_summary = ?, "
            "result_json = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "duration_ms = ?, "
            "rounds_used = ?, "
            "terminated_by = ?, "
            "answer_source = ?, "
            "lease_token = NULL "
            f"WHERE {where}",
            params,
        )
        await db.commit()
        return cursor.rowcount == 1


async def fail_task(
    task_id: str,
    error_message: str,
    lease_token: str | None = None,
) -> bool:
    """Mark a task as failed with an error message."""
    async with _connect() as db:
        where = "id = ? AND status IN ('pending', 'running')"
        params = [error_message, task_id]
        if lease_token is not None:
            where += " AND lease_token = ?"
            params.append(lease_token)
        cursor = await db.execute(
            "UPDATE tasks SET "
            "status = 'failed', "
            "run_state = 'failed', "
            "error_message = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_token = NULL "
            f"WHERE {where}",
            params,
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_task(task_id: str) -> dict | None:
    """Fetch a single task by ID. Returns None if not found."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        task = dict(row)
        raw_result = task.get("result_json")
        if isinstance(raw_result, str) and raw_result:
            with suppress(json.JSONDecodeError, TypeError):
                result = json.loads(raw_result)
                if isinstance(result, dict) and isinstance(
                    result.get("variant_metrics"), dict
                ):
                    task["variant_metrics"] = result["variant_metrics"]
        return task


async def get_resumable_tasks() -> list[dict]:
    """Return all unfinished tasks in creation order."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM tasks "
            "WHERE status IN ('pending', 'running') "
            "AND COALESCE(run_state, 'queued') NOT IN ('blocked', 'staging') "
            "ORDER BY created_at"
        )
        return [dict(row) for row in rows]


async def get_blocked_tasks(
    limit: int = 100,
    after: tuple[str, str] | None = None,
) -> list[dict]:
    """Return one stable page of blocked tasks for compatibility checks."""
    bounded_limit = min(max(limit, 1), 500)
    cursor_clause = ""
    params: list[Any] = []
    if after is not None:
        cursor_clause = (
            "AND (created_at > ? OR (created_at = ? AND id > ?)) "
        )
        params.extend((after[0], after[0], after[1]))
    params.append(bounded_limit)
    async with _connect() as connection:
        rows = await connection.execute_fetchall(
            "SELECT * FROM tasks "
            "WHERE status IN ('pending', 'running') AND run_state = 'blocked' "
            f"{cursor_clause}ORDER BY created_at, id LIMIT ?",
            params,
        )
        return [dict(row) for row in rows]


async def block_task_recovery(task_id: str) -> bool:
    """Block recovery for one unfinished and currently unleased task."""
    async with _connect() as connection:
        cursor = await connection.execute(
            "UPDATE tasks SET run_state = 'blocked', "
            "last_heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND status IN ('pending', 'running') "
            "AND lease_token IS NULL",
            (task_id,),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def retry_blocked_task(task_id: str) -> bool:
    """Move one unleased blocked task to recovering before a new claim."""
    async with _connect() as connection:
        cursor = await connection.execute(
            "UPDATE tasks SET run_state = 'recovering', "
            "last_heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND status IN ('pending', 'running') "
            "AND run_state = 'blocked' AND lease_token IS NULL",
            (task_id,),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def update_run_state(
    task_id: str,
    run_state: str,
    lease_token: str | None = None,
) -> bool:
    """Set the detailed execution state and refresh its heartbeat."""
    async with _connect() as db:
        where = "id = ?"
        params = [run_state, task_id]
        if lease_token is not None:
            where += " AND lease_token = ?"
            params.append(lease_token)
        cursor = await db.execute(
            "UPDATE tasks SET run_state = ?, "
            "last_heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            f"WHERE {where}",
            params,
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_task_lifecycle_telemetry(
    task_id: str,
    *,
    effective_actions: int | None = None,
    state_revision: int | None = None,
    resume_count: int | None = None,
    lease_token: str | None = None,
) -> bool:
    """Update generic task lifecycle telemetry with optional lease fencing."""
    updates: list[str] = []
    params: list = []
    for column, value in (
        ("effective_actions", effective_actions),
        ("state_revision", state_revision),
        ("resume_count", resume_count),
    ):
        if value is not None:
            updates.append(f"{column} = ?")
            params.append(value)
    if not updates:
        return True

    where = "id = ?"
    params.append(task_id)
    if lease_token is not None:
        where += " AND lease_token = ?"
        params.append(lease_token)
    async with _connect() as connection:
        cursor = await connection.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE {where}",
            params,
        )
        await connection.commit()
        return cursor.rowcount == 1


def _task_filter_clause(
    *,
    status: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_cost: float | None = None,
    max_cost: float | None = None,
    archived: str = "exclude",
) -> tuple[str, list[Any]]:
    """Build one parameterized task-history filter."""
    clauses: list[str] = []
    params: list[Any] = []
    if status == "attention":
        clauses.append(
            "(status = 'failed' OR COALESCE(run_state, '') IN "
            "('blocked', 'paused', 'pause_requested') OR "
            "(status IN ('pending', 'running') AND "
            "julianday(COALESCE(last_heartbeat_at, created_at)) < "
            "julianday('now', '-5 minutes')) OR "
            "EXISTS (SELECT 1 FROM event_journal AS request "
            "WHERE request.task_id = tasks.id "
            "AND request.event_type = 'approval_request' "
            "AND NOT EXISTS (SELECT 1 FROM event_journal AS response "
            "WHERE response.task_id = request.task_id "
            "AND response.event_type = 'approval_response' "
            "AND json_extract(response.data, '$.run_id') = "
            "json_extract(request.data, '$.run_id') "
            "AND response.cursor > request.cursor)))"
        )
    elif status:
        clauses.append("status = ?")
        params.append(status)
    if search:
        clauses.append(
            "(LOWER(id) LIKE ? OR LOWER(label) LIKE ? "
            "OR LOWER(full_input) LIKE ? OR LOWER(COALESCE(result_summary, '')) LIKE ? "
            "OR LOWER(COALESCE(result_json, '')) LIKE ? "
            "OR LOWER(COALESCE(error_message, '')) LIKE ?)"
        )
        pattern = f"%{search.strip().lower()}%"
        params.extend((pattern, pattern, pattern, pattern, pattern, pattern))
    if date_from:
        clauses.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?")
        params.append(date_to)
    if min_cost is not None:
        clauses.append("COALESCE(total_cost_usd, 0) >= ?")
        params.append(min_cost)
    if max_cost is not None:
        clauses.append("COALESCE(total_cost_usd, 0) <= ?")
        params.append(max_cost)
    if archived == "only":
        clauses.append("archived_at IS NOT NULL")
    elif archived == "exclude":
        clauses.append("archived_at IS NULL")
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)


async def list_tasks(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    *,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_cost: float | None = None,
    max_cost: float | None = None,
    archived: str = "exclude",
    sort: str = "priority",
) -> list[dict]:
    """List filtered tasks with urgent operator work first."""
    where, params = _task_filter_clause(
        status=status,
        search=search,
        date_from=date_from,
        date_to=date_to,
        min_cost=min_cost,
        max_cost=max_cost,
        archived=archived,
    )
    order_by = {
        "priority": "CASE WHEN COALESCE(run_state, '') IN ('blocked', 'paused', 'pause_requested') THEN 0 WHEN status = 'failed' THEN 1 WHEN status = 'running' THEN 2 WHEN status = 'pending' THEN 3 ELSE 4 END, created_at DESC",
        "created-desc": "created_at DESC",
        "created-asc": "created_at ASC",
        "duration-desc": "COALESCE(duration_ms, -1) DESC, created_at DESC",
        "duration-asc": "COALESCE(duration_ms, 9223372036854775807) ASC, created_at DESC",
        "cost-desc": "COALESCE(total_cost_usd, 0) DESC, created_at DESC",
        "cost-asc": "COALESCE(total_cost_usd, 0) ASC, created_at DESC",
        "status": "status ASC, created_at DESC",
        "activity-desc": "COALESCE(last_heartbeat_at, completed_at, started_at, created_at) DESC",
    }.get(sort, "created_at DESC")
    params.extend((limit, offset))
    async with _connect() as conn:
        rows = await conn.execute_fetchall(
            "SELECT tasks.*, "
            "CASE WHEN status IN ('pending', 'running') AND "
            "julianday(COALESCE(last_heartbeat_at, created_at)) < "
            "julianday('now', '-5 minutes') "
            "THEN 1 ELSE 0 END AS stale, "
            "CASE WHEN EXISTS (SELECT 1 FROM event_journal AS request "
            "WHERE request.task_id = tasks.id "
            "AND request.event_type = 'approval_request' "
            "AND NOT EXISTS (SELECT 1 FROM event_journal AS response "
            "WHERE response.task_id = request.task_id "
            "AND response.event_type = 'approval_response' "
            "AND json_extract(response.data, '$.run_id') = "
            "json_extract(request.data, '$.run_id') "
            "AND response.cursor > request.cursor)) "
            "THEN 1 ELSE 0 END AS pending_approval FROM tasks "
            f"{where} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
            params,
        )
        return [dict(r) for r in rows]


async def count_tasks(
    status: str | None = None,
    *,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_cost: float | None = None,
    max_cost: float | None = None,
    archived: str = "exclude",
) -> int:
    """Count tasks that match one task-history filter."""
    where, params = _task_filter_clause(
        status=status,
        search=search,
        date_from=date_from,
        date_to=date_to,
        min_cost=min_cost,
        max_cost=max_cost,
        archived=archived,
    )
    async with _connect() as conn:
        cursor = await conn.execute(
            f"SELECT COUNT(*) as cnt FROM tasks {where}",
            params,
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0


async def archive_tasks(task_ids: list[str], archived: bool = True) -> tuple[list[str], list[str]]:
    """Archive terminal tasks and return accepted and rejected identifiers."""
    accepted: list[str] = []
    rejected: list[str] = []
    async with _connect() as connection:
        for task_id in dict.fromkeys(task_ids):
            cursor = await connection.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
            if row is None or (archived and row["status"] not in ("completed", "failed")):
                rejected.append(task_id)
                continue
            value = datetime.now(UTC).isoformat() if archived else None
            await connection.execute(
                "UPDATE tasks SET archived_at = ? WHERE id = ?",
                (value, task_id),
            )
            accepted.append(task_id)
        await connection.commit()
    return accepted, rejected


async def get_task_analytics() -> dict[str, Any]:
    """Return complete task aggregates for the Analytics page."""
    async with _connect() as connection:
        status_rows = await connection.execute_fetchall(
            "SELECT status, COUNT(*) AS count FROM tasks "
            "WHERE archived_at IS NULL GROUP BY status"
        )
        cursor = await connection.execute(
            "SELECT COUNT(*) AS task_count, "
            "COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd, "
            "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
            "COALESCE(AVG(duration_ms), 0) AS average_duration_ms "
            "FROM tasks WHERE archived_at IS NULL"
        )
        totals = await cursor.fetchone()
        archived_cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM tasks WHERE archived_at IS NOT NULL"
        )
        archived_row = await archived_cursor.fetchone()
        return {
            "task_count": int(totals["task_count"] if totals else 0),
            "total_cost_usd": float(totals["total_cost_usd"] if totals else 0),
            "total_tokens": int(totals["total_tokens"] if totals else 0),
            "average_duration_ms": float(totals["average_duration_ms"] if totals else 0),
            "archived_count": int(archived_row["count"] if archived_row else 0),
            "by_status": {row["status"]: int(row["count"]) for row in status_rows},
        }


# ── Durable Event Journal and Outbox ────────────────────────────────

def _decode_event_row(row) -> dict:
    """Convert an event row to its public dictionary shape."""
    event = dict(row)
    raw_data = event.get("data")
    if isinstance(raw_data, str):
        with suppress(json.JSONDecodeError, TypeError):
            event["data"] = json.loads(raw_data)
    return event


async def append_delivery_event(
    stream: str,
    event_type: str,
    data: dict,
    *,
    task_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Append one authoritative event and enqueue a bounded delivery record."""
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    payload_bytes = len(payload.encode("utf-8"))
    if payload_bytes > MAX_EVENT_PAYLOAD_BYTES:
        raise EventPayloadTooLarge(
            f"Event payload is {payload_bytes} bytes. "
            f"The limit is {MAX_EVENT_PAYLOAD_BYTES} bytes."
        )

    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key is None and task_id and event_type in {"complete", "error"}:
                task_cursor = await connection.execute(
                    "SELECT status FROM tasks WHERE id = ?",
                    (task_id,),
                )
                task = await task_cursor.fetchone()
                if task and task["status"] in {"completed", "failed"}:
                    idempotency_key = f"terminal:{task_id}:{task['status']}"

            cursor = await connection.execute(
                "INSERT OR IGNORE INTO event_journal "
                "(stream, task_id, event_type, data, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (stream, task_id, event_type, payload, idempotency_key),
            )
            if cursor.rowcount == 0:
                existing_cursor = await connection.execute(
                    "SELECT * FROM event_journal "
                    "WHERE stream = ? AND idempotency_key = ?",
                    (stream, idempotency_key),
                )
                existing = await existing_cursor.fetchone()
                if not existing:
                    raise RuntimeError("The event insert did not return a row")
                if existing["event_type"] != event_type or existing["data"] != payload:
                    raise EventIdempotencyConflict(
                        f"Event key {idempotency_key!r} has different content"
                    )
                event_cursor = int(existing["cursor"])
                already_published = existing["published_at"] is not None
            else:
                event_cursor = int(cursor.lastrowid)
                already_published = False
                if task_id:
                    await connection.execute(
                        "UPDATE tasks SET "
                        "state_revision = COALESCE(state_revision, 0) + 1 "
                        "WHERE id = ?",
                        (task_id,),
                    )

            outbox_cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM event_outbox"
            )
            outbox_row = await outbox_cursor.fetchone()
            outbox_count = int(outbox_row["count"] if outbox_row else 0)
            if not already_published and outbox_count < MAX_OUTBOX_BACKLOG:
                await connection.execute(
                    "INSERT OR IGNORE INTO event_outbox (event_cursor) VALUES (?)",
                    (event_cursor,),
                )

            event_row_cursor = await connection.execute(
                "SELECT * FROM event_journal WHERE cursor = ?",
                (event_cursor,),
            )
            event_row = await event_row_cursor.fetchone()
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise

    if not event_row:
        raise RuntimeError("The durable event disappeared after insertion")
    return _decode_event_row(event_row)


async def fill_event_outbox() -> int:
    """Fill free outbox slots from unpublished journal rows."""
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM event_outbox"
            )
            row = await cursor.fetchone()
            current = int(row["count"] if row else 0)
            capacity = max(0, MAX_OUTBOX_BACKLOG - current)
            if capacity == 0:
                await connection.rollback()
                return 0
            inserted = await connection.execute(
                "INSERT OR IGNORE INTO event_outbox (event_cursor) "
                "SELECT journal.cursor FROM event_journal AS journal "
                "LEFT JOIN event_outbox AS outbox "
                "ON outbox.event_cursor = journal.cursor "
                "WHERE journal.published_at IS NULL "
                "AND outbox.event_cursor IS NULL "
                "ORDER BY journal.cursor "
                "LIMIT ?",
                (capacity,),
            )
            await connection.commit()
            return max(0, inserted.rowcount)
        except BaseException:
            await connection.rollback()
            raise


async def get_pending_delivery_events(limit: int = 100) -> list[dict]:
    """Return one bounded outbox batch in durable cursor order."""
    await fill_event_outbox()
    bounded_limit = min(max(limit, 1), 500)
    async with _connect() as connection:
        rows = await connection.execute_fetchall(
            "SELECT journal.*, outbox.attempts, outbox.last_error "
            "FROM event_outbox AS outbox "
            "JOIN event_journal AS journal "
            "ON journal.cursor = outbox.event_cursor "
            "ORDER BY journal.cursor LIMIT ?",
            (bounded_limit,),
        )
        return [_decode_event_row(row) for row in rows]


async def mark_delivery_published(event_cursor: int) -> None:
    """Mark one journal event published and remove its outbox record."""
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await connection.execute(
                "UPDATE event_journal SET "
                "published_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE cursor = ?",
                (event_cursor,),
            )
            await connection.execute(
                "DELETE FROM event_outbox WHERE event_cursor = ?",
                (event_cursor,),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise


async def mark_delivery_failed(event_cursor: int, error: str) -> None:
    """Record one failed delivery attempt without removing the event."""
    async with _connect() as connection:
        await connection.execute(
            "UPDATE event_outbox SET attempts = attempts + 1, last_error = ?, "
            "last_attempt_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE event_cursor = ?",
            (error[:1000], event_cursor),
        )
        await connection.commit()


async def get_delivery_events_after(
    stream: str,
    after_cursor: int,
    limit: int = 250,
) -> list[dict]:
    """Return a bounded replay page after one durable cursor."""
    bounded_limit = min(max(limit, 1), 500)
    async with _connect() as connection:
        rows = await connection.execute_fetchall(
            "SELECT * FROM event_journal "
            "WHERE stream = ? AND cursor > ? "
            "ORDER BY cursor LIMIT ?",
            (stream, after_cursor, bounded_limit),
        )
        return [_decode_event_row(row) for row in rows]


async def get_delivery_event_by_idempotency(
    stream: str,
    idempotency_key: str,
) -> dict | None:
    """Return one durable event by its stream-scoped identity."""
    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT * FROM event_journal "
            "WHERE stream = ? AND idempotency_key = ?",
            (stream, idempotency_key),
        )
        row = await cursor.fetchone()
        return _decode_event_row(row) if row else None


async def get_delivery_cursor_bounds(stream: str) -> dict[str, int]:
    """Return the earliest and latest durable cursors for one stream."""
    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT COALESCE(MIN(cursor), 0) AS earliest, "
            "COALESCE(MAX(cursor), 0) AS latest "
            "FROM event_journal WHERE stream = ?",
            (stream,),
        )
        row = await cursor.fetchone()
        return {
            "earliest": int(row["earliest"] if row else 0),
            "latest": int(row["latest"] if row else 0),
        }


async def get_terminal_tasks_without_events(limit: int = 100) -> list[dict]:
    """Return terminal tasks that lack their durable terminal event."""
    bounded_limit = min(max(limit, 1), 500)
    async with _connect() as connection:
        rows = await connection.execute_fetchall(
            "SELECT task.* FROM tasks AS task "
            "WHERE task.status IN ('completed', 'failed') "
            "AND NOT EXISTS ("
            "SELECT 1 FROM event_journal AS event "
            "WHERE event.stream = 'task:' || task.id "
            "AND event.idempotency_key = "
            "'terminal:' || task.id || ':' || task.status"
            ") ORDER BY task.completed_at LIMIT ?",
            (bounded_limit,),
        )
        return [dict(row) for row in rows]


async def get_terminal_tasks_missing_system_events(
    limit: int = 100,
) -> list[dict]:
    """Return terminal tasks that lack their durable system event."""
    bounded_limit = min(max(limit, 1), 500)
    async with _connect() as connection:
        rows = await connection.execute_fetchall(
            "SELECT task.* FROM tasks AS task "
            "WHERE task.status IN ('completed', 'failed') "
            "AND NOT EXISTS ("
            "SELECT 1 FROM event_journal AS event "
            "WHERE event.stream = 'system' "
            "AND event.idempotency_key = "
            "'system-terminal:' || task.id || ':' || task.status"
            ") ORDER BY task.completed_at LIMIT ?",
            (bounded_limit,),
        )
        return [dict(row) for row in rows]


async def get_event_delivery_health(task_id: str | None = None) -> dict:
    """Return journal, outbox, failure, and overload health signals."""
    where = ""
    params: list = []
    if task_id is not None:
        where = "WHERE task_id = ?"
        params.append(task_id)

    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN published_at IS NULL THEN 1 ELSE 0 END), 0) "
            "AS unpublished, "
            "MIN(CASE WHEN published_at IS NULL THEN created_at END) AS oldest, "
            "COALESCE(MAX(cursor), 0) AS latest_cursor "
            f"FROM event_journal {where}",
            params,
        )
        journal = await cursor.fetchone()

        if task_id is None:
            outbox_cursor = await connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(attempts), 0) AS failures "
                "FROM event_outbox"
            )
        else:
            outbox_cursor = await connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(outbox.attempts), 0) "
                "AS failures FROM event_outbox AS outbox "
                "JOIN event_journal AS journal "
                "ON journal.cursor = outbox.event_cursor "
                "WHERE journal.task_id = ?",
                (task_id,),
            )
        outbox = await outbox_cursor.fetchone()

    unpublished = int(journal["unpublished"] if journal else 0)
    outbox_count = int(outbox["count"] if outbox else 0)
    oldest = journal["oldest"] if journal else None
    oldest_age_seconds = 0.0
    if oldest:
        with suppress(ValueError, TypeError):
            oldest_age_seconds = max(
                0.0,
                (datetime.now(UTC) - datetime.fromisoformat(oldest)).total_seconds(),
            )
    overloaded = (
        unpublished >= OUTBOX_OVERLOAD_THRESHOLD
        or outbox_count >= MAX_OUTBOX_BACKLOG
    )
    state = "overloaded" if overloaded else "recovering" if unpublished else "healthy"
    return {
        "status": state,
        "unpublished_events": unpublished,
        "outbox_backlog": outbox_count,
        "outbox_capacity": MAX_OUTBOX_BACKLOG,
        "publish_failures": int(outbox["failures"] if outbox else 0),
        "oldest_unpublished_age_seconds": round(oldest_age_seconds, 3),
        "latest_cursor": int(journal["latest_cursor"] if journal else 0),
        "overloaded": overloaded,
    }


async def get_lifecycle_health() -> dict:
    """Return aggregate runtime telemetry for operational health."""
    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT "
            "SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running, "
            "COALESCE(SUM(effective_actions), 0) AS effective_actions, "
            "COALESCE(SUM(resume_count), 0) AS recoveries, "
            "MAX(checkpoint_at) AS latest_checkpoint_at "
            "FROM tasks"
        )
        row = await cursor.fetchone()
    return {
        "running_tasks": int(row["running"] or 0) if row else 0,
        "effective_actions": int(row["effective_actions"] or 0) if row else 0,
        "recovery_count": int(row["recoveries"] or 0) if row else 0,
        "latest_checkpoint_at": row["latest_checkpoint_at"] if row else None,
    }


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
                with suppress(json.JSONDecodeError, TypeError):
                    d["depends_on"] = json.loads(d["depends_on"])
            result.append(d)
        return result


# ── Debate CRUD ──────────────────────────────────────────────────────

async def get_debate(task_id: str) -> list[dict]:
    """Fetch all debate entries for a task, ordered chronologically."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM debate_entries WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        return [dict(r) for r in rows]


# ── Cost CRUD ────────────────────────────────────────────────────────

async def update_task_cost_totals(
    task_id: str,
    lease_token: str | None = None,
    reported_cost_usd: float | None = None,
) -> bool:
    """Save ledger totals and one runtime-reported cumulative cost floor."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT "
            "  COALESCE(SUM(cost_usd), 0.0) as total_cost, "
            "  COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens, "
            "  COALESCE((SELECT total_cost_usd FROM tasks WHERE id = ?), 0.0) "
            "  as current_total_cost "
            "FROM cost_entries WHERE task_id = ?",
            (task_id, task_id),
        )
        row = await cursor.fetchone()
        if row:
            total_cost = max(
                float(row["total_cost"]),
                float(row["current_total_cost"]),
            )
            if reported_cost_usd is not None:
                total_cost = max(total_cost, max(0.0, reported_cost_usd))
            where = "id = ?"
            params = [total_cost, row["total_tokens"], task_id]
            if lease_token is not None:
                where += " AND lease_token = ?"
                params.append(lease_token)
            updated = await db.execute(
                "UPDATE tasks SET total_cost_usd = ?, total_tokens = ? "
                f"WHERE {where}",
                params,
            )
            await db.commit()
            return updated.rowcount == 1
        return False


async def get_task_cost_summary(task_id: str) -> dict:
    """Aggregated cost breakdown by model, phase, and actor.

    Returns: {
        total_cost_usd, total_tokens,
        by_model: [{model, input_tokens, output_tokens, cost_usd}],
        by_phase: [{phase, cost_usd, tokens}],
        by_actor: [{actor, cost_usd, tokens, turns}]
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
        # By actor (join cost_entries → turns via turn_id)
        actor_rows = await conn.execute_fetchall(
            "SELECT COALESCE(t.actor, 'control_plane') as actor, "
            "  SUM(c.cost_usd) as cost_usd, "
            "  SUM(c.input_tokens + c.output_tokens) as tokens, "
            "  COUNT(DISTINCT c.turn_id) as turns "
            "FROM cost_entries c "
            "LEFT JOIN turns t ON c.turn_id = t.id AND c.task_id = t.task_id "
            "WHERE c.task_id = ? GROUP BY actor ORDER BY cost_usd DESC",
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
            "by_actor": [dict(r) for r in actor_rows],
        }


# ── Log CRUD ─────────────────────────────────────────────────────────

async def insert_log_entry(
    task_id: str,
    agent_role: str,
    level: str,
    message: str,
    fields: dict | None = None,
    node: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Insert a log entry (permanent archive — Redis streams are ephemeral).

    `fields` is an arbitrary structured payload (reasoning, tool calls,
    routing rationale, usage/cost, board reads/writes, …) stored as a JSON
    blob so the UI can render a full, lossless detail view.
    """
    fields_json = json.dumps(fields) if fields else None
    async with _connect() as db:
        await db.execute(
            "INSERT INTO log_entries "
            "(task_id, agent_role, level, message, fields, node, turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, agent_role, level, message, fields_json, node, turn_id),
        )
        await db.commit()


def _decode_log_row(row) -> dict:
    """Convert a log_entries row to a dict, decoding the JSON `fields` blob."""
    d = dict(row)
    raw_fields = d.get("fields")
    if isinstance(raw_fields, str) and raw_fields:
        try:
            d["fields"] = json.loads(raw_fields)
        except (json.JSONDecodeError, TypeError):
            d["fields"] = {"_raw": raw_fields}
    return d


async def get_task_logs(
    task_id: str, limit: int = 200, offset: int = 0
) -> list[dict]:
    """Fetch log entries for a task with pagination (structured fields decoded)."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM log_entries WHERE task_id = ? "
            "ORDER BY id LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        )
        return [_decode_log_row(r) for r in rows]


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
                "INSERT OR IGNORE INTO agent_traces "
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
                with suppress(json.JSONDecodeError, TypeError):
                    d["data"] = json.loads(d["data"])
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
                with suppress(json.JSONDecodeError, TypeError):
                    d["data"] = json.loads(d["data"])
            result.append(d)
        return result


# ── Turns CRUD (Phase 1, doc 07 §3) ──────────────────────────────────

async def create_turn(turn: dict) -> None:
    """Create a new turn record (one row per KS activation).

    `actor`, `rationale`, and `phase` are additive enrichment columns
    (doc 05 §1) that power the execution-graph visualization: the full
    opaque actor id (e.g. ``expert.valuation_analyst`` rather than the
    base ``expert`` role), the Control Unit's routing rationale for this
    round, and the inferred board phase. They default to NULL so older
    callers and pre-migration databases remain compatible.
    """
    async with _connect() as db:
        await db.execute(
            "INSERT INTO turns "
            "(id, task_id, round_no, role, actor, node, model, status, "
            "rationale, phase) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn["id"],
                turn["task_id"],
                turn.get("round_no", 1),
                turn["role"],
                turn.get("actor") or turn["role"],
                turn.get("node"),
                turn.get("model"),
                turn.get("status", "running"),
                turn.get("rationale"),
                turn.get("phase"),
            ),
        )
        await db.commit()


async def complete_turn(
    turn_id: str,
    status: str,
    entries_added: int,
    cost_usd: float,
    joules_estimate: float = 0.0,
    node: str | None = None,
) -> None:
    """Mark a turn terminal and save the endpoint that returned."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT task_id, status FROM turns WHERE id = ?",
            (turn_id,),
        )
        current = await cursor.fetchone()
        await db.execute(
            "UPDATE turns SET "
            "status = ?, entries_added = ?, cost_usd = ?, "
            "joules_estimate = ?, node = COALESCE(?, node), "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (
                status,
                entries_added,
                cost_usd,
                joules_estimate,
                node,
                turn_id,
            ),
        )
        if (
            current
            and current["status"] not in TURN_TERMINAL_STATUSES
            and status in TURN_TERMINAL_STATUSES
        ):
            await db.execute(
                "UPDATE tasks SET "
                "effective_actions = COALESCE(effective_actions, 0) + 1 "
                "WHERE id = ?",
                (current["task_id"],),
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
            "INSERT OR IGNORE INTO cost_entries "
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

async def upsert_board_entry(
    entry: dict,
    lease_token: str | None = None,
) -> None:
    """Insert or update a board entry in the durable snapshot.

    Uses INSERT OR REPLACE so that re-folding from events produces
    the same result as incremental updates.
    """
    refs = entry.get("refs", [])
    if isinstance(refs, list):
        refs = json.dumps(refs)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, entry["task_id"], lease_token)
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
        except BaseException:
            await db.rollback()
            raise


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
    lease_token: str | None = None,
) -> None:
    """Insert a board event into the durable event log.

    This is the SQLite-first write (doc 04 §5.1 durability contract).
    The caller must handle Redis separately.
    """
    payload_str = payload if isinstance(payload, str) else json.dumps(payload)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, task_id, lease_token)
            cursor = await db.execute(
                "INSERT OR IGNORE INTO board_events "
                "(task_id, seq, round, turn_id, actor, event_type, "
                "entry_id, payload, redis_stream_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, seq, round_no, turn_id, actor,
                    event_type, entry_id, payload_str, redis_stream_id,
                ),
            )
            if cursor.rowcount == 0:
                existing_cursor = await db.execute(
                    "SELECT actor, event_type, entry_id, payload FROM board_events "
                    "WHERE task_id = ? AND seq = ?",
                    (task_id, seq),
                )
                existing = await existing_cursor.fetchone()
                expected = (actor, event_type, entry_id, payload_str)
                actual = tuple(existing) if existing else None
                if actual != expected:
                    raise RuntimeError(
                        f"Board event sequence conflict for {task_id} at {seq}"
                    )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


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
                with suppress(json.JSONDecodeError, TypeError):
                    d["payload"] = json.loads(d["payload"])
            result.append(d)
        return result


async def import_legacy_board_snapshot(
    task_id: str,
    events: list[dict],
    entries: list[dict],
    meta: dict,
    lease_token: str | None = None,
) -> int:
    """Import a legacy board snapshot in one SQLite transaction.

    The event-log existence check and all writes share one write lock. A crash
    therefore leaves either the old empty board or the complete imported board.
    """
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, task_id, lease_token)
            cursor = await db.execute(
                "SELECT 1 FROM board_events WHERE task_id = ? LIMIT 1",
                (task_id,),
            )
            if await cursor.fetchone():
                await db.rollback()
                return 0

            for event in events:
                payload = event.get("payload", {})
                payload_json = (
                    payload if isinstance(payload, str) else json.dumps(payload)
                )
                await db.execute(
                    "INSERT INTO board_events "
                    "(task_id, seq, round, turn_id, actor, event_type, "
                    "entry_id, payload, redis_stream_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        int(event.get("seq", 0)),
                        event.get("round"),
                        event.get("turn_id"),
                        str(event.get("actor", "unknown")),
                        str(event.get("event_type", "entry_added")),
                        event.get("entry_id"),
                        payload_json,
                        event.get("redis_stream_id"),
                    ),
                )

            for entry in entries:
                refs = entry.get("refs", [])
                refs_json = refs if isinstance(refs, str) else json.dumps(refs)
                await db.execute(
                    "INSERT OR REPLACE INTO board_entries "
                    "(id, task_id, type, author, author_node, title, body, refs, "
                    "confidence, status, salience, round, space, created_by_turn, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["id"],
                        task_id,
                        entry["type"],
                        entry["author"],
                        entry.get("author_node"),
                        entry.get("title"),
                        entry.get("body"),
                        refs_json,
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

            await db.execute(
                "INSERT INTO board_meta (task_id, data, updated_at) "
                "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (task_id, json.dumps(meta)),
            )
            await db.commit()
            return len(events)
        except BaseException:
            await db.rollback()
            raise


async def update_board_entry_status(
    task_id: str,
    entry_id: str,
    status: str,
    lease_token: str | None = None,
) -> None:
    """Update the status of a board entry."""
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, task_id, lease_token)
            await db.execute(
                "UPDATE board_entries SET status = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND task_id = ?",
                (status, entry_id, task_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def update_board_entry_salience(
    task_id: str,
    entry_id: str,
    salience: float,
    lease_token: str | None = None,
) -> None:
    """Update the salience score of a board entry."""
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, task_id, lease_token)
            await db.execute(
                "UPDATE board_entries SET salience = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND task_id = ?",
                (salience, entry_id, task_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def update_board_entry_saliences(
    task_id: str,
    scores: dict[str, float],
    lease_token: str | None = None,
) -> None:
    """Update multiple salience scores in one fenced transaction."""
    if not scores:
        return
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, task_id, lease_token)
            await db.executemany(
                "UPDATE board_entries SET salience = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND task_id = ?",
                [
                    (salience, entry_id, task_id)
                    for entry_id, salience in scores.items()
                ],
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def upsert_board_meta(
    task_id: str,
    meta: dict,
    lease_token: str | None = None,
) -> None:
    """Persist the complete classic-board control metadata."""
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, task_id, lease_token)
            await db.execute(
                "INSERT INTO board_meta (task_id, data, updated_at) "
                "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (task_id, json.dumps(meta)),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def mark_task_checkpoint(
    task_id: str,
    lease_token: str | None = None,
) -> bool:
    """Record an explicit durable task checkpoint with optional fencing."""
    where = "id = ?"
    params = [task_id]
    if lease_token is not None:
        where += " AND lease_token = ?"
        params.append(lease_token)
    async with _connect() as connection:
        cursor = await connection.execute(
            "UPDATE tasks SET "
            "checkpoint_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            f"WHERE {where}",
            params,
        )
        await connection.commit()
        return cursor.rowcount == 1


async def get_board_meta(task_id: str) -> dict:
    """Read the persisted classic-board control metadata."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT data FROM board_meta WHERE task_id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        if not row or not row["data"]:
            return {}
        try:
            value = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}


async def delete_board_entries_in_space(
    task_id: str,
    space: str,
    lease_token: str | None = None,
) -> None:
    """Remove an archived private-space snapshot from SQLite."""
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, task_id, lease_token)
            await db.execute(
                "DELETE FROM board_entries WHERE task_id = ? AND space = ?",
                (task_id, space),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


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


async def get_task_files_total_bytes(task_id: str) -> int:
    """Return total bytes of all user inputs for one task."""
    async with _connect() as db, db.execute(
        "SELECT COALESCE(SUM(bytes), 0) FROM task_files WHERE task_id = ?",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0


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

async def update_task_output_dir(task_id: str, output_dir: str) -> None:
    """Set the output_dir column on the tasks row (B4 fix)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE tasks SET output_dir = ? WHERE id = ?",
            (output_dir, task_id),
        )
        await db.commit()


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


async def get_artifact_version(
    task_id: str,
    rel_path: str,
    version: int,
) -> dict | None:
    """Return one artifact path version."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM artifacts "
            "WHERE task_id = ? AND rel_path = ? AND version = ?",
            (task_id, rel_path, version),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_artifact_stored_path(
    artifact_id: str,
    stored_path: str,
) -> bool:
    """Point one artifact version at its immutable stored file."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE artifacts SET stored_path = ? WHERE id = ?",
            (stored_path, artifact_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_artifact_max_version(task_id: str, rel_path: str) -> int:
    """Return the current highest version number for a given (task_id, rel_path)."""
    async with _connect() as db, db.execute(
        "SELECT MAX(version) FROM artifacts WHERE task_id = ? AND rel_path = ?",
        (task_id, rel_path),
    ) as cur:
        row = await cur.fetchone()
        return row[0] if row and row[0] else 0


async def get_task_artifacts_total_bytes(task_id: str) -> int:
    """Return total bytes of all artifacts for a task (quota enforcement)."""
    async with _connect() as db, db.execute(
        "SELECT COALESCE(SUM(bytes), 0) FROM artifacts WHERE task_id = ?",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
        return row[0] if row else 0
