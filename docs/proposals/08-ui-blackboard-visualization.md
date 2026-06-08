[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Data Model](07-data-model.md) | [➡️ Next: UI — Agent Trace Inspector](09-ui-agent-trace-inspector.md) | [🎨 Design System](../design/DESIGN.md)

# 08 — UI: Live Blackboard Visualization

> [!ABSTRACT]
> The headline feature: a **real-time graph of the blackboard** and a **worker-activity view** showing what every agent is doing, right now, on the shared problem. This document specifies both — reusing the existing React Flow setup, primitives, tokens, and SSE plumbing. **No new shades, no new spacing values.**

> [!IMPORTANT] Read doc 13 first for the density philosophy
> Because this is a **showcase artifact**, these surfaces follow *legible maximalism*, not minimalism — surface as much board/agent state as possible at once. The governing UI philosophy, the dense "Mission" cockpit layout, and performance rules live in [doc 13 — UI Showcase Density](13-ui-showcase-density.md), which **supersedes** the "quiet/clarity-over-density" framing for these surfaces. This document covers the graph mechanics; doc 13 covers how it sits in the dense command center.

> [!IMPORTANT] Prerequisite
> This depends entirely on [06 — Agent Traces](06-agent-traces.md) and the `board_patch`/`trace` SSE events from [04 §8](04-blackboard-protocol.md#8-new-sse-event-types-additive). Build those first.

---

## 1. Design principles applied

The [DESIGN.md §1](../design/DESIGN.md) token system, type scale, spacing, and accessibility rules remain **fully binding**. The *density* principle, however, is amended for showcase surfaces ([doc 13 §1](13-ui-showcase-density.md#1-amending-the-design-contract-deliberately-not-casually)): show all available signal, encode importance via size/color/motion, and use progressive disclosure instead of omission. Concretely for the board graph:

- **Reuse, don't reinvent.** The board graph is an evolution of `DAGVisualizer.tsx`, which already wires `@xyflow/react`, the custom `BmasNode`, `Background`, `Controls`, `MiniMap`, and token-based styling. We add node/edge *types*, not a new rendering stack.
- **Entries are nodes; `refs` are edges.** The board model ([04 §1](04-blackboard-protocol.md#1-board-entries-typed-addressable-versioned)) is already a graph. No translation layer.
- **Status colors are sacred.** Node borders use the existing `STATUS_COLORS`; agent identity uses `AGENT_COLORS` (`design-tokens.ts`). New roles (critic, conflict-resolver, cleaner, decider) need **token additions** — added to `DESIGN.md §2.5` and `design-tokens.ts` *first* (see §7), never inline.

## 2. Where it lives

The existing `/task/[taskId]/blackboard` tab currently renders a flat `DebateList` (`blackboard/page.tsx`). We **split it into two views** behind a segmented control (matching the existing `task-tabs` pattern), preserving the debate list as one mode:

| Mode | Component | Purpose |
|:--|:--|:--|
| **Graph** (new default) | `BlackboardGraph.tsx` | Live entry/ref graph with agents acting on it |
| **Stream** | existing `DebateList` | Chronological entries (kept, now fed by `board_patch`) |

Add a `/task/[taskId]/board` route or a sub-toggle inside the blackboard tab — either is consistent with the App Router layout in [DESIGN.md §6.4](../design/DESIGN.md#64-main-content-area). Recommended: in-tab toggle to avoid a fifth tab.

## 3. The blackboard graph

```
┌─ Panel: "Blackboard" ─────────────────────────────────────── [Graph|Stream] ─┐
│                                                       Consensus ▓▓▓▓▓░░ 0.62  │
│        ┌────────────┐                                                          │
│        │ OBJECTIVE  │  (root, accent-primary)                                  │
│        └─────┬──────┘                                                          │
│         ┌────┴─────┐                                                           │
│         ▼          ▼                                                           │
│   ┌──────────┐ ┌──────────┐      critique (dashed, status-error)              │
│   │ finding  │ │ finding  │◄┄┄┄┄┄┄┄┄┄┄┐                                       │
│   │ e-12 ●   │ │ e-13 ●   │           ┊                                        │
│   │ valuation│ │ supply   │      ┌────┴─────┐                                  │
│   └────┬─────┘ └──────────┘      │ critique │ (critic color)                   │
│        ▼  rebuttal (solid)       │ e-14  ●  │                                  │
│   ┌──────────┐                   └──────────┘                                  │
│   │ rebuttal │                                                                 │
│   └──────────┘     ◆ conflict marker (expandable → private sub-board)          │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Node design (extends `BmasNode`)

Reuse the existing node anatomy from `DAGVisualizer.tsx` (lines 72–130): agent identity dot + label + `StatusBadge`. Add, per entry type:

- **Type glyph** (lucide icon, 14px, `--text-tertiary`): `Target` (objective), `ListTree` (plan), `Lightbulb` (finding), `AlertTriangle` (critique), `MessageSquareReply` (rebuttal), `GitMerge` (conflict), `CheckCircle2` (consensus). Icons only — color stays in the dot/border.
- **Salience → visual weight.** Map `salience` (0..1) to node opacity (0.6→1.0) and a subtle scale (1.0→1.06). High-salience entries literally stand out, implementing the "pheromone" intuition visually. Respect `prefers-reduced-motion` (no scale pulse when reduced).
- **Confidence bar.** A 2px bottom border-fill proportional to `confidence`, in the author's `AGENT_COLORS` value.
- **Live append animation.** When a `board_patch` adds a node, it fades+scales in over 300ms (matches [DESIGN.md §7.3](../design/DESIGN.md#73-real-time-data-transitions) DAG node transition). New edges draw with the existing `animated` dashed-flow style already used for running edges.

### Edge design

| `refs` relationship | Edge style (from existing token system) |
|:--|:--|
| plan → finding (derivation) | solid, `STATUS_COLORS.success` when accepted, else status color |
| critique → finding | **dashed**, `STATUS_COLORS.error`, animated while unrebutted |
| rebuttal → critique | solid, author color |
| conflict marker | double-stroke, `STATUS_COLORS.paused` (amber) |

All edge colors come from `design-tokens.ts`; reuse the `NODE_BORDER`/`STATUS_COLORS` mapping already in `DAGVisualizer.tsx` (lines 47–60).

### Layout

The existing `layoutDag` (depth-by-dependency columns) generalizes: compute depth from `refs` instead of `depends_on`. For cyclic graphs (critique↔rebuttal), break cycles for layout by ranking on `created_at`/`seq` and rendering back-edges as curved. Consider `dagre`/`elkjs` only if hand-layout gets unwieldy — but start by extending `layoutDag` to keep the dependency minimal.

## 4. The worker-activity lane

This is the "see all the workers doing the task" view the request calls out. A horizontal lane **above** the graph (or a right rail), showing each active turn as a live card. Data: `turn_start`/`turn_end` events + live `trace` events ([06 §4](06-agent-traces.md#4-the-bmas-trace-event-schema)).

```
┌─ Workers ─────────────────────────────────────────────────────────────────┐
│ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐                      │
│ │● Critic       │ │● Expert:Valu. │ │○ Cleaner       │                      │
│ │  node-2       │ │  node-1       │ │  idle          │                      │
│ │  ▸ web_search │ │  ▸ reasoning… │ │                │                      │
│ │  324 tok ↑    │ │  1.2k tok ↑   │ │                │                      │
│ │  $0.0011      │ │  $0.0009      │ │                │                      │
│ └───────────────┘ └───────────────┘ └───────────────┘                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

- Each card is a **`MetricCard`-styled** tile ([DESIGN.md §5.3](../design/DESIGN.md#53-metriccard)) with the agent identity dot + role + node, the current activity verb (from the latest `trace` type: "searching", "reasoning", "proposing"), a live token meter (`tabular-nums`, [DESIGN.md §3.3](../design/DESIGN.md#33-numeric-typography)), and running cost.
- **Idle agents** render in the empty/disabled state (muted), so the operator sees the full roster, not just the busy ones — satisfying the state-awareness principle.
- Clicking a card opens that turn's trace in the inspector ([09](09-ui-agent-trace-inspector.md)).

## 5. Private sub-boards and conflicts

A `conflict` entry renders as a `◆` marker node (amber). Clicking it opens a **modal/SplitView** (existing pattern) showing the private sub-board's rebuttal exchange ([05 §4](05-control-unit.md#4-private-sub-boards-conflict-resolution)). On resolution, the marker collapses and the promoted `finding` animates into the main graph. This keeps the public graph quiet (DESIGN principle 4) while making the deliberation inspectable on demand.

## 6. Convergence meter & rejection overlay

- **Convergence meter**: a slim progress bar in the Panel header bound to `consensus` events ([05 §3](05-control-unit.md#3-consensus--termination)). Fill uses `--accent-primary`; reaching `threshold` triggers a single success pulse (matches the task-complete pulse in [DESIGN.md §7.1](../design/DESIGN.md#71-feedback-on-every-action)). The operator *watches convergence happen*.
- **Rejection overlay** (toggle, default off): when a `patch_rejected` event arrives, briefly flash a red outline where the entry would have been + a toast ("Critic patch rejected: missing body"). Invaluable when debugging agent behavior; quiet by default per principle 4.

## 7. Replay / scrubber

Because the board is an event log ([04 §2](04-blackboard-protocol.md#2-the-board-as-an-event-log)), completed tasks get a **timeline scrubber** at the bottom of the graph: drag to fold `board_patches` up to seq N and watch the board assemble itself. This reuses `GET /tasks/{id}/board/replay` ([07 §4](07-data-model.md#4-new-rest-endpoints-daemon-routes)). For running tasks the scrubber sits at "live"; dragging back pauses live-follow (same pattern as the terminal "↓ N new lines" pill in `TaskLogTerminal.tsx`).

## 8. Token & primitive additions (do this first)

Per the design contract, add tokens before using them:

```ts
// design-tokens.ts — extend AgentRole + AGENT_COLORS (and DESIGN.md §2.5)
// NOTE: "executor"/"auditor" are LEGACY aliases (the paper has neither — doc 12 §2 / doc 04 §1).
// They're kept only so existing tasks/colors keep rendering; new roles use the paper-faithful set.
// Per doc 11 §6 this enum is a *display convenience* — back it with a fallback color generator
// (doc 13 §7) so roleless/dynamic authors (e.g. a V2 `universal` actor) still render.
export type AgentRole =
  | "planner" | "executor" | "auditor"
  | "critic" | "conflict_resolver" | "cleaner" | "decider";

export const AGENT_COLORS: Record<AgentRole, string> = {
  planner: "hsl(265, 50%, 60%)",
  executor: "hsl(175, 60%, 45%)",
  auditor: "hsl(32, 80%, 55%)",
  critic: "hsl(350, 60%, 58%)",          // rose — challenge
  conflict_resolver: "hsl(280, 45%, 58%)",// violet — mediation
  cleaner: "hsl(200, 25%, 55%)",          // slate — janitorial, quiet
  decider: "hsl(150, 45%, 50%)",          // green-cyan — judgment
} as const;
```

These hues are chosen muted (per [DESIGN.md §2.5](../design/DESIGN.md#25-agent-identity-colors): "intentionally muted to avoid competing with status colors") and must be mirrored as CSS custom properties in `globals.css` and documented in `DESIGN.md`.

## 9. State matrix (acceptance criteria)

Every new component implements all five states ([DESIGN.md §9](../design/DESIGN.md#9-state-design-matrix)):

| Component | Empty | Loading | Active | Error | Disabled |
|:--|:--|:--|:--|:--|:--|
| BlackboardGraph | "No board state yet — the swarm hasn't posted entries." + `Network` icon | 3 skeleton nodes + faded edges (reuse `Skeleton variant="dag"`) | live graph, animated patches | "Failed to load board" + retry | N/A |
| Worker lane | full roster, all idle | shimmer cards | live activity cards | "Trace stream unavailable" | idle agents muted |
| Convergence meter | 0.0, muted | indeterminate shimmer | filling bar | hidden | N/A |

## 10. Files

| File | Action |
|:--|:--|
| `components/features/BlackboardGraph.tsx` | new (extends DAGVisualizer patterns) |
| `components/features/WorkerLane.tsx` | new (MetricCard-based) |
| `components/features/ConsensusMeter.tsx` | new (Panel-header bar) |
| `app/task/[taskId]/blackboard/page.tsx` | edit — add Graph/Stream toggle |
| `hooks/useTaskStream.ts` | edit — handle `board_patch`, `consensus`, `turn_start/end`, `patch_rejected` |
| `lib/design-tokens.ts`, `app/globals.css`, `docs/design/DESIGN.md` | edit — new agent role tokens |
| `app/api/tasks/[taskId]/board/route.ts`, `…/turns/route.ts` | new proxies |

➡️ Continue to [09 — UI: Agent Trace Inspector](09-ui-agent-trace-inspector.md).
