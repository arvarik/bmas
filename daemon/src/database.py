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
SCHEMA_VERSION = 14


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
MAX_OUTBOX_BACKLOG = _bounded_env_int("BMAS_EVENT_OUTBOX_MAX", 10_000, 100, 1_000_000)
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


class DatasetVersionConflict(RuntimeError):
    """A dataset already contains one version with the same checksum."""


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
    sources       TEXT,
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


MIGRATION_V8_BENCHMARK_FOUNDATION_DDL = """
CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    source_uri      TEXT,
    license         TEXT,
    author          TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    archived_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_datasets_created ON datasets(created_at DESC);

CREATE TABLE IF NOT EXISTS dataset_versions (
    id              TEXT PRIMARY KEY,
    dataset_id      TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL CHECK (version > 0),
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','published')),
    checksum        TEXT NOT NULL,
    item_count      INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    schema_json     TEXT NOT NULL DEFAULT '{}',
    source_filename TEXT,
    source_mime     TEXT,
    source_checksum TEXT,
    source_path     TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    published_at    TEXT,
    UNIQUE(dataset_id, version),
    UNIQUE(dataset_id, checksum)
);

CREATE INDEX IF NOT EXISTS idx_dataset_versions_dataset
ON dataset_versions(dataset_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_dataset_versions_checksum
ON dataset_versions(checksum);

CREATE TABLE IF NOT EXISTS dataset_items (
    id                  TEXT PRIMARY KEY,
    dataset_version_id  TEXT NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    item_key            TEXT NOT NULL,
    input               TEXT NOT NULL,
    expected_output     TEXT NOT NULL,
    subject             TEXT,
    split               TEXT,
    tags                TEXT NOT NULL DEFAULT '[]',
    metadata            TEXT NOT NULL DEFAULT '{}',
    sort_order          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(dataset_version_id, item_key)
);

CREATE INDEX IF NOT EXISTS idx_dataset_items_version
ON dataset_items(dataset_version_id, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_dataset_items_subject
ON dataset_items(dataset_version_id, subject);
CREATE INDEX IF NOT EXISTS idx_dataset_items_split
ON dataset_items(dataset_version_id, split);

CREATE TABLE IF NOT EXISTS benchmark_scorers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    configuration_schema TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS benchmark_tests (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    archived_at     TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_test_revisions (
    id                  TEXT PRIMARY KEY,
    test_id             TEXT NOT NULL REFERENCES benchmark_tests(id) ON DELETE CASCADE,
    revision            INTEGER NOT NULL CHECK (revision > 0),
    dataset_version_id  TEXT NOT NULL REFERENCES dataset_versions(id),
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','published')),
    configuration       TEXT NOT NULL DEFAULT '{}',
    configuration_checksum TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    published_at        TEXT,
    UNIQUE(test_id, revision)
);

CREATE TABLE IF NOT EXISTS benchmark_test_arms (
    id                  TEXT PRIMARY KEY,
    test_revision_id    TEXT NOT NULL REFERENCES benchmark_test_revisions(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    slug                TEXT NOT NULL,
    runtime_id          TEXT NOT NULL,
    configuration       TEXT NOT NULL DEFAULT '{}',
    configuration_checksum TEXT NOT NULL,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(test_revision_id, slug)
);

CREATE TABLE IF NOT EXISTS benchmark_runs (
    id                  TEXT PRIMARY KEY,
    test_revision_id    TEXT NOT NULL REFERENCES benchmark_test_revisions(id),
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','paused','completed','failed','cancelling','cancelled','partial')),
    execution_plan      TEXT NOT NULL DEFAULT '{}',
    execution_plan_checksum TEXT NOT NULL,
    total_trials        INTEGER NOT NULL DEFAULT 0 CHECK (total_trials >= 0),
    completed_trials    INTEGER NOT NULL DEFAULT 0 CHECK (completed_trials >= 0),
    operator_note       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at          TEXT,
    completed_at        TEXT,
    archived_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_benchmark_runs_revision
ON benchmark_runs(test_revision_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_benchmark_runs_status
ON benchmark_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS benchmark_trials (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES benchmark_runs(id) ON DELETE CASCADE,
    test_arm_id         TEXT NOT NULL REFERENCES benchmark_test_arms(id),
    dataset_item_id     TEXT NOT NULL REFERENCES dataset_items(id),
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','paused','completed','failed','cancelling','cancelled','excluded')),
    current_attempt_id  TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at          TEXT,
    completed_at        TEXT,
    UNIQUE(run_id, test_arm_id, dataset_item_id)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_trials_run
ON benchmark_trials(run_id, status, id);

CREATE TABLE IF NOT EXISTS benchmark_attempts (
    id                  TEXT PRIMARY KEY,
    trial_id            TEXT NOT NULL REFERENCES benchmark_trials(id) ON DELETE CASCADE,
    attempt_number      INTEGER NOT NULL CHECK (attempt_number > 0),
    task_id             TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','completed','failed','cancelling','cancelled')),
    execution_snapshot  TEXT NOT NULL,
    snapshot_checksum   TEXT NOT NULL,
    failure_category    TEXT,
    error_message       TEXT,
    operator_intervened INTEGER NOT NULL DEFAULT 0 CHECK (operator_intervened IN (0,1)),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at          TEXT,
    completed_at        TEXT,
    UNIQUE(trial_id, attempt_number),
    UNIQUE(task_id)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_attempts_trial
ON benchmark_attempts(trial_id, attempt_number DESC);

CREATE TABLE IF NOT EXISTS benchmark_scores (
    id                  TEXT PRIMARY KEY,
    attempt_id          TEXT NOT NULL REFERENCES benchmark_attempts(id) ON DELETE CASCADE,
    scorer_id           TEXT NOT NULL REFERENCES benchmark_scorers(id),
    status              TEXT NOT NULL CHECK (status IN ('scored','error','excluded')),
    score               REAL,
    passed              INTEGER CHECK (passed IN (0,1)),
    extracted_output    TEXT,
    explanation         TEXT,
    evidence            TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(attempt_id, scorer_id)
);

CREATE TABLE IF NOT EXISTS benchmark_artifacts (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES benchmark_runs(id) ON DELETE CASCADE,
    attempt_id          TEXT REFERENCES benchmark_attempts(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    uri                 TEXT NOT NULL,
    checksum            TEXT NOT NULL,
    metadata            TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS operator_actions (
    action_id           TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action              TEXT NOT NULL,
    actor               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'requested'
                        CHECK (status IN ('requested','accepted','rejected','failed')),
    request_detail      TEXT NOT NULL DEFAULT '{}',
    result_detail       TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_operator_actions_task
ON operator_actions(task_id, created_at, action_id);

CREATE TRIGGER IF NOT EXISTS prevent_published_dataset_version_update
BEFORE UPDATE ON dataset_versions
WHEN OLD.status = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published dataset versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_dataset_version_delete
BEFORE DELETE ON dataset_versions
WHEN OLD.status = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published dataset versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_dataset_item_insert
BEFORE INSERT ON dataset_items
WHEN (SELECT status FROM dataset_versions WHERE id = NEW.dataset_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published dataset items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_dataset_item_update
BEFORE UPDATE ON dataset_items
WHEN (SELECT status FROM dataset_versions WHERE id = OLD.dataset_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published dataset items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_dataset_item_delete
BEFORE DELETE ON dataset_items
WHEN (SELECT status FROM dataset_versions WHERE id = OLD.dataset_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published dataset items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_test_revision_update
BEFORE UPDATE ON benchmark_test_revisions
WHEN OLD.status = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published test revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_test_revision_delete
BEFORE DELETE ON benchmark_test_revisions
WHEN OLD.status = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published test revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_test_arm_insert
BEFORE INSERT ON benchmark_test_arms
WHEN (SELECT status FROM benchmark_test_revisions WHERE id = NEW.test_revision_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published test arms are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_test_arm_update
BEFORE UPDATE ON benchmark_test_arms
WHEN (SELECT status FROM benchmark_test_revisions WHERE id = OLD.test_revision_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published test arms are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_test_arm_delete
BEFORE DELETE ON benchmark_test_arms
WHEN (SELECT status FROM benchmark_test_revisions WHERE id = OLD.test_revision_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published test arms are immutable');
END;
"""


