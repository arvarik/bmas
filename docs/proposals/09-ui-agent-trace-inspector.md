[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ UI: Blackboard Visualization](08-ui-blackboard-visualization.md) | [➡️ Next: Migration & Rollout](10-migration-and-rollout.md) | [🎨 Design System](../design/DESIGN.md)

# 09 — UI: Agent Trace Inspector

> [!ABSTRACT]
> Where the operator answers "what is this agent actually *doing*?" The trace inspector turns the new trace stream ([06](06-agent-traces.md)) into a legible, per-turn timeline of reasoning, tool calls, and token/cost flow — woven into the existing Logs tab and the worker lane, in the [DESIGN.md](../design/DESIGN.md) token language. The trace surfaces are **variant-agnostic** (traces look the same whether the turn proposed entries, patches, or pheromone deposits) — only the "Resulted in" footer is adapter-supplied.

> [!NOTE] Density philosophy
> These are showcase surfaces — the goal is to expose each agent's *full thought process*, densely but legibly. The governing philosophy (legible maximalism, parallel swimlanes for concurrency, per-span chips, virtualization for snappiness, inspiration from LangSmith/Langfuse/Phoenix/AgentOps/Weave) is in [doc 13](13-ui-showcase-density.md).

---

## 1. Two surfaces, one data source

