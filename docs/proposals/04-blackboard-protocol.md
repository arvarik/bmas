[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Target Architecture](03-target-architecture.md) | [➡️ Next: Control Unit](05-control-unit.md)

# 04 — The Blackboard Protocol (PatchBoard)

> [!ABSTRACT]
> This is the flagship document. It specifies how shared state is structured, how agents mutate it safely (JSON Patch through a deterministic kernel), how the board is event-sourced for replay and live visualization, how concurrent writers are reconciled, and how salience replaces ad-hoc pheromones. Everything here is additive to the existing dual-write model in `database.py` and `blackboard.py`.

---

## 1. Board entries: typed, addressable, versioned

The board is a set of **entries**. An entry is the unit agents read and react to. Unlike today's opaque debate strings ([Gap G3](01-gap-analysis.md#4-evidence-the-debate-is-sequential-string-concatenation)), entries are typed and addressable.

```jsonc
// One board entry (canonical shape)
{
  "id": "e-14",                      // stable, kernel-assigned
  "task_id": "task-a8f2",
  "type": "finding",                 // see entry types below
  "author": "expert.valuation",      // role or expert identity
  "author_node": "node-2",
  "title": "DCF implies 18% upside", // short, indexable
  "body": "…structured content…",    // markdown / structured per type
  "refs": ["e-12"],                  // entries this one responds to / cites
  "confidence": 0.74,                // agent-asserted 0..1
  "status": "open",                  // open | accepted | superseded | retracted
  "salience": 0.82,                  // kernel-computed (see §6)
  "rev": 3,                          // version, for optimistic concurrency (§5)
  "created_at": "2026-06-06T…Z",
  "updated_at": "2026-06-06T…Z",
  "created_by_turn": "turn-5"
}
```

### Entry types

| Type | Posted by | Meaning | UI shape |
|:--|:--|:--|:--|
| `objective` | Control Unit | The task goal + consensus threshold | Root node |
| `plan` | Planner | Decomposition / strategy | Plan node |
| `finding` | Executor / Expert | An assertion + evidence | Finding node |
| `critique` | Critic | Identifies an error/hallucination in a target entry | Critique edge |
| `rebuttal` | Any | Responds to a critique | Rebuttal edge |
| `conflict` | Conflict-Resolver | Two entries contradict | Conflict marker |
| `directive` | Control Unit | Focuses the next round | Directive banner |
| `consensus` | Decider | The convergent answer | Result node |

> [!NOTE]
> `refs` is what turns the relay race into a graph. A `critique` with `refs: ["e-12"]` is a *typed edge* from the critique to finding `e-12`. The live blackboard graph ([08](08-ui-blackboard-visualization.md)) renders entries as nodes and `refs` as edges — no separate graph model needed.

## 2. The board as an event log

> [!IMPORTANT]
> **Decision: the board is an append-only log of committed patches; the snapshot is a fold over that log.** This single choice gives us replay, crash recovery, and the live-graph animation stream for free. It extends — does not replace — the existing dual-write pattern.

```
Agent proposes Patch ──► Kernel validates ──► Kernel commits:
                                               1. append to patch log (Redis Stream + SQLite)
                                               2. apply to snapshot hash (Redis)
                                               3. publish "board_patch" event (Pub/Sub → SSE)
```

- **Patch log** is the truth. `bmas:board:{task_id}:log` (Redis Stream, mirrored to SQLite `board_patches`).
- **Snapshot** is a cache. `bmas:board:{task_id}:entries` (Redis Hash of `entry_id → JSON`). Rebuildable by replaying the log.
- **Crash recovery**: on restart, if the snapshot is missing/stale, fold the log. This replaces today's brittle "zombie task → failed" recovery for board state.

### 2.1 Fork-from-event (counterfactual replay)