MIGRATION_V9_BENCHMARK_EXECUTION_DDL = """
CREATE TABLE IF NOT EXISTS benchmark_test_revision_scorers (
    test_revision_id    TEXT NOT NULL
                        REFERENCES benchmark_test_revisions(id) ON DELETE CASCADE,
    scorer_id           TEXT NOT NULL REFERENCES benchmark_scorers(id),
    sort_order          INTEGER NOT NULL DEFAULT 0,
    configuration       TEXT NOT NULL DEFAULT '{}',
    configuration_checksum TEXT NOT NULL,
    PRIMARY KEY(test_revision_id, scorer_id)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_revision_scorers
ON benchmark_test_revision_scorers(test_revision_id, sort_order);

CREATE TRIGGER IF NOT EXISTS prevent_published_test_scorer_insert
BEFORE INSERT ON benchmark_test_revision_scorers
WHEN (SELECT status FROM benchmark_test_revisions WHERE id = NEW.test_revision_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published test scorers are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_test_scorer_update
BEFORE UPDATE ON benchmark_test_revision_scorers
WHEN (SELECT status FROM benchmark_test_revisions WHERE id = OLD.test_revision_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published test scorers are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_published_test_scorer_delete
BEFORE DELETE ON benchmark_test_revision_scorers
WHEN (SELECT status FROM benchmark_test_revisions WHERE id = OLD.test_revision_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published test scorers are immutable');
END;

CREATE INDEX IF NOT EXISTS idx_benchmark_attempts_status
ON benchmark_attempts(status, created_at);
"""


MIGRATION_V10_BENCHMARK_ANALYSIS_DDL = """
CREATE TABLE IF NOT EXISTS benchmark_baselines (
    id                  TEXT PRIMARY KEY,
    test_id             TEXT NOT NULL REFERENCES benchmark_tests(id),
    run_id              TEXT NOT NULL UNIQUE REFERENCES benchmark_runs(id),
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    rules               TEXT NOT NULL,
    rules_checksum      TEXT NOT NULL,
    created_by          TEXT NOT NULL DEFAULT 'operator',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(test_id, name)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_baselines_test
ON benchmark_baselines(test_id, created_at DESC);

CREATE TABLE IF NOT EXISTS benchmark_gate_evaluations (
    id                  TEXT PRIMARY KEY,
    baseline_id         TEXT NOT NULL REFERENCES benchmark_baselines(id),
    candidate_run_id    TEXT NOT NULL REFERENCES benchmark_runs(id),
    status              TEXT NOT NULL CHECK (status IN ('passed','failed','indeterminate')),
    report              TEXT NOT NULL,
    report_checksum     TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(baseline_id, candidate_run_id)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_gate_evaluations_baseline
ON benchmark_gate_evaluations(baseline_id, created_at DESC);

CREATE TABLE IF NOT EXISTS benchmark_runtime_qualifications (
    id                  TEXT PRIMARY KEY,
    runtime_id          TEXT NOT NULL,
    contract_version    TEXT NOT NULL,
    evidence_key        TEXT NOT NULL,
    run_id              TEXT REFERENCES benchmark_runs(id),
    status              TEXT NOT NULL CHECK (status IN ('provisional','passed','failed')),
    report              TEXT NOT NULL,
    report_checksum     TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(runtime_id, contract_version, evidence_key)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_runtime_qualifications_runtime
ON benchmark_runtime_qualifications(runtime_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_benchmark_baseline_update
BEFORE UPDATE ON benchmark_baselines BEGIN
    SELECT RAISE(ABORT, 'benchmark baselines are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_benchmark_baseline_delete
BEFORE DELETE ON benchmark_baselines BEGIN
    SELECT RAISE(ABORT, 'benchmark baselines are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_benchmark_gate_evaluation_update
BEFORE UPDATE ON benchmark_gate_evaluations BEGIN
    SELECT RAISE(ABORT, 'benchmark gate evaluations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_benchmark_gate_evaluation_delete
BEFORE DELETE ON benchmark_gate_evaluations BEGIN
    SELECT RAISE(ABORT, 'benchmark gate evaluations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_benchmark_runtime_qualification_update
BEFORE UPDATE ON benchmark_runtime_qualifications BEGIN
    SELECT RAISE(ABORT, 'benchmark runtime qualifications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_benchmark_runtime_qualification_delete
BEFORE DELETE ON benchmark_runtime_qualifications BEGIN
    SELECT RAISE(ABORT, 'benchmark runtime qualifications are immutable');
END;
"""