Both consume `agent_traces` / live `trace` events ([06 §4](06-agent-traces.md#4-the-bmas-trace-event-schema)). No new data pipeline.

| Surface | Where | When to use |
|:--|:--|:--|
| **Trace Timeline** | the Logs tab, upgraded | the operator's default "what happened" view |
| **Turn Inspector** | a slide-over panel opened from a graph node or worker card | drill into one agent's single turn |

## 2. Upgrading the Logs tab

Today `/task/[taskId]/logs` renders `TaskLogTerminal` xterm panes fed by sparse daemon logs ([Gap G5](01-gap-analysis.md#7-the-silent-observability-failure-root-cause-for-the-ui-work)). With real traces, keep the terminal as a **"Raw"** mode and add a structured **"Trace"** mode (toggle, same pattern as [08 §2](08-ui-blackboard-visualization.md#2-where-it-lives)):

- **Raw mode**: the existing xterm terminals, now actually full of agent output (`reasoning` lines stream in). The `TaskLogTerminal` ANSI formatting is reused as-is.
- **Trace mode**: a structured, collapsible timeline (below). Better for tool-call-heavy turns where raw text is noisy.

## 3. The trace timeline

Grouped by **turn**, newest turn pinned but scrollable — reusing the smart-auto-scroll + "↓ N new" pill from `blackboard/page.tsx` and `TaskLogTerminal.tsx`.

```
┌─ Trace ──────────────────────────────────────────────────── [Trace|Raw] ─┐
│ ▾ turn-7 · Critic · node-2 · Debate · R3        642 tok · $0.0011 · 3.4s   │
│   │ ● 19:14:02  reasoning   "The DCF discount rate of 8% seems low…"        │
│   │ ▸ 19:14:03  tool_call   web_search("NVDA beta 2026")           [card]   │
│   │   └ result  3 results · finance.yahoo.com, …                  [expand]  │
│   │ ● 19:14:06  reasoning   "Beta ~1.7 implies WACC closer to 11%…"         │
│   │ ◆ 19:14:08  entries     posted 1 critique → e-12                         │
│   └ ✓ 19:14:08  final       "Posted critique; confidence 0.66"              │
│ ▸ turn-6 · Expert:Valuation · node-1 · Discovery   1.2k tok · $0.0019       │
│ ▸ turn-5 · Planner · node-1 · Discovery            …                         │
└────────────────────────────────────────────────────────────────────────────┘
```

### Trace line anatomy (composed from primitives)

- **Gutter glyph + color** by `type`: `reasoning` ● (author color), `tool_call`/`tool_result` ▸ (`--text-secondary`), `entries_posted` ◆ (`--accent-primary`), `approval_request` ⏸ (`--status-paused`), `final` ✓ (`STATUS_COLORS.success`), `error` ✕ (`STATUS_COLORS.error`).
- **Timestamp**: `--text-tertiary`, `--text-mono`, `tabular-nums`.
- **Body**: `--text-sm`. `reasoning` text renders inline (truncated with "Show more"); long bodies use the existing markdown/`Show more` toggle from `DebateList`.
- **Turn header**: agent identity dot + role + node + round + phase `StatusBadge` + right-aligned token/cost/duration (`tabular-nums`). A "free-text" chip when `envelope_fallback: true` ([04 §3](04-blackboard-protocol.md#3-the-agent-response-contract)).

### Tool-call cards

A `tool_call` + its `tool_result` collapse into one card (default collapsed):

```
┌ ▸ web_search ───────────────────────────────── ✓ 3 results · 0.4s ┐
│  args:   { "query": "NVDA beta 2026" }                              │
│  result: finance.yahoo.com — "Beta (5Y): 1.71" …          [open ↗] │
└─────────────────────────────────────────────────────────────────────┘
```

- Card background `--surface-overlay`, radius `--radius-md`, border `--border-default` (the "interactive container" exception in [DESIGN.md §2.1](../design/DESIGN.md#21-surface-hierarchy-backgrounds)).
- `result.ok=false` → red left accent (`--status-error`), expanded by default.
- `artifact_ref` (scraped page, code output, synced file) opens via the existing resource-open pattern, the artifact browser ([17 §8](17-files-and-artifacts.md#8-ui-surfaces)), or a modal.

> [!NOTE] This is where evidence becomes challengeable
> Tool-call cards surface the *sources* behind a finding — exactly the [agent-integration roadmap](../roadmap/agent-integration.md) goal. When a finding's tool-calls are visible, the Critic's critique of those sources becomes legible to the operator too.

## 4. Turn Inspector (slide-over)

Opened by clicking a board node ([08](08-ui-blackboard-visualization.md)) or a worker card. A right-side slide-over (reuse Toast/modal motion tokens, slide-in 200ms ease-out) showing **one turn** in full:

- Header: role, node, round, phase, status, model used, total tokens/cost/duration.
- The complete trace timeline for that turn (no truncation).
- **"Resulted in"** footer (adapter-supplied per variant, [08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)): for the traditional variant — the board entries this turn created (links into the graph), any `entry_rejected` with reason, any artifacts produced (links into the browser, [17 §8](17-files-and-artifacts.md#8-ui-surfaces)). This closes the correlation loop from [06 §6](06-agent-traces.md#6-correlation-traces--board--turns).
- **Inline Approve/Deny** when the turn is paused on an `approval_request` ([12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals), Phase 5).

## 5. Cost integration

The Cost tab (`/task/[taskId]/cost`) finally has real data ([06 §5](06-agent-traces.md#5-transport--persistence)). Additions, all reusing `MetricCard` + Recharts already present:

- **Per-turn cost** breakdown (new), alongside existing per-model/per-phase — plus per-node (the cost/locality demo needs it, [doc 15 §5](15-novelty-and-research-directions.md#5-concrete-demos--experiments-to-build-ranked-by-wow-per-effort)).
- **Budget gauge**: `budget_spent / budget_ceiling_usd` ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)) — a `MetricCard` with a progress ring; amber near the ceiling (`--status-paused`), red at the ceiling.
- The top-bar live cost ticker (`TopBar.tsx`) now reflects genuine spend per turn via the `cost`/`trace` events.

## 6. Live typing indicator (reuse, don't rebuild)

The Stream/DebateList already has a phase-aware typing indicator ("Planner is deliberating…", `blackboard/page.tsx` lines 199–219). Generalize its `PHASE_MAP` to opaque actors (any author string, fallback color) and drive it from live `trace` activity (`reasoning` → "thinking", `tool_call` → "searching"), so the indicator reflects *actual* agent activity rather than a coarse phase guess.

## 7. State matrix

| Component | Empty | Loading | Active | Error | Disabled |
|:--|:--|:--|:--|:--|:--|
| Trace timeline | "No agent activity recorded for this task." + `Activity` icon | skeleton turn rows | streaming trace lines, auto-scroll | "Trace stream unavailable — showing raw logs" (fall back to Raw) | N/A |
| Tool-call card | — | "running…" with pulse | collapsed/expandable | red accent, expanded | N/A |
| Turn Inspector | "Select a turn to inspect." | skeleton | full turn detail | "Failed to load turn" + retry | N/A |
| Budget gauge | 0% of ceiling | shimmer | live fill | last-known, grayed | N/A |

## 8. Files

| File | Action |
|:--|:--|
| `components/features/AgentTrace.tsx` | new — the trace timeline |
| `components/features/ToolCallCard.tsx` | new |
| `components/features/TurnInspector.tsx` | new — slide-over |
| `app/task/[taskId]/logs/page.tsx` | edit — add Trace/Raw toggle |
| `app/task/[taskId]/cost/page.tsx` | edit — per-turn/per-node + budget gauge |
| `hooks/useTaskStream.ts` | edit — accumulate `trace` events into `traces[]` by turn |
| `app/api/tasks/[taskId]/turns/[turnId]/trace/route.ts`, `…/trace/route.ts` | new proxies |

> [!IMPORTANT]
> `useTaskStream` is the one hot path. Buffer high-frequency `trace`/`token_delta` events (coalesce with `requestAnimationFrame` or a short debounce) before `setState`, or a token-streaming turn will cause hundreds of re-renders/sec. This mirrors the smart-scroll batching already used in the terminal.

➡️ Continue to [10 — Migration & Rollout](10-migration-and-rollout.md).
