[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Target Architecture](03-target-architecture.md) | [➡️ Next: Control Unit](05-control-unit.md)

# 04 — The Blackboard Protocol (Natural-Language Entries on an Event-Sourced Board)

> [!ABSTRACT]
> This is the core protocol document. It specifies how shared state is structured (typed envelopes around **natural-language bodies**), how agents write to it (an entries response contract through a thin deterministic **Board Gateway**), how the board is event-sourced for replay and live visualization, how concurrency works (append-only + a serialized maintenance path — no CAS machinery needed), and how salience gives the UI and the Cleaner a cheap importance signal. Everything here is additive to the existing dual-write model in `database.py` and `blackboard.py`.

> [!IMPORTANT] Scope guard: what this protocol deliberately does NOT include
> No JSON-Patch mutations, no per-entry JSON Schemas, no role write-contracts at the path level, no compare-and-swap revisions, no `test`-op preconditions, no Architect-generated schemas. All of that belongs to the **PatchBoard variant** ([doc 11](11-variant-patchboard.md)) — a different coordination paradigm optimized for long-horizon stateful tasks and token reduction. The core implements the paper's contract: *agents write natural-language messages to a shared blackboard, and the blackboard is the only communication channel.* Earlier drafts of this document mixed the two; if you see "PatchBoard" anywhere in the core path, it is a bug.

---

## 1. Board entries: typed envelopes, natural-language bodies