MIGRATION_V11_BENCHMARK_SCHEDULER_DDL = """
CREATE TABLE IF NOT EXISTS benchmark_scheduler_workers (
    worker_id           TEXT PRIMARY KEY,
    hostname            TEXT NOT NULL,
    process_id          INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','stopped')),
    started_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    stopped_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_benchmark_scheduler_workers_seen
ON benchmark_scheduler_workers(status, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_benchmark_attempts_lease
ON benchmark_attempts(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS benchmark_human_reviews (
    id                  TEXT PRIMARY KEY,
    attempt_id          TEXT NOT NULL REFERENCES benchmark_attempts(id) ON DELETE CASCADE,
    reviewer_id         TEXT NOT NULL,
    score               REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    passed              INTEGER NOT NULL CHECK (passed IN (0,1)),
    note                TEXT NOT NULL DEFAULT '',
    idempotency_key     TEXT NOT NULL UNIQUE,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(attempt_id, reviewer_id)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_human_reviews_attempt
ON benchmark_human_reviews(attempt_id, created_at);

CREATE TRIGGER IF NOT EXISTS prevent_benchmark_human_review_update
BEFORE UPDATE ON benchmark_human_reviews BEGIN
    SELECT RAISE(ABORT, 'benchmark human reviews are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_benchmark_human_review_delete
BEFORE DELETE ON benchmark_human_reviews BEGIN
    SELECT RAISE(ABORT, 'benchmark human reviews are immutable');
END;
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
    composite_pk = {row[1] for row in board_columns if int(row[5] or 0) > 0} == {"task_id", "id"}

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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_archived ON tasks(archived_at)")
    await db.commit()
    logger.info("Migration v7 applied: reversible task archiving")


async def _migrate_to_v8(db: aiosqlite.Connection) -> None:
    """Add benchmark records, immutable revisions, and task terminal identity."""
    await db.executescript(MIGRATION_V8_BENCHMARK_FOUNDATION_DDL)
    db.row_factory = aiosqlite.Row

    cursor = await db.execute("PRAGMA table_info(tasks)")
    task_columns = {row[1] for row in await cursor.fetchall()}
    for column, ddl in (
        (
            "terminal_kind",
            "ALTER TABLE tasks ADD COLUMN terminal_kind TEXT "
            "CHECK (terminal_kind IN ('completed','failed','cancelled'))",
        ),
        (
            "failure_category",
            "ALTER TABLE tasks ADD COLUMN failure_category TEXT",
        ),
        (
            "cancel_requested_at",
            "ALTER TABLE tasks ADD COLUMN cancel_requested_at TEXT",
        ),
    ):
        if column not in task_columns:
            await db.execute(ddl)

    await db.execute(
        "UPDATE tasks SET terminal_kind = CASE "
        "WHEN status = 'completed' THEN 'completed' "
        "WHEN status = 'failed' AND (run_state IN ('aborted','cancelled') "
        "OR error_message LIKE 'Task aborted:%') THEN 'cancelled' "
        "WHEN status = 'failed' THEN 'failed' ELSE NULL END "
        "WHERE terminal_kind IS NULL"
    )
    await db.execute(
        "UPDATE tasks SET run_state = 'cancelled', failure_category = 'cancelled' "
        "WHERE terminal_kind = 'cancelled'"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_terminal_kind "
        "ON tasks(terminal_kind, completed_at DESC)"
    )
    await db.executemany(
        "INSERT OR IGNORE INTO benchmark_scorers "
        "(id, name, version, kind, configuration_schema) VALUES (?, ?, ?, ?, ?)",
        (
            (
                "scorer-gsm8k-numeric-v1",
                "GSM8K numeric match",
                "1",
                "numeric_match",
                "{}",
            ),
            (
                "scorer-mmlu-letter-v1",
                "MMLU letter match",
                "1",
                "letter_match",
                "{}",
            ),
        ),
    )
    await db.commit()
    logger.info("Migration v8 applied: benchmark foundation and task terminal identity")


async def _migrate_to_v9(db: aiosqlite.Connection) -> None:
    """Add versioned scorers and durable benchmark execution controls."""
    await db.executescript(MIGRATION_V9_BENCHMARK_EXECUTION_DDL)
    db.row_factory = aiosqlite.Row

    cursor = await db.execute("PRAGMA table_info(benchmark_runs)")
    run_columns = {row[1] for row in await cursor.fetchall()}
    for column, ddl in (
        (
            "idempotency_key",
            "ALTER TABLE benchmark_runs ADD COLUMN idempotency_key TEXT",
        ),
        (
            "total_attempts",
            "ALTER TABLE benchmark_runs ADD COLUMN total_attempts INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "completed_attempts",
            "ALTER TABLE benchmark_runs ADD COLUMN completed_attempts INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "cancel_reason",
            "ALTER TABLE benchmark_runs ADD COLUMN cancel_reason TEXT",
        ),
        (
            "state_revision",
            "ALTER TABLE benchmark_runs ADD COLUMN state_revision INTEGER NOT NULL DEFAULT 0",
        ),
    ):
        if column not in run_columns:
            await db.execute(ddl)

    cursor = await db.execute("PRAGMA table_info(benchmark_attempts)")
    attempt_columns = {row[1] for row in await cursor.fetchall()}
    for column, ddl in (
        (
            "repeat_index",
            "ALTER TABLE benchmark_attempts ADD COLUMN repeat_index INTEGER NOT NULL DEFAULT 1",
        ),
        (
            "retry_index",
            "ALTER TABLE benchmark_attempts ADD COLUMN retry_index INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "random_seed",
            "ALTER TABLE benchmark_attempts ADD COLUMN random_seed INTEGER",
        ),
        (
            "claimed_at",
            "ALTER TABLE benchmark_attempts ADD COLUMN claimed_at TEXT",
        ),
    ):
        if column not in attempt_columns:
            await db.execute(ddl)

    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_benchmark_runs_idempotency "
        "ON benchmark_runs(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_benchmark_attempt_repeat_retry "
        "ON benchmark_attempts(trial_id, repeat_index, retry_index)"
    )
    await db.execute(
        "INSERT OR IGNORE INTO benchmark_scorers "
        "(id, name, version, kind, configuration_schema) "
        "VALUES ('scorer-exact-match-v1', 'Exact text match', '1', "
        "'exact_match', '{}')"
    )
    await db.commit()
    logger.info("Migration v9 applied: benchmark authoring, execution, and scoring")


async def _migrate_to_v10(db: aiosqlite.Connection) -> None:
    """Add immutable baselines, gate results, and runtime qualifications."""
    await db.executescript(MIGRATION_V10_BENCHMARK_ANALYSIS_DDL)
    db.row_factory = aiosqlite.Row
    await db.commit()
    logger.info("Migration v10 applied: benchmark analysis and regression gates")


async def _migrate_to_v11(db: aiosqlite.Connection) -> None:
    """Add fenced benchmark attempt leases and queue priorities."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("PRAGMA table_info(benchmark_runs)")
    run_columns = {row[1] for row in await cursor.fetchall()}
    if "priority" not in run_columns:
        await db.execute(
            "ALTER TABLE benchmark_runs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
        )

    cursor = await db.execute("PRAGMA table_info(benchmark_attempts)")
    attempt_columns = {row[1] for row in await cursor.fetchall()}
    for column, ddl in (
        (
            "lease_owner",
            "ALTER TABLE benchmark_attempts ADD COLUMN lease_owner TEXT",
        ),
        (
            "lease_token",
            "ALTER TABLE benchmark_attempts ADD COLUMN lease_token TEXT",
        ),
        (
            "lease_expires_at",
            "ALTER TABLE benchmark_attempts ADD COLUMN lease_expires_at TEXT",
        ),
        (
            "lease_fence",
            "ALTER TABLE benchmark_attempts ADD COLUMN lease_fence INTEGER NOT NULL DEFAULT 0",
        ),
    ):
        if column not in attempt_columns:
            await db.execute(ddl)
    await db.executescript(MIGRATION_V11_BENCHMARK_SCHEDULER_DDL)
    db.row_factory = aiosqlite.Row
    await db.commit()
    logger.info("Migration v11 applied: fenced benchmark scheduling")


