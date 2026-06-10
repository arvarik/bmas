[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Data Model](07-data-model.md) | [➡️ Next: UI — Agent Trace Inspector](09-ui-agent-trace-inspector.md) | [🎨 Design System](../design/DESIGN.md)

# 08 — UI: Live Blackboard Visualization & Variant Extensibility

> [!ABSTRACT]
> The headline feature: a **real-time graph of the blackboard** and a **worker-activity view** showing what every agent is doing, right now, on the shared problem — reusing the existing React Flow setup, primitives, tokens, and SSE plumbing. **No new shades, no new spacing values.** This document also specifies the **variant selector** (the per-task dropdown for traditional / PatchBoard / stigmergic) and the **panel registry** that lets each variant ship its own visualizations without rewiring Mission Control.

> [!IMPORTANT] Read doc 13 first for the density philosophy
> Because this is a **showcase artifact**, these surfaces follow *legible maximalism*, not minimalism. The governing UI philosophy, the dense "Mission" cockpit layout, and performance rules live in [doc 13 — UI Showcase Density](13-ui-showcase-density.md), which **supersedes** the "quiet/clarity-over-density" framing for these surfaces. This document covers the graph mechanics; doc 13 covers how it sits in the dense command center.

> [!IMPORTANT] Prerequisite
> This depends entirely on [06 — Agent Traces](06-agent-traces.md) and the `board_entry`/`trace` SSE events from [04 §9](04-blackboard-protocol.md#9-new-sse-event-types-additive). Build those first.

---

## 1. Design principles applied

The [DESIGN.md §1](../design/DESIGN.md) token system, type scale, spacing, and accessibility rules remain **fully binding**. The *density* principle is amended for showcase surfaces ([doc 13 §1](13-ui-showcase-density.md#1-amending-the-design-contract-deliberately-not-casually)). Concretely for the board graph:

- **Reuse, don't reinvent.** The board graph is an evolution of `DAGVisualizer.tsx`, which already wires `@xyflow/react`, the custom `BmasNode`, `Background`, `Controls`, `MiniMap`, and token-based styling. We add node/edge *types*, not a new rendering stack.
- **Entries are nodes; `refs` are edges.** The board model ([04 §1](04-blackboard-protocol.md#1-board-entries-typed-envelopes-natural-language-bodies)) is already a graph. No translation layer.
- **Status colors are sacred.** Node borders use the existing `STATUS_COLORS`; agent identity uses `AGENT_COLORS` (`design-tokens.ts`). New roles (critic, conflict-resolver, cleaner, decider) need **token additions** — added to `DESIGN.md §2.5` and `design-tokens.ts` *first* (see §8), never inline.
- **Authors are open-ended** (seam rule 3): generated experts, PatchBoard workers, and stigmergic actors all render via the deterministic fallback color generator ([13 §7](13-ui-showcase-density.md#7-component--token-additions)); the `AgentRole` enum is a display convenience only.

## 2. Where it lives

The existing `/task/[taskId]/blackboard` tab currently renders a flat `DebateList` (`blackboard/page.tsx`). We **split it into two views** behind a segmented control (matching the existing `task-tabs` pattern), preserving the debate list as one mode:

| Mode | Component | Purpose |
|:--|:--|:--|
| **Graph** (new default) | `BlackboardGraph.tsx` | Live entry/ref graph with agents acting on it |
| **Stream** | existing `DebateList` | Chronological entries (kept, now fed by `board_entry`) |

Recommended: in-tab toggle (consistent with the App Router layout in [DESIGN.md §6.4](../design/DESIGN.md#64-main-content-area)) to avoid a fifth tab.

### 2.1 The variant selector and the panel registry

The system will ultimately run three coordination paradigms ([03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)). The UI must be built for that **now**, even though V1 ships only `traditional` — otherwise the variant work becomes a Mission Control rewrite.

**The dropdown.** The task composer gains a compact variant `Select` (existing primitive) next to the submit button:

- Options come from the daemon's **`GET /capabilities`** endpoint ([07 §4](07-data-model.md#4-new-rest-endpoints-daemon-routes)): `{variants: [{id: "traditional", label: "Blackboard (bMAS)", available: true}, {id: "patchboard", label: "PatchBoard", available: false, reason: "planned"}, {id: "stigmergic", label: "Stigmergic", available: false, reason: "planned"}]}`. Unavailable variants render disabled with a tooltip — the dropdown ships in V1 with one enabled option, so the UX and plumbing are proven before the variants exist.
- The choice is submitted as `variant` on `POST /submit`, stored on the task ([07 §1.7](07-data-model.md#17-tasks-column-additions)), and displayed as a chip in the task header and task list.
- Default = `coordination.variant` from config.

**The panel registry.** Variant-specific surfaces register against the variant id instead of being hard-wired:

```ts
// mission-control/src/lib/variants.ts
export interface VariantUIAdapter {
  id: string;                                  // "traditional" | "patchboard" | "stigmergic"
  graph: {
    nodeTypes: Record<string, NodeRenderer>;   // entry-type → node renderer (traditional set below)
    edgeFor(entry: BoardEntry): EdgeSpec[];    // refs → typed edges
    overlays?: OverlayRenderer[];              // e.g. stigmergic pressure heatmap (doc 16 §6)
  };
  missionPanels: PanelSpec[];                  // extra cockpit panels (doc 13 §3) — e.g. PatchBoard's
                                               // blueprint inspector / transaction log (doc 11 §7)
  composerExtras?: ComponentType;              // variant-specific submit options
  eventHandlers: Record<string, (ev) => void>; // namespaced SSE events this variant emits
}

export const VARIANT_ADAPTERS: Record<string, VariantUIAdapter> = { traditional: …  /* 11 & 16 add theirs */ };
```

Rules: the shell (tabs, TopBar, stream plumbing, trace inspector, artifact browser, cost) is **shared and variant-blind**; anything paradigm-specific (node vocabularies, overlays, extra panels) lives in an adapter; unknown SSE event types are ignored by the shell and routed to the active adapter. The V1 acceptance test: adding a dummy adapter with one fake panel requires **zero** edits outside `variants.ts` + the adapter file.

## 3. The blackboard graph

```
┌─ Panel: "Blackboard" ─────────────────────────────────────── [Graph|Stream] ─┐
│                                                  Convergence ▓▓▓▓▓░░ 0.62      │
│   ┌────────────┐      ┌─────────────┐                                          │
│   │ OBJECTIVE  │      │ 📎 q3.pdf   │  (attachment node)                       │
│   └─────┬──────┘      └─────────────┘                                          │
│    ┌────┴─────┐                                                                │
│    ▼          ▼                                                                │
│ ┌──────────┐ ┌──────────┐      critique (dashed, status-error)                │
│ │ finding  │ │ finding  │◄┄┄┄┄┄┄┄┄┄┄┐                                          │
│ │ e-12 ●   │ │ e-13 ●   │           ┊                                           │
│ │ valuation│ │ supply   │      ┌────┴─────┐                                     │
│ └────┬─────┘ └──────────┘      │ critique │ (critic color)                      │
│      ▼  rebuttal (solid)       │ e-14  ●  │                                     │
│ ┌──────────┐                   └──────────┘                                     │
│ │ rebuttal │    ◆ conflict marker (expandable → private sub-board)              │
│ └──────────┘    📄 artifact node (src/main.py · expert.coder · v1)              │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### Node design (extends `BmasNode`)

Reuse the existing node anatomy from `DAGVisualizer.tsx` (lines 72–130): agent identity dot + label + `StatusBadge`. Add, per entry type:

- **Type glyph** (lucide icon, 14px, `--text-tertiary`): `Target` (objective), `Paperclip` (attachment), `ListTree` (plan), `Lightbulb` (finding), `AlertTriangle` (critique), `MessageSquareReply` (rebuttal), `GitMerge` (conflict), `CheckCircle2` (solution), `FileCode2` (artifact). Icons only — color stays in the dot/border.
- **Salience → visual weight.** Map `salience` (0..1) to node opacity (0.6→1.0) and a subtle scale (1.0→1.06). High-salience entries literally stand out. Respect `prefers-reduced-motion` (no scale pulse when reduced).
- **Confidence bar.** A 2px bottom border-fill proportional to `confidence`, in the author's color.
- **Live append animation.** When a `board_entry` event adds a node, it fades+scales in over 300ms (matches [DESIGN.md §7.3](../design/DESIGN.md#73-real-time-data-transitions)). New edges draw with the existing `animated` dashed-flow style.
- **Removal animation.** `entry_removed` (the Cleaner) fades the node to 20% opacity + strikethrough title for 2s, then removes it from the live layout (it remains in replay). The Cleaner *visibly tidying the board* is a showcase moment, not a silent mutation.

### Edge design

| `refs` relationship | Edge style (from existing token system) |
|:--|:--|
| plan → finding (derivation) | solid, `STATUS_COLORS.success` when uncritiqued, else status color |
| critique → finding | **dashed**, `STATUS_COLORS.error`, animated while unrebutted |
| rebuttal → critique | solid, author color |
| conflict marker | double-stroke, `STATUS_COLORS.paused` (amber) |
| entry → attachment/artifact (cites file) | dotted, `--text-tertiary` |

All edge colors come from `design-tokens.ts`; reuse the `NODE_BORDER`/`STATUS_COLORS` mapping already in `DAGVisualizer.tsx` (lines 47–60).

### Layout

The existing `layoutDag` (depth-by-dependency columns) generalizes: compute depth from `refs` instead of `depends_on`. For cyclic graphs (critique↔rebuttal), break cycles for layout by ranking on `created_at`/`seq` and rendering back-edges as curved. Consider `dagre`/`elkjs` only if hand-layout gets unwieldy — but start by extending `layoutDag` to keep the dependency minimal.

## 4. The worker-activity lane

A horizontal lane **above** the graph (or a right rail), showing each active turn as a live card. Data: `turn_start`/`turn_end` events + live `trace` events ([06 §4](06-agent-traces.md#4-the-bmas-trace-event-schema)).

```
┌─ Workers ─────────────────────────────────────────────────────────────────┐
│ ┌───────────────┐ ┌────────────────┐ ┌───────────────┐                     │
│ │● Critic       │ │● Expert:Valu.  │ │○ Cleaner       │                     │
│ │  node-2       │ │  node-1        │ │  idle          │                     │
│ │  ▸ web_search │ │  ▸ reasoning…  │ │                │                     │
│ │  324 tok ↑    │ │  1.2k tok ↑    │ │                │                     │
│ │  $0.0011      │ │  $0.0009       │ │                │                     │
│ └───────────────┘ └────────────────┘ └───────────────┘                     │
└──────────────────────────────────────────────────────────────────────────────┘
```

- Each card is a **`MetricCard`-styled** tile ([DESIGN.md §5.3](../design/DESIGN.md#53-metriccard)) with the agent identity dot + role + node, the current activity verb (from the latest `trace` type: "searching", "reasoning", "writing"), a live token meter (`tabular-nums`, [DESIGN.md §3.3](../design/DESIGN.md#33-numeric-typography)), and running cost.
- **The roster is dynamic** — constant roles + this task's generated experts (from genesis), rendered from data, never a hardcoded list. Idle agents render muted (state-awareness), so the operator sees the full roster.
- Clicking a card opens that turn's trace in the inspector ([09](09-ui-agent-trace-inspector.md)).

## 5. Private sub-boards and conflicts

A `conflict` entry renders as a `◆` marker node (amber). Clicking it opens a **modal/SplitView** (existing pattern) showing the private sub-board's exchange ([05 §4](05-control-unit.md#4-private-sub-boards-conflict-resolution)). On resolution, the marker collapses and the reconciled public entries animate into the main graph. This keeps the public graph quiet while making the deliberation inspectable on demand.

## 6. Convergence meter & rejection overlay

- **Convergence meter**: a slim progress bar in the Panel header bound to `consensus` events ([05 §3](05-control-unit.md#3-consensus--termination)) — explicitly labeled as a *progress signal* (salience-weighted uncritiqued-finding ratio), with the decider state ("Decider: not yet selected / evaluating / solution posted") next to it. Fill uses `--accent-primary`; an accepted solution triggers the single success pulse ([DESIGN.md §7.1](../design/DESIGN.md#71-feedback-on-every-action)).
- **Rejection overlay** (toggle, default off): when an `entry_rejected` event arrives, briefly flash a red outline + a toast ("Critic entry rejected: capability"). Rare in the traditional variant, invaluable when debugging; quiet by default.

## 7. Replay / scrubber

Because the board is an event log ([04 §5](04-blackboard-protocol.md#5-the-board-as-an-event-log)), completed tasks get a **timeline scrubber** at the bottom of the graph: drag to fold `board_events` up to seq N and watch the board assemble itself — including Cleaner removals un-happening as you scrub backward. Reuses `GET /tasks/{id}/board/replay` ([07 §4](07-data-model.md#4-new-rest-endpoints-daemon-routes)). For running tasks the scrubber sits at "live"; dragging back pauses live-follow (same pattern as the terminal "↓ N new lines" pill in `TaskLogTerminal.tsx`).

## 8. Token & primitive additions (do this first)

Per the design contract, add tokens before using them:

```ts
// design-tokens.ts — extend AgentRole + AGENT_COLORS (and DESIGN.md §2.5)
// NOTE: "executor"/"auditor" are LEGACY aliases (the paper has neither — doc 12 §2 / doc 04 §4).
// Kept only so existing tasks/colors keep rendering; new roles use the paper-faithful set.
// Per seam rule 3 this enum is a *display convenience* — back it with the deterministic
// fallback color generator (doc 13 §7) so dynamic authors (expert.<slug>, worker.<id>,
// universal-<n>) still render.
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

These hues are muted (per [DESIGN.md §2.5](../design/DESIGN.md#25-agent-identity-colors)) and must be mirrored as CSS custom properties in `globals.css` and documented in `DESIGN.md`.

## 9. State matrix (acceptance criteria)

Every new component implements all five states ([DESIGN.md §9](../design/DESIGN.md#9-state-design-matrix)):

| Component | Empty | Loading | Active | Error | Disabled |
|:--|:--|:--|:--|:--|:--|
| BlackboardGraph | "No board state yet — the swarm hasn't posted entries." + `Network` icon | 3 skeleton nodes + faded edges (reuse `Skeleton variant="dag"`) | live graph, animated entries | "Failed to load board" + retry | N/A |
| Worker lane | full roster, all idle | shimmer cards | live activity cards | "Trace stream unavailable" | idle agents muted |
| Convergence meter | 0.0, muted | indeterminate shimmer | filling bar + decider state | hidden | N/A |
| Variant selector | default option | — | enabled options from `/capabilities` | falls back to config default, warning toast | unavailable variants disabled + tooltip |

## 10. Files

| File | Action |
|:--|:--|
| `components/features/BlackboardGraph.tsx` | new (extends DAGVisualizer patterns) |
| `components/features/WorkerLane.tsx` | new (MetricCard-based) |
| `components/features/ConsensusMeter.tsx` | new (Panel-header bar) |
| `lib/variants.ts` | new — adapter registry (§2.1) |
| `components/features/VariantSelect.tsx` | new — composer dropdown (§2.1) |
| `app/task/[taskId]/blackboard/page.tsx` | edit — add Graph/Stream toggle |
| `hooks/useTaskStream.ts` | edit — handle `board_entry`, `entry_removed`, `consensus`, `turn_start/end`, `entry_rejected`, `file_added`, `artifact_created` |
| `lib/design-tokens.ts`, `app/globals.css`, `docs/design/DESIGN.md` | edit — new agent role tokens + fallback color generator |
| `app/api/tasks/[taskId]/board/route.ts`, `…/turns/route.ts`, `app/api/capabilities/route.ts` | new proxies |

➡️ Continue to [09 — UI: Agent Trace Inspector](09-ui-agent-trace-inspector.md).