The board is a list of **entries** (the paper's "messages"). An entry is the unit agents read and react to. Unlike today's opaque debate strings ([Gap G3](01-gap-analysis.md#4-evidence-the-debate-is-sequential-string-concatenation)), entries carry a light, daemon-stamped envelope — but the **body is free natural language** (markdown). The envelope exists for the UI graph, the Cleaner, and the Control Unit's bookkeeping; it never constrains what the agent can *say*.

```jsonc
// One board entry (canonical shape)
{
  "id": "e-14",                      // stable, gateway-assigned
  "task_id": "task-a8f2",
  "type": "critique",                // see entry types below
  "author": "critic",                // opaque actor id (role, or expert.<domain>)
  "author_node": "node-2",
  "title": "DCF discount rate unjustified",   // short, indexable (agent-supplied)
  "body": "The 8% WACC ignores NVDA's beta of ~1.7. Re-running the DCF at an 11% discount rate cuts the implied upside to ~4%, which…",   // FREE natural language / markdown
  "refs": ["e-12"],                  // entries this one responds to / cites (agent-supplied)
  "confidence": 0.66,                // agent-asserted 0..1 (optional; default 0.5)
  "status": "open",                  // open | superseded | removed (Cleaner) — gateway-managed
  "salience": 0.82,                  // gateway-computed (§7), derived, never agent-set
  "round": 3,                        // blackboard-cycle round that produced it
  "created_by_turn": "turn-7",
  "created_at": "2026-06-10T…Z"
}
```

### Entry types

| Type | Posted by | Meaning | UI shape ([08](08-ui-blackboard-visualization.md)) |
|:--|:--|:--|:--|
| `objective` | Control Unit (genesis) | The task goal | Root node |
| `attachment` | Daemon (genesis, [17 §4](17-files-and-artifacts.md#4-attachments-on-the-board)) | An uploaded file: summary + handle | Paperclip node |
| `plan` | Planner | Decomposition / strategy | Plan node |
| `finding` | Experts | An assertion + evidence + reasoning | Finding node |
| `critique` | Critic | Identifies an error/hallucination in a target entry | Critique edge |
| `rebuttal` | Any | Responds to a critique | Rebuttal edge |
| `conflict` | Conflict-Resolver | Two entries contradict; names the agents | Conflict marker |
| `directive` | Control Unit / operator ([05 §6](05-control-unit.md#6-hitl-during-the-loop)) | Focuses the next round | Directive banner |
| `solution` | Decider | The proposed/final answer | Result node |
| `artifact` | Daemon ([17 §6](17-files-and-artifacts.md#6-artifacts-agent-created-files)) | A file the swarm produced (code, doc, image) | Artifact node |

> [!NOTE]
> `refs` is what turns the relay race into a graph. A `critique` with `refs: ["e-12"]` is a *typed edge* from the critique to finding `e-12`. The live blackboard graph ([08](08-ui-blackboard-visualization.md)) renders entries as nodes and `refs` as edges — no separate graph model needed. `refs` are agent-supplied and validated only for existence (unknown ids are dropped with a warning event, not rejected — agents misremembering an id should not lose their contribution).

> [!NOTE] Why types at all, when the paper's messages are untyped?
> Three consumers need them, none of which is the agents themselves: (a) the **UI** needs node/edge shapes; (b) the **Control Unit's deterministic guards** need to count open critiques and detect a posted `solution` without an LLM call; (c) **authorization** ([§4](#capability-profiles-who-may-write-what)) needs a unit ("only the Decider posts `solution`"). The type vocabulary is fixed and small, the cost to agents is one field in the response envelope, and an agent that omits it gets a sensible default from its role (§3). This is a *light* extension to the paper, not a schema regime.

## 2. Public and private spaces

The paper divides the blackboard into a **public space** (all agents read/write) and **private spaces** (a subset of agents debate or self-reflect without polluting the public board). We map this directly:

- Public: `bmas:board:{task}:entries` — everything in §1.
- Private: `bmas:board:{task}:private:{topic}` — a transient entry list with the same envelope, created when the Conflict-Resolver names conflicting agents ([05 §4](05-control-unit.md#4-private-sub-boards-conflict-resolution)). On resolution, each involved agent posts its reconciled message back to the public space (the paper's contract) and the private space is archived to SQLite, then wiped from Redis.

## 3. The agent response contract

Agents never write to Redis. A turn returns a `TaskResponse` ([06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)) whose payload is **one or more proposed entries**:

```jsonc
// Agent → Daemon: proposed entries for this turn ("entries_v1" contract)
{
  "turn_id": "turn-7",
  "action": "contribute",            // or "decline"
  "entries": [
    {
      "type": "critique",            // optional — defaults from the actor's role (table below)
      "title": "DCF discount rate unjustified",
      "body": "The 8% WACC ignores NVDA's beta of ~1.7…",   // natural language, required
      "refs": ["e-12"],              // optional
      "confidence": 0.66             // optional
    }
  ],
  "trace_id": "trace-turn-7"
}
```

How the Hermes agent produces this, concretely (`agent/api_server.py` owns the extraction):

1. The per-turn instructions (`AGENTS.md` / `instructions`) tell the agent to end its run with a fenced ```` ```json ```` block containing the envelope above. Hermes models do this reliably — it is a *single small JSON object wrapping free text*, not a structured-mutation language. (This is exactly why the core avoids JSON-Patch: emitting valid RFC 6902 against a live document was an open reliability question — old Q3 — that simply disappears here. It remains a gate for the PatchBoard variant, [doc 11 §8](11-variant-patchboard.md#8-open-questions-gates-before-building).)
2. `extract_entries(output)` parses the **last** fenced JSON block. On success → the envelope is forwarded as-is.
3. **Plain-text fallback (never lose work):** if no parseable block exists, the agent server wraps the entire final output as a single entry: `{type: <role default>, title: <first line, truncated 80 chars>, body: <full output>}`. The turn still contributes; the UI flags it with a "free-text" chip; the trace records `envelope_fallback: true`.

Role → default `type`: planner→`plan`, expert.*→`finding`, critic→`critique`, conflict_resolver→`conflict`, decider→`solution`, cleaner→(no default — cleaner uses the maintenance contract below).

**The Cleaner's contract is different** (it mutates, not appends): it returns `{"action": "clean", "remove": ["e-7", "e-9"], "reason": "redundant restatements of e-4"}`. The paper is specific that the cleaner *removes* messages (their ablation shows mark-don't-remove degrades MMLU/GPQA/MATH performance), so removal is honored — but because our board is event-sourced, "removed" entries stay in the event log and SQLite forever (status flips to `removed`, the entry leaves the live snapshot and future prompts, the UI shows it struck-through in replay). Nothing is ever physically destroyed.

## 4. The Board Gateway

`daemon/src/core/gateway.py` (new) is the **only** component that mutates the board. It is deterministic, contains no LLM calls, and is fully unit-testable with an in-memory fake.

```python
# daemon/src/core/gateway.py  (sketch)
class EntryRejected(Exception):
    def __init__(self, reason: str, entry: dict): ...

class BoardGateway:
    """Deterministic write path. Agents propose entries; the gateway disposes."""

    async def append(self, task_id: str, actor: str, capabilities: list[str],
                     proposed: list[dict], turn_id: str) -> list[dict]:
        committed = []
        async with self._task_lock(task_id):                  # §6 — one writer per task
            for raw in proposed:
                try:
                    entry = self._normalize(raw, actor, turn_id)   # defaults, strip reserved fields
                    self._validate_envelope(entry)                 # types/lengths/refs-exist (cheap)
                    self._authorize(capabilities, entry)           # capability check (below)
                    await self._commit(task_id, entry)             # event log + snapshot (§5)
                    committed.append(entry)
                    await self._emit(task_id, "board_entry", entry)        # → SSE
                except EntryRejected as e:
                    await self._log_event(task_id, "entry_rejected",
                                          {"entry": raw, "actor": actor, "reason": e.reason})
                    await self._emit(task_id, "entry_rejected", {...})
            await self._recompute_derived(task_id)             # salience + variant hook (§7)
        return committed

    async def remove(self, task_id, actor, capabilities, ids, reason): ...   # Cleaner path
    async def set_status(self, task_id, entry_id, status, actor): ...        # supersede etc.
    async def set_meta(self, task_id, **fields): ...                          # phase, round, budget
```

Validation is **envelope-only** and intentionally minimal: `type` ∈ the fixed vocabulary, `title` ≤ 200 chars, `body` non-empty and ≤ `board.max_entry_chars` (default 8000 — bigger content belongs in an artifact, [17 §6](17-files-and-artifacts.md#6-artifacts-agent-created-files)), `refs` resolve (unknown ids dropped with a warning), `confidence` clamped to `[0,1]`. The body's *content* is never validated — natural language is the point.

The gateway assigns `id`, `status`, `salience`, `round`, `author`, `author_node`, and timestamps. Agents may not set those fields — attempts are stripped in `_normalize`.

### Rejection is a feature

When an actor proposes an entry of a type its capabilities don't allow (a Critic posting `solution`), or an empty body, the gateway **rejects and emits `entry_rejected`**. This prevents role-confusion from corrupting the board, gives the UI a visible signal ("Critic's solution entry rejected"), and can be fed back to the agent next turn. Expect rejections to be *rare* in the core (the envelope is easy to satisfy) — unlike the PatchBoard variant, where rejection is a load-bearing mechanism.

### Capability profiles (who may write what)

The gateway authorizes by **capability profile**, not hardcoded role names ([seam rule 4](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)). The traditional variant maps roles to profiles; other variants map their actors however they like.

| Capability profile | Traditional role | May post | May remove | May never |
|:--|:--|:--|:--|:--|
| `plan_writer` | Planner | `plan` | — | `solution` |
| `finding_writer` | Experts (and legacy `executor`) | `finding`, `rebuttal` | — | `solution`, others' types |
| `critique_writer` | Critic | `critique` | — | `solution`, `finding` |
| `conflict_mediator` | Conflict-Resolver | `conflict`, `rebuttal` (in private spaces) | — | `solution` |
| `board_maintenance` | Cleaner | — | any non-`objective`/`solution` entry | posting content entries |
| `decision_writer` | Decider, CU | `objective`, `directive`, `solution` | — | — |

Today the persona text *asks* agents to behave ("You are the ONLY agent allowed to write to the Public results namespace" in `personas.py`) but nothing enforces it. The gateway enforces capabilities; the variant decides which actor receives which profile.

> [!NOTE] "Executor" and "auditor" are back-compat aliases
> Han & Zhang have **no "executor" or "auditor" role** — findings come from *experts*, and the auditor's bundled duties (critic + conflict-resolver + cleaner + decider) are separate schedulable roles ([doc 12 §2](12-hermes-and-node-topology.md#2-agents-personas-and-nodes--clearing-up-the-count)). The aliases exist only so current tasks/colors keep rendering; `executor` resolves to `finding_writer`, `auditor` to `critique_writer`. New code uses the paper-faithful names.

## 5. The board as an event log

> [!IMPORTANT]
> **Decision: the board is an append-only log of committed events; the snapshot is a fold over that log.** This single choice gives us replay, crash recovery, and the live-graph animation stream for free — and it is **variant-agnostic** (seam rule 2): the PatchBoard variant logs `patch_committed` events and the stigmergic variant logs `pheromone_*` events into the *same* log shape.

```
Agent returns entries ──► Gateway validates ──► Gateway commits:
                                                 1. append event to bmas:board:{task}:events
                                                    (Redis Stream) + SQLite board_events
                                                 2. apply to snapshot (Redis Hash of entries)
                                                 3. publish SSE event (Pub/Sub → UI)
```

Core event types (traditional variant namespace): `genesis`, `entry_added`, `entry_removed`, `entry_status_changed`, `entry_rejected`, `directive_added`, `phase_changed`, `solution_posted`, `private_opened`, `private_promoted`, `file_added`, `artifact_created`. Each event: `{task_id, seq, round, actor, event_type, entry_id?, payload, ts}`.

### 5.1 Durability and ordering contract

The durable event log is the source of truth for replay, fork, and crash recovery, so Phase 2 must implement and test this contract:

- Every committed or rejected event receives a task-local monotonic `seq` from the gateway before it is emitted.
- SQLite `board_events(task_id, seq)` is the durable recovery source. Redis Streams/Pub-Sub are the live transport and cache.
- A commit is not complete until the SQLite row and Redis live state agree on the same `seq`. If Redis succeeds but SQLite fails, the gateway must retry or mark the task degraded before emitting success; it must not silently continue with an unreplayable board.
- On restart, rebuild Redis snapshots from SQLite `board_events` first, then reconcile or discard stale Redis stream entries whose `seq` is not durably present.
- Property tests must cover interrupted writes and replay determinism: folding events ordered by `seq` reconstructs exactly the same `board_entries` snapshot (including `removed` statuses).

### 5.2 Fork-from-event (counterfactual replay)

The event log supports not just linear replay but **fork**: create a new board timeline starting from event N, with events added, removed, or modified. This enables [counterfactual analysis](15-novelty-and-research-directions.md#35-causality--replay-enabled-by-event-sourcing) ("what if we suppressed agent X's critique?").

- `fork(task_id, at_event_n, mutate_fn=None)` → creates `fork_task_id` with its own event log containing events 1..N (optionally transformed by `mutate_fn`, e.g. dropping all events from one author). The fork's snapshot is materialized by folding.
- The parent log is **immutable** — forks are independent copies (cheap: events are small JSON; a 4-round task has ~20–60 events).
- Forked boards carry `forked_from: {task_id, at_event}` metadata for UI provenance; agents can be re-run against a fork.

> [!NOTE]
> Forks are a Phase 2 deliverable (the data structure and `replay+fork` test). The *UI* for counterfactual exploration is a later/optional extension ([doc 15](15-novelty-and-research-directions.md)); the linear replay scrubber (Phase 4) is built first and is trivial on top of this.

## 6. Concurrency: append-only makes it easy

The paper's algorithm executes the selected agents of a round in sequence; we run them **concurrently across nodes** by default (`coordination.round_execution: concurrent`) because distribution is our headline ([doc 15 §2.1](15-novelty-and-research-directions.md#21-distribution-is-the-headline)). This is safe without optimistic-concurrency machinery because the write model is deliberately narrow:

- **Content writes are pure appends.** Two agents posting findings simultaneously both land; there is no shared entry they could clobber. Entry ids are gateway-minted; ordering is the event `seq`.
- **Mutations (remove/status/meta) flow through one serialized path**: the gateway holds a short per-task lock (`asyncio.Lock` keyed by `task_id`; a Redis lock only if the daemon ever runs multi-process) around the commit + derived-field recompute. With ≤ `max_concurrent_activations` (default 3) writers per task, contention is negligible.
- `coordination.round_execution: sequential` reproduces the paper exactly (each selected agent sees its round-predecessors' messages) — keep it for A/B fidelity experiments ([doc 10 Phase E](10-migration-and-rollout.md#phase-e-cross-cutting--begin-at-phase-1--evaluation-ab--showcase-instrumentation)).

> [!NOTE]
> Per-entry CAS revisions, conflict retry rules, and competing-patch resolution were part of the earlier PatchBoard-flavored draft. They are required only when multiple actors can *mutate the same addressed state in place* — which is the PatchBoard variant's model, and the machinery now lives in [doc 11 §5](11-variant-patchboard.md#5-the-deterministic-kernel). The core never needed it.

## 7. Salience: a cheap, explainable importance signal

Each entry gets a deterministic score the gateway recomputes after each commit (in the shared `recompute_derived` hook — seam rule 5):

```
salience(e) = clamp01( w_c · confidence(e)
                     + w_r · recency(e)            // 1.0 now → decays over rounds
                     + w_x · min(1, refs_in(e)/3)  // how many entries cite/respond to e
                     - w_p · penalty(e) )          // open critiques against e, unrebutted
```

Default weights `w_c=0.4, w_r=0.2, w_x=0.3, w_p=0.3` (configurable under `board.salience_weights` in `bmas.yaml`). Salience is an **extension, not paper machinery** — the paper has no scores. It exists for exactly three consumers:

- **Budgeted board views** ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)) — which bodies stay full when the board exceeds the token budget.
- **Cleaner support** — the Cleaner's prompt includes per-entry salience as a hint; removal decisions remain the LLM's (paper-faithful).
- **UI visual weight** — node opacity/size ([08 §3](08-ui-blackboard-visualization.md#3-the-blackboard-graph)).

The Control Unit does **not** depend on salience for selection — selection is the CU LLM's job ([05 §1](05-control-unit.md#1-the-control-unit-is-a-referee-not-a-brain)). The stigmergic variant later registers its **pressure field** in this same derived-fields hook ([16 §3](16-variant-stigmergic.md#3-the-pressure-field)); nothing in the gateway changes.

## 8. Redis schema v2

Additive to the [existing namespace](../architecture/README.md#namespace-schema). Old keys remain for backward compatibility during migration ([10](10-migration-and-rollout.md)).

| Key | Type | Purpose |
|:--|:--|:--|
| `bmas:board:{task}:entries` | Hash | Snapshot: `entry_id → entry JSON` (live, non-removed) |
| `bmas:board:{task}:events` | Stream | Append-only committed event log (live transport; SQLite is durable truth) |
| `bmas:board:{task}:meta` | Hash | `phase`, `round`, `budget_spent`, `variant`, `decider_state` |
| `bmas:board:{task}:private:{topic}` | Hash | Transient private sub-board (conflict debates) |
| `bmas:board:{task}:salience` | ZSet | `entry_id` scored by salience (fast top-N for budgeted views) |
| `bmas:traces:{task}:{turn}` | Stream | Agent trace events for a turn ([06](06-agent-traces.md)) |
| `bmas:files:{task}` | Hash | Uploaded file metadata ([17](17-files-and-artifacts.md)) |
| `bmas:events:{task}` | Channel | **Existing** — extended with the new event names (§9) |

## 9. New SSE event types (additive)

The existing `routes/events.py` loop forwards any `{event, data}` published to `bmas:events:{task_id}`. We add new event names without touching the loop:

| Event | Payload | Consumed by |
|:--|:--|:--|
| `board_entry` | committed entry (full) | live graph, debate list |
| `entry_removed` / `entry_status_changed` | `{entry_id, by, reason}` | graph (fade-out / strikethrough) |
| `entry_rejected` | `{entry, actor, reason}` | rejection overlay, debug |
| `consensus` | `{decider_state, open_critiques, phase, round}` | convergence meter ([05 §3](05-control-unit.md#3-consensus--termination)) |
| `trace` | trace event ([06](06-agent-traces.md)) | trace inspector, log terminal |
| `turn_start` / `turn_end` | `{turn_id, actor, node, round}` | worker activity lane |
| `file_added` / `artifact_created` | file/artifact metadata ([17](17-files-and-artifacts.md)) | attachments rail, artifact browser |

Backward compatibility: the legacy `debate`, `subtask`, `phase`, `log`, `cost`, `complete` events continue to fire (the daemon emits both during migration), so the current UI keeps working until the new components replace the old tabs.

## 10. Blackboard API surface (replaces ad-hoc methods)

The rewritten `blackboard.py` exposes a small, intention-revealing store API; the gateway is its only writer for board state. Compare with today's `post_debate`/`get_debate`:

```python
class Blackboard:
    async def genesis(task_id, objective, variant, meta) -> None
    async def snapshot(task_id) -> dict             # live entries; folds event log if missing
    async def serialize_for_prompt(task_id, budget_tokens=None) -> dict   # full | budgeted (03 §4)
    async def get_entries(task_id, ids) -> list     # pull-by-id (budgeted-mode overflow)
    async def append_event(task_id, event) -> int   # gateway-only; returns seq
    async def open_private(task_id, topic) -> str
    async def archive_private(task_id, topic) -> None
    async def replay(task_id, until_seq=None) -> list   # ordered events (UI scrubber)
    async def fork(task_id, at_event_n, mutate_fn=None) -> str   # §5.2
```

> [!IMPORTANT]
> Keep the gateway free of LLM calls and unit-test it with an in-memory fake: feed proposed entries, assert committed/rejected/derived. The test suite is what lets you trust three concurrent agents writing to one task — and it is small, because this protocol has no patch dialect, no schemas, and no CAS to test.

➡️ Continue to [05 — Control Unit & Roles](05-control-unit.md).