async def _migrate_to_v12(db: aiosqlite.Connection) -> None:
    """Add the sources column that carries external evidence citations."""
    cursor = await db.execute("PRAGMA table_info(board_entries)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "sources" not in columns:
        await db.execute("ALTER TABLE board_entries ADD COLUMN sources TEXT")
    await db.commit()
    logger.info("Migration v12 applied: board entry sources")


async def _migrate_add_runtime_pair(db: aiosqlite.Connection) -> None:
    """Add the stored runtime contract version to every task row.

    Every task now persists its exact runtime pair before queue
    admission. The backfill stamps the historical rows with contract
    version "1", because every runtime registered before this
    migration declares that contract version.
    """
    cursor = await db.execute("PRAGMA table_info(tasks)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "runtime_contract_version" not in columns:
        await db.execute(
            "ALTER TABLE tasks ADD COLUMN runtime_contract_version TEXT"
        )
    await db.execute(
        "UPDATE tasks SET runtime_contract_version = '1' "
        "WHERE runtime_contract_version IS NULL"
    )
    await db.commit()
    logger.info("Migration 13 applied: stored runtime pair on tasks")


RUN_CONTROLS_DDL = """
CREATE TABLE IF NOT EXISTS run_controls (
    run_id                  TEXT PRIMARY KEY,
    task_id                 TEXT NOT NULL,
    lease_owner             TEXT,
    lease_fence             TEXT,
    lease_acquired_at       TEXT,
    lease_renewed_at        TEXT,
    lease_expires_at        TEXT,
    lease_expired           INTEGER NOT NULL DEFAULT 0
                            CHECK (lease_expired IN (0, 1)),
    task_fence              TEXT NOT NULL,
    control_version         INTEGER NOT NULL DEFAULT 0,
    pause_state             TEXT NOT NULL DEFAULT 'active'
                            CHECK (pause_state IN ('active', 'paused')),
    cancellation_state      TEXT NOT NULL DEFAULT 'active'
                            CHECK (cancellation_state IN
                                   ('active', 'requested',
                                    'acknowledged', 'terminal')),
    deadline_at             TEXT,
    deadline_policy         TEXT,
    deadline_expired        INTEGER NOT NULL DEFAULT 0
                            CHECK (deadline_expired IN (0, 1)),
    database_time_watermark TEXT NOT NULL,
    clock_fault             INTEGER NOT NULL DEFAULT 0
                            CHECK (clock_fault IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_run_controls_task
ON run_controls(task_id);
"""


async def _migrate_add_run_controls(db: aiosqlite.Connection) -> None:
    """Add the durable run-control row for the fenced run boundary.

    The row is the live authority for lease ownership, the task fence,
    pause and cancellation states, the durable deadline, and the
    nondecreasing database-time watermark. Every fenced mutation loads
    this row again inside its own transaction.
    """
    await db.executescript(RUN_CONTROLS_DDL)
    db.row_factory = aiosqlite.Row
    await db.commit()
    logger.info("Migration 14 applied: durable run-control rows")


async def _migrate(db: aiosqlite.Connection, version: int) -> None:
    """Dispatch to the migration function for the given version."""
    migrations = {
        2: _migrate_to_v2,
        3: _migrate_to_v3,
        4: _migrate_to_v4,
        5: _migrate_to_v5,
        6: _migrate_to_v6,
        7: _migrate_to_v7,
        8: _migrate_to_v8,
        9: _migrate_to_v9,
        10: _migrate_to_v10,
        11: _migrate_to_v11,
        12: _migrate_to_v12,
        13: _migrate_add_runtime_pair,
        14: _migrate_add_run_controls,
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
            f"Database directory is not writable: {db_dir}. Check volume mount permissions."
        )

    try:
        async with _connect() as db:
            # Run schema DDL (IF NOT EXISTS makes this idempotent)
            await db.executescript(SCHEMA_DDL)

            # executescript commits and may reset connection state,
            # so re-set row_factory for subsequent queries
            db.row_factory = aiosqlite.Row

            # Ensure schema_version row exists
            cursor = await db.execute("SELECT MAX(version) as v FROM schema_version")
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
    task_id: str,
    label: str,
    full_input: str,
    variant: str = "classic",
    runtime_contract_version: str = "1",
) -> None:
    """Create a new task record with status='pending'.

    Always writes the active variant and its exact contract version at
    creation time so the row never sits at the old schema default
    between INSERT and the triage update_task_status call.
    """
    async with _connect() as db:
        await db.execute(
            "INSERT INTO tasks "
            "(id, label, full_input, status, variant, runtime_contract_version)"
            " VALUES (?, ?, ?, 'pending', ?, ?)",
            (task_id, label, full_input, variant, runtime_contract_version),
        )
        await db.commit()


async def create_task_with_meta(
    task_id: str,
    label: str,
    full_input: str,
    variant: str,
    metadata: dict,
    *,
    runtime_contract_version: str,
    run_state: str = "queued",
) -> None:
    """Create a task and its recovery metadata in one transaction.

    The row persists the exact runtime pair before the task can enter
    the queue, so every later boundary reads the stored pair.
    """
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await connection.execute(
                "INSERT INTO tasks "
                "(id, label, full_input, status, variant, "
                "runtime_contract_version, run_state) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (
                    task_id,
                    label,
                    full_input,
                    variant,
                    runtime_contract_version,
                    run_state,
                ),
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
        cursor = await db.execute("SELECT started_at FROM tasks WHERE id = ?", (task_id,))
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
            "terminal_kind = 'completed', "
            "failure_category = NULL, "
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
    failure_category: str = "execution",
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
            "terminal_kind = 'failed', "
            "failure_category = ?, "
            "error_message = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_token = NULL "
            f"WHERE {where}",
            [failure_category, *params],
        )
        await db.commit()
        return cursor.rowcount == 1


async def request_task_cancellation(task_id: str) -> bool:
    """Record a durable cancellation request before execution stops."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE tasks SET run_state = 'cancelling', "
            "cancel_requested_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "last_heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND status IN ('pending','running')",
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def cancel_task(
    task_id: str,
    reason: str,
    lease_token: str | None = None,
) -> bool:
    """Mark one task as cancelled while preserving legacy terminal status."""
    where = "id = ? AND status IN ('pending','running')"
    params: list[Any] = [f"Task cancelled: {reason}", task_id]
    if lease_token is not None:
        where += " AND lease_token = ?"
        params.append(lease_token)
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE tasks SET status = 'failed', run_state = 'cancelled', "
            "terminal_kind = 'cancelled', failure_category = 'cancelled', "
            "error_message = ?, "
            "cancel_requested_at = COALESCE(cancel_requested_at, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
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
        cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        task = dict(row)
        raw_result = task.get("result_json")
        if isinstance(raw_result, str) and raw_result:
            with suppress(json.JSONDecodeError, TypeError):
                result = json.loads(raw_result)
                if isinstance(result, dict) and isinstance(result.get("variant_metrics"), dict):
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
        cursor_clause = "AND (created_at > ? OR (created_at = ? AND id > ?)) "
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
            "((status = 'failed' AND COALESCE(terminal_kind, 'failed') != 'cancelled') "
            "OR COALESCE(run_state, '') IN "
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
    elif status == "cancelled":
        clauses.append("terminal_kind = 'cancelled'")
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
            "SELECT CASE WHEN terminal_kind = 'cancelled' THEN 'cancelled' "
            "ELSE status END AS status, COUNT(*) AS count FROM tasks "
            "WHERE archived_at IS NULL GROUP BY CASE "
            "WHEN terminal_kind = 'cancelled' THEN 'cancelled' ELSE status END"
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
            f"Event payload is {payload_bytes} bytes. The limit is {MAX_EVENT_PAYLOAD_BYTES} bytes."
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
                    "SELECT * FROM event_journal WHERE stream = ? AND idempotency_key = ?",
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

            outbox_cursor = await connection.execute("SELECT COUNT(*) AS count FROM event_outbox")
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
            cursor = await connection.execute("SELECT COUNT(*) AS count FROM event_outbox")
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
            "SELECT * FROM event_journal WHERE stream = ? AND cursor > ? ORDER BY cursor LIMIT ?",
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
            "SELECT * FROM event_journal WHERE stream = ? AND idempotency_key = ?",
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
                "SELECT COUNT(*) AS count, COALESCE(SUM(attempts), 0) AS failures FROM event_outbox"
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
    overloaded = unpublished >= OUTBOX_OVERLOAD_THRESHOLD or outbox_count >= MAX_OUTBOX_BACKLOG
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
                f"UPDATE tasks SET total_cost_usd = ?, total_tokens = ? WHERE {where}",
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


async def get_task_logs(task_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
    """Fetch log entries for a task with pagination (structured fields decoded)."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM log_entries WHERE task_id = ? ORDER BY id LIMIT ? OFFSET ?",
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
            "SELECT * FROM agent_traces WHERE task_id = ? AND turn_id = ? ORDER BY seq",
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


