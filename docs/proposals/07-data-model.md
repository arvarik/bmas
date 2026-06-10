[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Agent Traces](06-agent-traces.md) | [➡️ Next: UI — Blackboard Visualization](08-ui-blackboard-visualization.md)

# 07 — Data Model: SQLite Migration & Redis v2

> [!ABSTRACT]
> Concrete, additive schema changes. The guiding rule: **additive and reversible.** New tables and columns only; no destructive changes to the existing schema (`database.py` lines 31–118). Bump `SCHEMA_VERSION` and write forward migrations using the migration hook the code already anticipates. The schema is **variant-agnostic** (seam rule 2): the generic `board_events` log serves the traditional core and both future variants.

---

## 1. SQLite migration v2

`database.py` already reserves a migration path:

```194:203:daemon/src/database.py
            if current_version < SCHEMA_VERSION:
                # Run migrations sequentially (currently none — v1 is the first)
                # Future: for v in range(current_version + 1, SCHEMA_VERSION + 1):
                #             await _migrate(db, v)
```

Set `SCHEMA_VERSION = 2` and implement `_migrate(db, 2)` to add the tables below. All new tables `REFERENCES tasks(id) ON DELETE CASCADE` to inherit the existing task-deletion semantics.

### 1.1 `board_entries` — the durable board snapshot

```sql
CREATE TABLE IF NOT EXISTS board_entries (
    id            TEXT PRIMARY KEY,                -- gateway-assigned (e-14)
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,                   -- objective|attachment|plan|finding|critique|rebuttal|conflict|directive|solution|artifact
    author        TEXT NOT NULL,                   -- opaque actor id (role, expert.<slug>, worker.<id>, …)
    author_node   TEXT,
    title         TEXT,
    body          TEXT,                            -- natural language / markdown
    refs          TEXT,                            -- JSON array of entry ids
    confidence    REAL,
    status        TEXT NOT NULL DEFAULT 'open',    -- open|superseded|removed
    salience      REAL DEFAULT 0.0,
    round         INTEGER,
    space         TEXT NOT NULL DEFAULT 'public',  -- public | private:<topic> (archived sub-boards, 04 §2)
    created_by_turn TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_board_entries_task ON board_entries(task_id);
CREATE INDEX IF NOT EXISTS idx_board_entries_salience ON board_entries(task_id, salience DESC);
```

### 1.2 `board_events` — the append-only event log (source of truth)

```sql
CREATE TABLE IF NOT EXISTS board_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,                -- task-local monotonic ordering (04 §5.1)
    round         INTEGER,
    turn_id       TEXT,
    actor         TEXT NOT NULL,                   -- opaque actor id
    event_type    TEXT NOT NULL,                   -- entry_added|entry_removed|entry_status_changed|entry_rejected|
                                                   -- directive_added|phase_changed|solution_posted|private_opened|
                                                   -- private_promoted|file_added|artifact_created|genesis|…
                                                   -- (variants namespace their own: patch_committed, pheromone_decayed, …)
    entry_id      TEXT,                            -- affected entry, if any
    payload       TEXT NOT NULL,                   -- JSON: full event body (entry content, reasons, …)
    redis_stream_id TEXT,                          -- live-stream id if appended
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_board_events_task ON board_events(task_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_board_events_task_seq ON board_events(task_id, seq);
```

