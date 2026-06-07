[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Agent Traces](06-agent-traces.md) | [➡️ Next: UI — Blackboard Visualization](08-ui-blackboard-visualization.md)

# 07 — Data Model: SQLite Migration & Redis v2

> [!ABSTRACT]
> Concrete, additive schema changes. The guiding rule: **additive and reversible.** New tables and columns only; no destructive changes to the existing schema (`database.py` lines 31–118). Bump `SCHEMA_VERSION` and write forward migrations using the migration hook the code already anticipates.

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
    id            TEXT PRIMARY KEY,                -- kernel-assigned (e-14)
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,                   -- objective|plan|finding|critique|rebuttal|conflict|directive|consensus
    author        TEXT NOT NULL,
    author_node   TEXT,
    title         TEXT,
    body          TEXT,
    refs          TEXT,                            -- JSON array of entry ids
    confidence    REAL,
    status        TEXT NOT NULL DEFAULT 'open',    -- open|accepted|superseded|retracted
    salience      REAL DEFAULT 0.0,
    rev           INTEGER NOT NULL DEFAULT 1,
    created_by_turn TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_board_entries_task ON board_entries(task_id);
CREATE INDEX IF NOT EXISTS idx_board_entries_salience ON board_entries(task_id, salience DESC);
```

### 1.2 `board_patches` — the append-only event log (source of truth)

```sql
CREATE TABLE IF NOT EXISTS board_patches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    turn_id       TEXT,
    seq           INTEGER NOT NULL,                -- ordering within task
    role          TEXT NOT NULL,
    op            TEXT NOT NULL,                   -- JSON: the RFC 6902 op as committed
    entry_id      TEXT,                            -- affected entry
    accepted      INTEGER NOT NULL DEFAULT 1,      -- 0 = rejected (with reason)
    reason        TEXT,                            -- rejection reason if accepted=0
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_board_patches_task ON board_patches(task_id, seq);
```

> [!NOTE]
> `board_patches` stores **both** accepted and rejected ops. Rejections are valuable debugging data and feed the UI's rejection overlay ([08](08-ui-blackboard-visualization.md)). Replaying only `accepted=1` rows in `seq` order reconstructs `board_entries` exactly.

### 1.3 `agent_traces` — durable agent activity

```sql
CREATE TABLE IF NOT EXISTS agent_traces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    turn_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL,
    node          TEXT,
    type          TEXT NOT NULL,                   -- turn_start|reasoning|tool_call|tool_result|token_delta|patch_proposed|final|error
    data          TEXT,                            -- JSON, type-specific
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
    role          TEXT NOT NULL,
    node          TEXT,
    status        TEXT NOT NULL DEFAULT 'running', -- running|completed|declined|failed
    consensus_after REAL,                          -- consensus score after this turn
    started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at  TEXT,
    cost_usd      REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_turns_task ON turns(task_id, round_no);
```

### 1.5 `tasks` column additions

```sql
ALTER TABLE tasks ADD COLUMN rounds_used      INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN consensus_score  REAL;
ALTER TABLE tasks ADD COLUMN terminated_by    TEXT;     -- consensus|max_rounds|budget|abort|error
ALTER TABLE tasks ADD COLUMN phase            TEXT;     -- Discovery|Debate|Convergence|Verified_Complete
```

> [!IMPORTANT]
> SQLite `ALTER TABLE … ADD COLUMN` is safe and non-locking for these defaults. Keep `debate_entries`, `sub_tasks`, `cost_entries`, and `log_entries` intact — the migration is purely additive so the current dashboard keeps working through the transition.

## 2. Mapping old concepts to new

| Old table | Fate | Why |
|:--|:--|:--|
| `tasks` | **Kept + extended** | Still the task root. |
| `sub_tasks` | **Kept** | DAG view still valid for SIMPLE tasks; can be derived from `plan` entries. |
| `debate_entries` | **Superseded by `board_entries`** | Keep writing during migration for back-compat; deprecate after UI cutover. |
| `cost_entries` | **Kept, now actually populated** | Real `usage` from traces ([06 §5](06-agent-traces.md#5-transport--persistence)). |
| `log_entries` | **Kept** | Daemon logs still useful; agent activity moves to `agent_traces`. |

During migration the daemon **dual-writes** debate→board (a `finding` board entry *and* a legacy `debate_entries` row) so both the old Blackboard tab and the new graph render correctly. Remove the legacy write after cutover ([10](10-migration-and-rollout.md)).

## 3. New daemon DB functions

Add to `database.py`, following the existing ephemeral-connection pattern (`_connect()`):

```python
# Board
async def upsert_board_entry(entry: dict) -> None
async def get_board_entries(task_id: str) -> list[dict]
async def insert_board_patch(task_id, turn_id, seq, role, op, entry_id, accepted, reason) -> None
async def get_board_patches(task_id: str) -> list[dict]   # ordered replay

# Traces
async def insert_agent_traces(rows: list[dict]) -> None    # batch
async def get_turn_traces(task_id: str, turn_id: str) -> list[dict]
async def get_task_traces(task_id: str, limit: int, offset: int) -> list[dict]

# Turns
async def create_turn(turn: dict) -> None
async def complete_turn(turn_id: str, status: str, consensus_after: float, cost_usd: float) -> None
async def get_turns(task_id: str) -> list[dict]
```

## 4. New REST endpoints (daemon `routes/`)

Mirror the existing `routes/tasks.py` style; the Next.js proxies follow the [HERMES_API.md proxy pattern](../HERMES_API.md#quick-start-adding-a-new-hermes-proxy-route).

| Method | Path | Returns |
|:--|:--|:--|
| `GET` | `/tasks/{id}/board` | current `board_entries` (graph snapshot) |
| `GET` | `/tasks/{id}/board/replay` | ordered `board_patches` (replay/scrubber) |
| `GET` | `/tasks/{id}/turns` | `turns` (worker activity lane) |
| `GET` | `/tasks/{id}/turns/{turn}/trace` | `agent_traces` for a turn (trace inspector) |
| `GET` | `/tasks/{id}/trace` | flattened task trace (paginated) |

Corresponding Next.js proxies under `mission-control/src/app/api/tasks/[taskId]/…` (e.g. `board/route.ts`, `turns/route.ts`, `turns/[turnId]/trace/route.ts`), each forwarding to the daemon exactly like the existing `cost/route.ts` and `debate/route.ts`.

## 5. Retention & size control

- `agent_traces` is the fastest-growing table. Mitigations: sample/summarize `reasoning`/`token_delta` before archival ([06 §5](06-agent-traces.md#5-transport--persistence)); add a retention job (reuse Hermes cron or a daemon background task) that prunes traces older than N days while keeping `turns` + `board_*`.
- Redis trace streams are capped (`maxlen`) and TTL'd (24h), matching the existing `bmas:logs:task:{id}` pattern in `blackboard.py` (lines 158–163).

➡️ Continue to [08 — UI: Blackboard Visualization](08-ui-blackboard-visualization.md).