async def get_task_traces(task_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
    """Fetch trace events for a task (paginated), ordered by turn_id + seq."""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM agent_traces WHERE task_id = ? ORDER BY turn_id, seq LIMIT ? OFFSET ?",
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
                task_id,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                phase,
                node_id,
                turn_id,
                provider,
                price_source,
                joules_estimate,
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
    sources = entry.get("sources", [])
    if isinstance(sources, list):
        sources = json.dumps(sources)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, entry["task_id"], lease_token)
            await db.execute(
                "INSERT OR REPLACE INTO board_entries "
                "(id, task_id, type, author, author_node, title, body, refs, "
                "sources, confidence, status, salience, round, space, "
                "created_by_turn, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry["id"],
                    entry["task_id"],
                    entry["type"],
                    entry["author"],
                    entry.get("author_node"),
                    entry.get("title"),
                    entry.get("body"),
                    refs,
                    sources,
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
            for field in ("refs", "sources"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        d[field] = []
                else:
                    d[field] = []
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
                    task_id,
                    seq,
                    round_no,
                    turn_id,
                    actor,
                    event_type,
                    entry_id,
                    payload_str,
                    redis_stream_id,
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
                    raise RuntimeError(f"Board event sequence conflict for {task_id} at {seq}")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def get_board_events(task_id: str, until_seq: int | None = None) -> list[dict]:
    """Fetch board events for a task, ordered by seq (replay).

    If until_seq is provided, returns events up to and including that seq.
    """
    async with _connect() as db:
        if until_seq is not None:
            rows = await db.execute_fetchall(
                "SELECT * FROM board_events WHERE task_id = ? AND seq <= ? ORDER BY seq",
                (task_id, until_seq),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM board_events WHERE task_id = ? ORDER BY seq",
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
                payload_json = payload if isinstance(payload, str) else json.dumps(payload)
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
                sources = entry.get("sources", [])
                sources_json = sources if isinstance(sources, str) else json.dumps(sources)
                await db.execute(
                    "INSERT OR REPLACE INTO board_entries "
                    "(id, task_id, type, author, author_node, title, body, refs, "
                    "sources, confidence, status, salience, round, space, "
                    "created_by_turn, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["id"],
                        task_id,
                        entry["type"],
                        entry["author"],
                        entry.get("author_node"),
                        entry.get("title"),
                        entry.get("body"),
                        refs_json,
                        sources_json,
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
                [(salience, entry_id, task_id) for entry_id, salience in scores.items()],
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


async def patch_board_meta(
    task_id: str,
    patch: dict,
    lease_token: str | None = None,
) -> dict:
    """Merge selected task metadata fields inside one fenced transaction."""
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _assert_task_lease(db, task_id, lease_token)
            cursor = await db.execute(
                "SELECT data FROM board_meta WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
            current: dict = {}
            if row and row["data"]:
                try:
                    decoded = json.loads(row["data"])
                    if isinstance(decoded, dict):
                        current = decoded
                except (json.JSONDecodeError, TypeError):
                    current = {}
            current.update(patch)
            await db.execute(
                "INSERT INTO board_meta (task_id, data, updated_at) "
                "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (task_id, json.dumps(current)),
            )
            await db.commit()
            return current
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
            f"UPDATE tasks SET checkpoint_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE {where}",
            params,
        )
        await connection.commit()
        return cursor.rowcount == 1


async def get_board_meta(task_id: str) -> dict:
    """Read the persisted classic-board control metadata."""
    async with _connect() as db:
        cursor = await db.execute("SELECT data FROM board_meta WHERE task_id = ?", (task_id,))
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
    async with (
        _connect() as db,
        db.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM task_files WHERE task_id = ?",
            (task_id,),
        ) as cur,
    ):
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def get_task_file(file_id: str) -> dict | None:
    """Return a single file row by ID."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM task_files WHERE id = ?", (file_id,)) as cur:
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
            (
                artifact_id,
                task_id,
                turn_id,
                author,
                rel_path,
                stored_path,
                mime,
                size_bytes,
                sha256,
                version,
            ),
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
        async with db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)) as cur:
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
            "SELECT * FROM artifacts WHERE task_id = ? AND rel_path = ? AND version = ?",
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
    async with (
        _connect() as db,
        db.execute(
            "SELECT MAX(version) FROM artifacts WHERE task_id = ? AND rel_path = ?",
            (task_id, rel_path),
        ) as cur,
    ):
        row = await cur.fetchone()
        return row[0] if row and row[0] else 0


async def get_task_artifacts_total_bytes(task_id: str) -> int:
    """Return total bytes of all artifacts for a task (quota enforcement)."""
    async with (
        _connect() as db,
        db.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM artifacts WHERE task_id = ?",
            (task_id,),
        ) as cur,
    ):
        row = await cur.fetchone()
        return row[0] if row else 0


# ── Benchmark and Dataset Registry ──────────────────────────────────


def _decode_json_columns(record: dict, columns: tuple[str, ...]) -> dict:
    """Decode selected JSON columns without failing the complete record."""
    for column in columns:
        value = record.get(column)
        if not isinstance(value, str):
            continue
        with suppress(json.JSONDecodeError, TypeError):
            record[column] = json.loads(value)
    return record


async def create_dataset_version(
    *,
    dataset_id: str,
    version_id: str,
    name: str,
    description: str,
    source_uri: str | None,
    license_name: str | None,
    author: str | None,
    dataset_metadata: dict,
    checksum: str,
    schema: dict,
    source_filename: str,
    source_mime: str,
    source_checksum: str,
    source_path: str,
    version_metadata: dict,
    items: list[dict],
    publish: bool = True,
) -> dict:
    """Create one dataset and one immutable version in one transaction."""
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            existing_cursor = await connection.execute(
                "SELECT id FROM dataset_versions WHERE dataset_id = ? AND checksum = ?",
                (dataset_id, checksum),
            )
            if await existing_cursor.fetchone():
                raise DatasetVersionConflict("This dataset already contains the same content")

            await connection.execute(
                "INSERT INTO datasets "
                "(id, name, description, source_uri, license, author, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "name = excluded.name, "
                "description = CASE WHEN excluded.description <> '' "
                "THEN excluded.description ELSE datasets.description END, "
                "source_uri = COALESCE(excluded.source_uri, datasets.source_uri), "
                "license = COALESCE(excluded.license, datasets.license), "
                "author = COALESCE(excluded.author, datasets.author), "
                "metadata = CASE WHEN excluded.metadata <> '{}' "
                "THEN excluded.metadata ELSE datasets.metadata END, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (
                    dataset_id,
                    name,
                    description,
                    source_uri,
                    license_name,
                    author,
                    json.dumps(dataset_metadata, sort_keys=True),
                ),
            )
            version_cursor = await connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM dataset_versions WHERE dataset_id = ?",
                (dataset_id,),
            )
            version_row = await version_cursor.fetchone()
            version_number = int(version_row["next_version"] if version_row else 1)
            await connection.execute(
                "INSERT INTO dataset_versions "
                "(id, dataset_id, version, status, checksum, item_count, "
                "schema_json, source_filename, source_mime, source_checksum, "
                "source_path, metadata) "
                "VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    dataset_id,
                    version_number,
                    checksum,
                    len(items),
                    json.dumps(schema, sort_keys=True),
                    source_filename,
                    source_mime,
                    source_checksum,
                    source_path,
                    json.dumps(version_metadata, sort_keys=True),
                ),
            )
            await connection.executemany(
                "INSERT INTO dataset_items "
                "(id, dataset_version_id, item_key, input, expected_output, "
                "subject, split, tags, metadata, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["id"],
                        version_id,
                        item["item_key"],
                        item["input"],
                        item["expected_output"],
                        item.get("subject"),
                        item.get("split"),
                        json.dumps(item.get("tags", []), sort_keys=True),
                        json.dumps(item.get("metadata", {}), sort_keys=True),
                        index,
                    )
                    for index, item in enumerate(items)
                ],
            )
            if publish:
                await connection.execute(
                    "UPDATE dataset_versions SET status = 'published', "
                    "published_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE id = ? AND status = 'draft'",
                    (version_id,),
                )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    result = await get_dataset(dataset_id)
    if result is None:
        raise RuntimeError("The dataset disappeared after creation")
    return result


async def list_datasets(
    *,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return one dataset page with its latest version summary."""
    clauses = ["dataset.archived_at IS NULL"]
    params: list[Any] = []
    if search:
        pattern = f"%{search.strip().lower()}%"
        clauses.append(
            "(LOWER(dataset.name) LIKE ? OR LOWER(dataset.description) LIKE ? "
            "OR LOWER(COALESCE(dataset.author, '')) LIKE ?)"
        )
        params.extend((pattern, pattern, pattern))
    where = f"WHERE {' AND '.join(clauses)}"
    bounded_limit = min(max(limit, 1), 200)
    bounded_offset = max(offset, 0)
    async with _connect() as connection:
        count_cursor = await connection.execute(
            f"SELECT COUNT(*) AS count FROM datasets AS dataset {where}",
            params,
        )
        count_row = await count_cursor.fetchone()
        rows = await connection.execute_fetchall(
            "SELECT dataset.*, version.id AS latest_version_id, "
            "version.version AS latest_version, version.status AS latest_status, "
            "version.checksum AS latest_checksum, version.item_count, "
            "version.published_at AS latest_published_at, "
            "(SELECT COUNT(*) FROM dataset_versions AS all_versions "
            "WHERE all_versions.dataset_id = dataset.id) AS version_count "
            "FROM datasets AS dataset "
            "LEFT JOIN dataset_versions AS version ON version.id = ("
            "SELECT candidate.id FROM dataset_versions AS candidate "
            "WHERE candidate.dataset_id = dataset.id "
            "ORDER BY candidate.version DESC LIMIT 1) "
            f"{where} ORDER BY dataset.updated_at DESC, dataset.id LIMIT ? OFFSET ?",
            [*params, bounded_limit, bounded_offset],
        )
    return (
        [_decode_json_columns(dict(row), ("metadata",)) for row in rows],
        int(count_row["count"] if count_row else 0),
    )


async def get_dataset(dataset_id: str) -> dict | None:
    """Return one dataset with all versions and latest field summaries."""
    async with _connect() as connection:
        dataset_cursor = await connection.execute(
            "SELECT * FROM datasets WHERE id = ?",
            (dataset_id,),
        )
        dataset_row = await dataset_cursor.fetchone()
        if not dataset_row:
            return None
        version_rows = await connection.execute_fetchall(
            "SELECT id, dataset_id, version, status, checksum, item_count, "
            "schema_json, source_filename, source_mime, source_checksum, "
            "metadata, created_at, published_at "
            "FROM dataset_versions WHERE dataset_id = ? "
            "ORDER BY version DESC",
            (dataset_id,),
        )
        subject_rows = await connection.execute_fetchall(
            "SELECT item.subject, COUNT(*) AS count FROM dataset_items AS item "
            "JOIN dataset_versions AS version ON version.id = item.dataset_version_id "
            "WHERE version.dataset_id = ? AND version.version = ("
            "SELECT MAX(version) FROM dataset_versions WHERE dataset_id = ?) "
            "GROUP BY item.subject ORDER BY count DESC, item.subject",
            (dataset_id, dataset_id),
        )
        split_rows = await connection.execute_fetchall(
            "SELECT item.split, COUNT(*) AS count FROM dataset_items AS item "
            "JOIN dataset_versions AS version ON version.id = item.dataset_version_id "
            "WHERE version.dataset_id = ? AND version.version = ("
            "SELECT MAX(version) FROM dataset_versions WHERE dataset_id = ?) "
            "GROUP BY item.split ORDER BY count DESC, item.split",
            (dataset_id, dataset_id),
        )
    dataset = _decode_json_columns(dict(dataset_row), ("metadata",))
    dataset["versions"] = [
        _decode_json_columns(dict(row), ("schema_json", "metadata")) for row in version_rows
    ]
    dataset["subjects"] = {
        str(row["subject"] or "Unspecified"): int(row["count"]) for row in subject_rows
    }
    dataset["splits"] = {
        str(row["split"] or "Unspecified"): int(row["count"]) for row in split_rows
    }
    return dataset


async def get_dataset_version_source(
    dataset_id: str,
    version_id: str,
) -> dict | None:
    """Return private source storage data for one dataset version."""
    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT source_path, source_filename, source_mime, source_checksum "
            "FROM dataset_versions WHERE dataset_id = ? AND id = ?",
            (dataset_id, version_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_dataset_items(
    dataset_version_id: str,
    *,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return one searchable page from an immutable dataset version."""
    clauses = ["dataset_version_id = ?"]
    params: list[Any] = [dataset_version_id]
    if search:
        pattern = f"%{search.strip().lower()}%"
        clauses.append(
            "(LOWER(item_key) LIKE ? OR LOWER(input) LIKE ? "
            "OR LOWER(expected_output) LIKE ? OR LOWER(COALESCE(subject, '')) LIKE ?)"
        )
        params.extend((pattern, pattern, pattern, pattern))
    where = f"WHERE {' AND '.join(clauses)}"
    bounded_limit = min(max(limit, 1), 200)
    bounded_offset = max(offset, 0)
    async with _connect() as connection:
        count_cursor = await connection.execute(
            f"SELECT COUNT(*) AS count FROM dataset_items {where}", params
        )
        count_row = await count_cursor.fetchone()
        rows = await connection.execute_fetchall(
            f"SELECT * FROM dataset_items {where} ORDER BY sort_order, id LIMIT ? OFFSET ?",
            [*params, bounded_limit, bounded_offset],
        )
    return (
        [_decode_json_columns(dict(row), ("tags", "metadata")) for row in rows],
        int(count_row["count"] if count_row else 0),
    )


async def get_task_operator_actions(task_id: str, limit: int = 200) -> list[dict]:
    """Return durable operator action requests and their final outcomes."""
    bounded_limit = min(max(limit, 1), 500)
    async with _connect() as connection:
        rows = await connection.execute_fetchall(
            "SELECT cursor, event_type, data, created_at FROM event_journal "
            "WHERE task_id = ? AND event_type IN "
            "('operator_action_requested','operator_action_result') "
            "ORDER BY cursor DESC LIMIT ?",
            (task_id, bounded_limit),
        )
    return [_decode_event_row(row) for row in reversed(rows)]


async def claim_operator_action(
    *,
    action_id: str,
    task_id: str,
    action: str,
    actor: str,
    detail: dict,
) -> tuple[dict, bool]:
    """Claim one operator action once and return any prior action record."""
    detail_json = json.dumps(detail, separators=(",", ":"), sort_keys=True)
    async with _connect() as connection:
        cursor = await connection.execute(
            "INSERT OR IGNORE INTO operator_actions "
            "(action_id, task_id, action, actor, request_detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (action_id, task_id, action, actor, detail_json),
        )
        created = cursor.rowcount == 1
        record_cursor = await connection.execute(
            "SELECT * FROM operator_actions WHERE action_id = ?",
            (action_id,),
        )
        record_row = await record_cursor.fetchone()
        await connection.commit()
    if not record_row:
        raise RuntimeError("The claimed operator action disappeared")
    record = _decode_json_columns(dict(record_row), ("request_detail", "result_detail"))
    if (
        record["task_id"] != task_id
        or record["action"] != action
        or record["actor"] != actor
        or record["request_detail"] != detail
    ):
        raise EventIdempotencyConflict(
            f"Operator action key {action_id!r} identifies different content"
        )
    return record, created


async def finish_operator_action(
    *,
    action_id: str,
    status: str,
    detail: dict,
) -> dict:
    """Save one final operator outcome without replacing an earlier outcome."""
    if status not in {"accepted", "rejected", "failed"}:
        raise ValueError(f"Invalid operator action status: {status}")
    detail_json = json.dumps(detail, separators=(",", ":"), sort_keys=True)
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                "SELECT * FROM operator_actions WHERE action_id = ?",
                (action_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise KeyError(f"Unknown operator action: {action_id}")
            if row["status"] == "requested":
                await connection.execute(
                    "UPDATE operator_actions SET status = ?, result_detail = ?, "
                    "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE action_id = ? AND status = 'requested'",
                    (status, detail_json, action_id),
                )
            elif row["status"] != status or row["result_detail"] != detail_json:
                raise EventIdempotencyConflict(
                    f"Operator action {action_id!r} already has a different outcome"
                )
            result_cursor = await connection.execute(
                "SELECT * FROM operator_actions WHERE action_id = ?",
                (action_id,),
            )
            result_row = await result_cursor.fetchone()
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    if not result_row:
        raise RuntimeError("The operator action outcome disappeared")
    return _decode_json_columns(dict(result_row), ("request_detail", "result_detail"))


async def create_benchmark_attempt(
    *,
    attempt_id: str,
    trial_id: str,
    attempt_number: int,
    task_id: str | None,
    execution_snapshot: dict,
    snapshot_checksum: str,
) -> dict:
    """Create one append-only benchmark attempt for a Trial."""
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await connection.execute(
                "INSERT INTO benchmark_attempts "
                "(id, trial_id, attempt_number, task_id, execution_snapshot, "
                "snapshot_checksum) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    trial_id,
                    attempt_number,
                    task_id,
                    json.dumps(execution_snapshot, sort_keys=True),
                    snapshot_checksum,
                ),
            )
            await connection.execute(
                "UPDATE benchmark_trials SET current_attempt_id = ? WHERE id = ?",
                (attempt_id, trial_id),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return {
        "id": attempt_id,
        "trial_id": trial_id,
        "attempt_number": attempt_number,
        "task_id": task_id,
        "status": "queued",
        "execution_snapshot": execution_snapshot,
        "snapshot_checksum": snapshot_checksum,
    }


# ── Foundation run controls ──────────────────────────────────────────────
# The run-control row is the live authority for lease ownership, the
# task fence, pause and cancellation states, the durable deadline, and
# the nondecreasing database-time watermark. Every function here reads
# and validates the row inside one transaction against database UTC
# time. A forward clock jump can expire a lease or a deadline, and the
# sticky expiry flags keep that decision durable after a later clock
# correction.

CLOCK_TOLERANCE_SECONDS = 2.0

_DB_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


async def _control_now(
    db: aiosqlite.Connection, database_time: str | None,
) -> str:
    """Return the authoritative database UTC time for one transaction."""
    if database_time is not None:
        return database_time
    cursor = await db.execute(f"SELECT {_DB_NOW}")
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("The database clock query returned no row")
    return str(row[0])


def _shifted(timestamp: str, seconds: float) -> str:
    """Return one ISO UTC timestamp moved by a number of seconds."""
    from datetime import datetime, timedelta

    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    moved = parsed + timedelta(seconds=seconds)
    return moved.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def _load_control_row(
    db: aiosqlite.Connection, run_id: str,
) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM run_controls WHERE run_id = ?", (run_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def _advance_watermark(
    db: aiosqlite.Connection, control: dict, now: str,
) -> bool:
    """Advance the watermark or enter clock fault. Returns fault state."""
    if control["clock_fault"]:
        return True
    watermark = str(control["database_time_watermark"])
    if now >= watermark:
        await db.execute(
            "UPDATE run_controls SET database_time_watermark = ? "
            "WHERE run_id = ?",
            (now, control["run_id"]),
        )
        control["database_time_watermark"] = now
        return False
    if _shifted(watermark, -CLOCK_TOLERANCE_SECONDS) > now:
        await db.execute(
            "UPDATE run_controls SET clock_fault = 1 WHERE run_id = ?",
            (control["run_id"],),
        )
        control["clock_fault"] = 1
        return True
    return False


async def create_run_control(
    run_id: str,
    task_id: str,
    task_fence: str,
    *,
    database_time: str | None = None,
) -> None:
    """Create the durable run-control row for one run."""
    async with _connect() as db:
        now = await _control_now(db, database_time)
        await db.execute(
            "INSERT INTO run_controls "
            "(run_id, task_id, task_fence, database_time_watermark) "
            "VALUES (?, ?, ?, ?)",
            (run_id, task_id, task_fence, now),
        )
        await db.commit()


async def get_run_control(run_id: str) -> dict | None:
    """Read the live run-control row."""
    async with _connect() as db:
        return await _load_control_row(db, run_id)


async def acquire_run_lease(
    run_id: str,
    owner: str,
    fence: str,
    ttl_seconds: float,
    *,
    database_time: str | None = None,
) -> bool:
    """Acquire the run lease when no live lease exists.

    The scheduler alone calls this function. Acquisition is denied in
    clock fault and while another owner holds an unexpired lease.
    """
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            control = await _load_control_row(connection, run_id)
            if control is None:
                await connection.commit()
                return False
            now = await _control_now(connection, database_time)
            if await _advance_watermark(connection, control, now):
                await connection.commit()
                return False
            held = (
                control["lease_owner"] is not None
                and not control["lease_expired"]
                and str(control["lease_expires_at"] or "") > now
            )
            if held:
                await connection.commit()
                return False
            expires = _shifted(now, ttl_seconds)
            await connection.execute(
                "UPDATE run_controls SET lease_owner = ?, lease_fence = ?, "
                "lease_acquired_at = ?, lease_renewed_at = ?, "
                "lease_expires_at = ?, lease_expired = 0, "
                "control_version = control_version + 1 WHERE run_id = ?",
                (owner, fence, now, now, expires, run_id),
            )
            await connection.commit()
            return True
        except BaseException:
            await connection.rollback()
            raise


async def renew_run_lease(
    run_id: str,
    owner: str,
    fence: str,
    ttl_seconds: float,
    *,
    database_time: str | None = None,
) -> bool:
    """Renew one held lease for the same owner and fence."""
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            control = await _load_control_row(connection, run_id)
            if control is None:
                await connection.commit()
                return False
            now = await _control_now(connection, database_time)
            if await _advance_watermark(connection, control, now):
                await connection.commit()
                return False
            if (
                control["lease_owner"] != owner
                or control["lease_fence"] != fence
                or control["lease_expired"]
            ):
                await connection.commit()
                return False
            if str(control["lease_expires_at"] or "") <= now:
                await connection.execute(
                    "UPDATE run_controls SET lease_expired = 1 "
                    "WHERE run_id = ?",
                    (run_id,),
                )
                await connection.commit()
                return False
            expires = _shifted(now, ttl_seconds)
            await connection.execute(
                "UPDATE run_controls SET lease_renewed_at = ?, "
                "lease_expires_at = ?, "
                "control_version = control_version + 1 WHERE run_id = ?",
                (now, expires, run_id),
            )
            await connection.commit()
            return True
        except BaseException:
            await connection.rollback()
            raise


async def release_run_lease(run_id: str, owner: str, fence: str) -> bool:
    """Release one held lease for the same owner and fence."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE run_controls SET lease_owner = NULL, lease_fence = NULL, "
            "lease_expires_at = NULL, "
            "control_version = control_version + 1 "
            "WHERE run_id = ? AND lease_owner = ? AND lease_fence = ?",
            (run_id, owner, fence),
        )
        await db.commit()
        return cursor.rowcount == 1


async def check_run_authority(
    run_id: str,
    owner: str | None,
    fence: str | None,
    *,
    deny_paused: bool = False,
    database_time: str | None = None,
) -> dict:
    """Validate the live run authority for one mutation.

    One transaction validates the clock watermark, the lease owner, the
    fence, the lease expiry, the cancellation state, the deadline, and
    optionally the pause state. On success the optimistic control
    version advances and returns with the decision.
    """
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            control = await _load_control_row(connection, run_id)
            if control is None:
                await connection.commit()
                return {"authorized": False, "reason": "unknown_run"}
            now = await _control_now(connection, database_time)
            denial: str | None = None
            if await _advance_watermark(connection, control, now):
                denial = "clock_fault"
            elif owner is None or control["lease_owner"] != owner:
                denial = "lease_owner"
            elif fence is None or control["lease_fence"] != fence:
                denial = "stale_fence"
            elif (
                control["lease_expired"]
                or str(control["lease_expires_at"] or "") <= now
            ):
                await connection.execute(
                    "UPDATE run_controls SET lease_expired = 1 "
                    "WHERE run_id = ?",
                    (run_id,),
                )
                denial = "lease_expired"
            elif control["cancellation_state"] != "active":
                denial = "cancelled"
            elif deny_paused and control["pause_state"] == "paused":
                denial = "paused"
            elif control["deadline_expired"] or (
                control["deadline_at"] is not None
                and str(control["deadline_at"]) <= now
            ):
                await connection.execute(
                    "UPDATE run_controls SET deadline_expired = 1 "
                    "WHERE run_id = ?",
                    (run_id,),
                )
                denial = "deadline"
            if denial is not None:
                await connection.commit()
                return {"authorized": False, "reason": denial}
            await connection.execute(
                "UPDATE run_controls SET "
                "control_version = control_version + 1 WHERE run_id = ?",
                (run_id,),
            )
            await connection.commit()
            return {
                "authorized": True,
                "reason": None,
                "control_version": int(control["control_version"]) + 1,
                "database_time": now,
            }
        except BaseException:
            await connection.rollback()
            raise


async def request_run_cancellation_control(run_id: str) -> bool:
    """Move one active run control to the requested cancellation state."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE run_controls SET cancellation_state = 'requested', "
            "control_version = control_version + 1 "
            "WHERE run_id = ? AND cancellation_state = 'active'",
            (run_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def acknowledge_run_cancellation_control(
    run_id: str, owner: str, fence: str,
) -> bool:
    """Acknowledge one requested cancellation under the live lease."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE run_controls SET cancellation_state = 'acknowledged', "
            "control_version = control_version + 1 "
            "WHERE run_id = ? AND cancellation_state = 'requested' "
            "AND lease_owner = ? AND lease_fence = ?",
            (run_id, owner, fence),
        )
        await db.commit()
        return cursor.rowcount == 1


async def finalize_run_cancellation_control(run_id: str) -> bool:
    """Move one cancellation to its terminal state."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE run_controls SET cancellation_state = 'terminal', "
            "control_version = control_version + 1 "
            "WHERE run_id = ? AND cancellation_state IN "
            "('requested', 'acknowledged')",
            (run_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def set_run_pause_state(run_id: str, paused: bool) -> bool:
    """Set the live pause state of one run control."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE run_controls SET pause_state = ?, "
            "control_version = control_version + 1 WHERE run_id = ?",
            ("paused" if paused else "active", run_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def set_run_deadline(
    run_id: str, deadline_at: str, deadline_policy: str,
) -> bool:
    """Set the durable deadline of one run control."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE run_controls SET deadline_at = ?, deadline_policy = ?, "
            "control_version = control_version + 1 WHERE run_id = ?",
            (deadline_at, deadline_policy, run_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def clear_run_clock_fault(
    run_id: str,
    new_task_fence: str,
    *,
    database_time: str | None = None,
) -> bool:
    """Clear one clock fault after an operator corrects time.

    The operator creates a new task fence, and the watermark restarts
    at the corrected database time.
    """
    async with _connect() as db:
        now = await _control_now(db, database_time)
        cursor = await db.execute(
            "UPDATE run_controls SET clock_fault = 0, task_fence = ?, "
            "database_time_watermark = ?, "
            "control_version = control_version + 1 "
            "WHERE run_id = ? AND clock_fault = 1",
            (new_task_fence, now, run_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def database_utc_now() -> str:
    """Return the authoritative database UTC time."""
    async with _connect() as db:
        return await _control_now(db, None)