> [!NOTE]
> `board_events` stores **everything**, including `entry_rejected` (debugging gold) and Cleaner removals. Folding the events in `seq` order reconstructs `board_entries` exactly, including `removed` statuses ([04 §5.1](04-blackboard-protocol.md#51-durability-and-ordering-contract)); this table is the durable source of truth for replay/fork — inserts here are **not** best-effort like legacy logs. The generic `{actor, event_type, payload}` shape is deliberately variant-agnostic.

### 1.3 `agent_traces` — durable agent activity

```sql
CREATE TABLE IF NOT EXISTS agent_traces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    turn_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL,                   -- opaque actor id
    node          TEXT,
    type          TEXT NOT NULL,                   -- turn_start|reasoning|tool_call|tool_result|token_delta|entries_posted|approval_request|final|error
    data          TEXT,                            -- JSON, type-specific
    model         TEXT,
    tokens_in     INTEGER DEFAULT 0,
    tokens_out    INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0.0,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_traces_task ON agent_traces(task_id, turn_id, seq);
```

### 1.4 `turns` — one row per KS activation

```sql
CREATE TABLE IF NOT EXISTS turns (
    id            TEXT PRIMARY KEY,                -- turn-7
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    round_no      INTEGER NOT NULL,
    role          TEXT NOT NULL,                   -- opaque actor id
    node          TEXT,
    model         TEXT,                            -- the pool-drawn model actually used (05 §2.1)
    status        TEXT NOT NULL DEFAULT 'running', -- running|completed|declined|failed
    entries_added INTEGER DEFAULT 0,
    started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at  TEXT,
    cost_usd      REAL DEFAULT 0.0,
    joules_estimate REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_turns_task ON turns(task_id, round_no);
```

### 1.5 `task_files` — uploaded inputs (doc 17 §3)

```sql
CREATE TABLE IF NOT EXISTS task_files (
    id            TEXT PRIMARY KEY,                -- f-1
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,                   -- original filename (sanitized)
    mime          TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    stored_path   TEXT NOT NULL,                   -- {storage.user_media_dir}/{task_id}/{name}
    extracted_chars INTEGER DEFAULT 0,             -- PDF/text extraction result size
    summary_entry TEXT,                            -- the attachment board entry id
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_task_files_task ON task_files(task_id);
```

### 1.6 `artifacts` — agent-created outputs (doc 17 §6)

```sql
CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,                -- a-3
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    turn_id       TEXT,                            -- which turn produced it
    author        TEXT,                            -- opaque actor id
    rel_path      TEXT NOT NULL,                   -- path inside the task output dir (src/main.py)
    stored_path   TEXT NOT NULL,                   -- {storage.artifacts_dir}/{task_slug}/{rel_path}
    mime          TEXT,
    bytes         INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,      -- bumped when the same rel_path is re-synced
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_task_path_v ON artifacts(task_id, rel_path, version);
```

### 1.7 `tasks` column additions

```sql
ALTER TABLE tasks ADD COLUMN variant          TEXT DEFAULT 'legacy_pipeline';  -- traditional|patchboard|stigmergic|legacy_pipeline
ALTER TABLE tasks ADD COLUMN rounds_used      INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN terminated_by    TEXT;     -- solution|max_rounds|budget|stalled|abort|error
ALTER TABLE tasks ADD COLUMN answer_source    TEXT;     -- decider|sole_vote (05 §3)
ALTER TABLE tasks ADD COLUMN phase            TEXT;     -- Discovery|Debate|Convergence|Solved
ALTER TABLE tasks ADD COLUMN output_dir       TEXT;     -- {storage.artifacts_dir}/{task_slug} (17 §2)
ALTER TABLE tasks ADD COLUMN joules_estimate  REAL DEFAULT 0.0;
```

### 1.8 `cost_entries` column additions

The legacy `cost_entries` table is kept, but Phase 1 needs enough dimensionality for the cost/locality demos:

```sql
ALTER TABLE cost_entries ADD COLUMN node_id        TEXT;
ALTER TABLE cost_entries ADD COLUMN turn_id        TEXT;
ALTER TABLE cost_entries ADD COLUMN provider       TEXT;
ALTER TABLE cost_entries ADD COLUMN price_source   TEXT;  -- bmas.yaml|litellm|manual
ALTER TABLE cost_entries ADD COLUMN joules_estimate REAL DEFAULT 0.0;
```

Hermes returns token counts only. The daemon computes `cost_usd` from model pricing config or LiteLLM response-cost metadata and records the source here ([06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)).

> [!IMPORTANT]
> SQLite `ALTER TABLE … ADD COLUMN` is safe and non-locking for these defaults. Keep `debate_entries`, `sub_tasks`, `cost_entries`, and `log_entries` intact — the migration is purely additive so the current dashboard keeps working through the transition.

## 2. Mapping old concepts to new

| Old table | Fate | Why |
|:--|:--|:--|
| `tasks` | **Kept + extended** | Still the task root. |
| `sub_tasks` | **Kept** | DAG view still valid for legacy tasks; derivable from `plan` entries later. |
| `debate_entries` | **Superseded by `board_entries`** | Keep writing during migration for back-compat; deprecate after UI cutover. |
| `cost_entries` | **Kept, now actually populated** | Real `usage` from traces ([06 §5](06-agent-traces.md#5-transport--persistence)). |
| `log_entries` | **Kept** | Daemon logs still useful; agent activity moves to `agent_traces`. |

During migration the daemon **dual-writes** board→legacy (a `finding` board entry *and* a legacy `debate_entries` row) so both the old Blackboard tab and the new graph render correctly. Remove the legacy write after cutover ([10](10-migration-and-rollout.md)).

## 3. New daemon DB functions

Add to `database.py`, following the existing ephemeral-connection pattern (`_connect()`):

```python
# Board
async def upsert_board_entry(entry: dict) -> None
async def get_board_entries(task_id: str) -> list[dict]
async def insert_board_event(task_id, seq, round, turn_id, actor, event_type, entry_id, payload, redis_stream_id=None) -> None
async def get_board_events(task_id: str, until_seq: int | None = None) -> list[dict]   # ordered replay

# Traces
async def insert_agent_traces(rows: list[dict]) -> None    # batch
async def get_turn_traces(task_id: str, turn_id: str) -> list[dict]
async def get_task_traces(task_id: str, limit: int, offset: int) -> list[dict]

# Turns
async def create_turn(turn: dict) -> None
async def complete_turn(turn_id: str, status: str, entries_added: int, cost_usd: float, joules_estimate: float = 0.0) -> None
async def get_turns(task_id: str) -> list[dict]

# Files & artifacts (doc 17)
async def insert_task_file(row: dict) -> None
async def get_task_files(task_id: str) -> list[dict]
async def insert_artifact(row: dict) -> None
async def get_artifacts(task_id: str) -> list[dict]
```

## 4. New REST endpoints (daemon `routes/`)

Mirror the existing `routes/tasks.py` style; the Next.js proxies follow the [HERMES_API.md proxy pattern](../HERMES_API.md#quick-start-adding-a-new-hermes-proxy-route).

| Method | Path | Returns / does |
|:--|:--|:--|
| `GET` | `/tasks/{id}/board` | current `board_entries` (graph snapshot) |
| `GET` | `/tasks/{id}/board/replay` | ordered `board_events` (replay/scrubber) |
| `GET` | `/tasks/{id}/board/entries?ids=…` | pull-by-id (budgeted-mode overflow; node bearer auth) |
| `GET` | `/tasks/{id}/turns` | `turns` (worker activity lane) |
| `GET` | `/tasks/{id}/turns/{turn}/trace` | `agent_traces` for a turn (trace inspector) |
| `GET` | `/tasks/{id}/trace` | flattened task trace (paginated) |
| `POST` | `/tasks/{id}/files` | upload (multipart, [17 §3](17-files-and-artifacts.md#3-the-upload-path)) |
| `GET` | `/tasks/{id}/files` / `/files/{fid}` | list / download (UI session or node bearer auth) |
| `POST` | `/ingest/traces/{task}/{turn}` | node trace ingest (bearer auth, [06 §5](06-agent-traces.md#5-transport--persistence)) |
| `POST` | `/ingest/artifacts/{task}/{turn}` | node artifact sync (multipart, bearer auth, [17 §6](17-files-and-artifacts.md#6-artifacts-agent-created-files)) |
| `GET` | `/tasks/{id}/artifacts` / `/artifacts/{aid}` | list / download artifacts |
| `GET` | `/capabilities` | available variants + feature flags (drives the UI dropdown, [08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)) |

Corresponding Next.js proxies under `mission-control/src/app/api/tasks/[taskId]/…` (e.g. `board/route.ts`, `turns/route.ts`, `files/route.ts`, `artifacts/route.ts`), each forwarding to the daemon exactly like the existing `cost/route.ts` and `debate/route.ts`.

## 5. Retention & size control

- `agent_traces` is the fastest-growing table. Mitigations: sample/summarize `reasoning`/`token_delta` before archival ([06 §5](06-agent-traces.md#5-transport--persistence)); add a retention job (daemon background task) that prunes traces older than N days while keeping `turns` + `board_*`.
- Redis trace streams are capped (`maxlen`) and TTL'd (24h), matching the existing `bmas:logs:task:{id}` pattern in `blackboard.py` (lines 158–163).
- Files/artifacts live on disk, not in SQLite — the DB stores metadata + hashes only. Disk quotas in [17 §7](17-files-and-artifacts.md#7-limits-security-and-retention).

➡️ Continue to [08 — UI: Blackboard Visualization](08-ui-blackboard-visualization.md).