The event log supports not just linear replay but **fork**: create a new board timeline starting from event N, with one or more events added, removed, or modified. This enables [counterfactual analysis](15-novelty-and-research-directions.md#35-causality--replay-enabled-by-event-sourcing) ("what if we suppressed agent X's critique?").

Fork semantics:

- `fork(task_id, at_event_n)` → creates a new `fork_task_id` with its own patch log, initially containing events 1..N copied from the parent. The fork's snapshot is materialized by folding those N events.
- The parent log is **immutable** — a fork never modifies the original. Forks are independent copies (cheap: events are small JSON; a 4-round task has ~20–40 events).
- Forked boards get a `forked_from: {task_id, at_event}` metadata field, enabling the UI to show provenance.
- Mutations after the fork point are written only to the fork's log. The fork is a fully functional board — agents can be re-run against it.
- A `mutate_fn` (optional) can modify the replayed events before materialization — e.g., dropping all events from a specific author to test "what if this agent never acted?"

> [!NOTE]
> Forks are a Phase 2 deliverable (the data structure and `replay+fork` test). The *UI* for counterfactual exploration is a later/optional extension ([doc 15](15-novelty-and-research-directions.md)); the linear replay scrubber (Phase 4) is built first and is trivial on top of this.

## 3. JSON Patch mutations (RFC 6902)

Agents never write entries directly. They return a list of **proposed operations** scoped to allowed paths. The kernel is the only writer.

```jsonc
// Agent → Daemon: proposed mutations for this turn
{
  "turn_id": "turn-7",
  "role": "critic",
  "patches": [
    { "op": "add", "path": "/entries/-", "value": {
        "type": "critique",
        "title": "DCF discount rate unjustified",
        "body": "The 8% WACC ignores NVDA's beta of ~1.7…",
        "refs": ["e-12"],
        "confidence": 0.66
    }},
    { "op": "replace", "path": "/entries/e-12/status", "value": "open" }  // may be rejected (see §4)
  ],
  "trace_id": "trace-turn-7",   // links to the agent trace (doc 06)
  "action": "contribute"        // or "decline"
}
```

The agent supplies *intent*; the kernel assigns `id`, `rev`, `author`, `salience`, and timestamps. Agents may not set those fields — attempts are stripped.

## 4. The deterministic kernel

`daemon/src/core/kernel.py` (new) is the **only** component that mutates the board. It is pure, synchronous-in-spirit, and fully testable without an LLM.

```python
# daemon/src/core/kernel.py  (sketch)
from jsonschema import validate, ValidationError
import jsonpatch

class PatchRejected(Exception):
    def __init__(self, reason: str, op: dict): ...

class BoardKernel:
    """Deterministic gatekeeper. LLMs propose; the kernel disposes."""

    async def apply(self, task_id: str, proposal: dict) -> list[dict]:
        committed = []
        for op in proposal["patches"]:
            try:
                entry_op = self._normalize(op, author=proposal["role"])
                self._authorize(proposal["role"], entry_op)      # role can write this type/path?
                self._validate_schema(entry_op)                  # JSON Schema per entry type
                applied = await self._commit_cas(task_id, entry_op)  # optimistic concurrency (§5)
                committed.append(applied)
                await self._emit(task_id, "board_patch", applied)    # → SSE
            except (PatchRejected, ValidationError) as e:
                await self._emit(task_id, "patch_rejected", {"op": op, "reason": str(e)})
                # rejection is itself an observable event — surfaced in the UI
        await self._recompute_salience(task_id)
        return committed
```

### Rejection is a feature

When an agent proposes a malformed `finding` (missing `body`), writes to a path its role may not touch (a Critic trying to publish `consensus`), or violates the schema, the kernel **rejects the op and emits `patch_rejected`**. This:

- prevents hallucinated/garbage state from ever entering the board;
- gives the UI a visible signal ("Critic's malformed patch rejected") — a debugging goldmine;
- can be fed back to the agent next turn as a correction ("your last patch was rejected because …").

### Authorization matrix (who may write what)

| Role | May `add` | May `replace` status of | May never |
|:--|:--|:--|:--|
| Planner | `plan` | own `plan` | `consensus` |
| Executor / Expert | `finding`, `rebuttal` | own entries | others' entries, `consensus` |
| Critic | `critique` | — | `finding` bodies |
| Conflict-Resolver | `conflict` | `conflict` resolution | `finding` bodies |
| Cleaner | — | `status` → `superseded`/`retracted` (low salience only) | entry bodies, `consensus` |
| Control Unit / Decider | `objective`, `directive`, `consensus` | phase, thresholds | — |

This matrix is the structural enforcement of the paper's role separation. Today the persona text *asks* agents to behave ("You are the ONLY agent allowed to write to the Public results namespace" in `personas.py`) but nothing enforces it. The kernel enforces it.

## 5. Optimistic concurrency

To allow concurrent writers ([Gap G4](01-gap-analysis.md#6-evidence-strictly-single-task-single-writer-concurrency)) without a global lock, mutations to an *existing* entry use compare-and-swap on `rev`:

```lua
-- commit_cas.lua  (atomic): only apply if rev matches
local cur = redis.call("HGET", KEYS[1], ARGV[1])      -- entries hash, entry_id
if not cur then return redis.error_reply("NOT_FOUND") end
local entry = cjson.decode(cur)
if entry.rev ~= tonumber(ARGV[2]) then                -- ARGV[2] = expected rev
  return redis.error_reply("CONFLICT")
end
-- merge patched fields (ARGV[3] = json of changed fields), bump rev
...
redis.call("HSET", KEYS[1], ARGV[1], cjson.encode(merged))
return merged.rev
```

- **`add` of a new entry**: never conflicts (kernel assigns a fresh id). Concurrent findings from different agents both land.
- **`replace`/`remove` of an existing entry**: CAS on `rev`. On `CONFLICT`, the kernel re-reads, re-checks the op's precondition, and either retries or rejects with `patch_rejected` (reason: stale). Bounded retries (e.g. 3).

This serializes only true contention on the *same* entry, exactly the blackboard concurrency model the external review asked for.

### 5.1 Conflict resolution policy (V1)

When CAS fails after retries, the kernel emits a `patch_rejected` event with `reason: "conflict"` and the stale `rev`. **The losing patch is not silently dropped or auto-queued** — it becomes a visible event in the trace stream and the UI. The `ControlUnitStrategy` then decides whether to re-schedule the rejected agent for another attempt (with the updated board state) in a subsequent round, or to move on. This keeps conflict resolution in the strategy layer (consistent with the [CoordinationStrategy seam](11-extensibility-and-variants.md#2-the-seam-coordinationstrategy)) and avoids baking retry-or-drop policy into the kernel itself. In V2 (stigmergic), the rejected agent simply observes the updated pressure field and self-activates if the region is still high-pressure — no CU arbitration needed.

## 6. Salience: the pragmatic pheromone

Rather than tunable pheromone decay ([Peer Review §2.5](02-peer-review.md#25-pheromone-decay--sbp-suggestion-5--adapt-now-defer-the-full-version)), each entry gets a deterministic, explainable score the kernel recomputes after each commit:

```
salience(e) = w_c · confidence(e)
            + w_r · recency(e)            // 1.0 now → decays over rounds
            + w_x · min(1, refs_in(e)/3)  // how many entries cite/respond to e
            - w_p · penalty(e)            // critiques against e that are unrebutted
```

Default weights `w_c=0.4, w_r=0.2, w_x=0.3, w_p=0.3` (configurable in `bmas.yaml`). Salience drives:

- **Control Unit prioritization** — orient toward high-salience conflicts/critiques.
- **Cleaner pruning** — entries below `salience_floor` for N rounds get `status: superseded`.
- **Board index ordering** — agents see the most salient entries first ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)).
- **UI node sizing/opacity** — high-salience nodes render larger/brighter ([08](08-ui-blackboard-visualization.md)).

> [!NOTE] Upgrade path to true SBP
> `recency` is already a decay term. The opt-in SBP module replaces it with continuous time-based exponential decay and adds per-role activation thresholds. Because salience is centralized in the kernel, this is a localized change, not a refactor.

### 6.1 Salience vs. pressure (the V2 seam)

Salience measures *entry importance*. The pure-stigmergic variant ([doc 11](11-extensibility-and-variants.md)) needs a complementary, region-level signal — **pressure** = *how much unfinished work exists in a neighborhood* (unrebutted critiques + open conflicts + low-confidence findings + unmet constraints − reinforcement). Build the pressure field in V1 too: it is computed in the **same kernel hook** that recomputes salience, stored in `bmas:board:{task}:pressure` (ZSet), and consumed by the V1 Control Unit's "orient" step *and* the UI heatmap. Making pressure first-class now is what lets the roleless V2 ("agents act on high-pressure regions") drop in later without touching the kernel. Spec: [doc 11 §3](11-extensibility-and-variants.md#3-the-pressure-field-generalizes-salience).

## 7. Redis schema v2

Additive to the [existing namespace](../architecture/README.md#namespace-schema). Old keys remain for backward compatibility during migration ([10](10-migration-and-rollout.md)).

| Key | Type | Purpose |
|:--|:--|:--|
| `bmas:board:{task}:entries` | Hash | Snapshot: `entry_id → entry JSON` |
| `bmas:board:{task}:log` | Stream | Append-only committed patch log (source of truth) |
| `bmas:board:{task}:meta` | Hash | `phase`, `round`, `consensus_score`, `budget_spent`, `threshold` |
| `bmas:board:{task}:private:{topic}` | Hash | Transient private sub-board (conflict debates) |
| `bmas:board:{task}:salience` | ZSet | `entry_id` scored by salience (fast top-N for the index) |
| `bmas:traces:{task}:{turn}` | Stream | Agent trace events for a turn (doc 06) |
| `bmas:events:{task}` | Channel | **Existing** — extended with `board_patch`, `patch_rejected`, `consensus`, `trace` events |

## 8. New SSE event types (additive)

The existing `routes/events.py` loop forwards any `{event, data}` published to `bmas:events:{task_id}`. We add new event names without touching the loop:

| Event | Payload | Consumed by |
|:--|:--|:--|
| `board_patch` | committed entry (full) | live graph, debate list |
| `patch_rejected` | `{op, reason}` | live graph (rejection flash), debug overlay |
| `consensus` | `{score, threshold, phase}` | convergence meter, phase indicator |
| `trace` | trace event (doc 06) | trace inspector, log terminal |
| `turn_start` / `turn_end` | `{turn_id, role, node}` | worker activity view |

Backward compatibility: the legacy `debate`, `subtask`, `phase`, `log`, `cost`, `complete` events continue to fire (the daemon can emit both during migration), so the current UI keeps working until the new components replace the old tabs.

## 9. Blackboard API surface (replaces ad-hoc methods)

The rewritten `blackboard.py` exposes a small, intention-revealing API. Compare with today's `post_debate`/`get_debate`:

```python
class Blackboard:
    async def genesis(task_id, objective, threshold, weights) -> None
    async def snapshot(task_id) -> dict            # folds log if snapshot missing
    async def index(task_id, top_n=20) -> list     # salient table-of-contents for agents
    async def get_entries(task_id, ids) -> list     # agent pulls specific entries by id
    async def commit(task_id, proposal) -> list      # delegates to BoardKernel
    async def open_private(task_id, topic) -> str    # transient sub-board
    async def promote(task_id, private_topic, entry) # private → public
    async def replay(task_id) -> list                # full ordered patch log (UI/debug)
```

> [!IMPORTANT]
> Keep the kernel free of LLM calls and Redis-version-specific surprises. It must be unit-testable with an in-memory fake: feed proposals, assert committed/rejected. This is what makes the "deterministic" claim real — and it is the test suite that lets you trust concurrent agents.

➡️ Continue to [05 — Control Unit & Roles](05-control-unit.md).
