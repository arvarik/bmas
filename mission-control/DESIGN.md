[🏠 Index](../README.md) | [📋 Context](../examples/stigmergic/CONTEXT.md)

# Mission Control — Design System

> [!ABSTRACT] Purpose
> This document is the **single source of truth** for Mission Control's visual language. Every component, every screen, every interaction should reference this spec to maintain enterprise-grade consistency. This is not a component library — it is the design contract that any implementation (human or AI) must follow.
>
> **Inspiration benchmarks:** Notion (information density + clean hierarchy), Airbnb (typography + whitespace mastery), Linear (real-time ops dashboard), Vercel (dark-mode polish).

---

## 1. Design Principles

These principles are ordered by priority. When two principles conflict, the higher-ranked one wins.

1. **Clarity over density.** The operator should understand the swarm state within 2 seconds of looking at the screen. If they need to read a label to understand what a color means, the design failed.

2. **Consistency over novelty.** Every panel, badge, chart, and button must draw from the same token system defined below. Never invent a new shade, spacing value, or border radius inline. If you need a new token, add it here first.

3. **State-awareness everywhere.** Every component has five states: *empty*, *loading*, *active*, *error*, *disabled*. Designing only the happy path is a failure. The empty state matters most — it's what the operator sees before the daemon is running.

4. **Quiet until important.** The default UI is calm — muted tones, subtle borders, unhurried animations. Only status changes (task completion, failures, pause events) should demand attention. The UI should never feel like it's shouting.

5. **Motion is communication.** Every transition has purpose. A panel slides in to show spatial relationship. A number morphs to show change. A color pulses to show activity. Gratuitous animation is worse than no animation.

---

## 2. Color System

Dark-mode-first. All colors defined as HSL for precise control.

### 2.1 Surface Hierarchy (Backgrounds)

The UI uses layered surfaces to create depth without borders. Each level is slightly lighter than the one below it, like cards stacked on a desk.

| Token | HSL | Usage |
|:---|:---|:---|
| `--surface-base` | `222 47% 6%` | Page background (deepest layer) |
| `--surface-raised` | `222 44% 9%` | Sidebar, fixed panels |
| `--surface-overlay` | `222 40% 12%` | Cards, panels, modals |
| `--surface-hover` | `222 36% 16%` | Hover state on cards/rows |
| `--surface-active` | `222 32% 20%` | Active/selected state |

> [!NOTE] The Notion Principle
> Notice the surfaces don't use borders to separate sections — they use *elevation* (progressively lighter backgrounds). This creates the same clean, boundaryless feel as Notion's workspace. Reserve borders for **interactive containers only** (inputs, terminal panes).

### 2.2 Text Hierarchy

| Token | HSL | Usage |
|:---|:---|:---|
| `--text-primary` | `210 20% 96%` | Headings, primary labels, metric values |
| `--text-secondary` | `215 15% 65%` | Body text, descriptions, secondary labels |
| `--text-tertiary` | `220 10% 45%` | Timestamps, metadata, disabled text |
| `--text-inverse` | `222 47% 6%` | Text on light/accent backgrounds |

### 2.3 Semantic Status Colors

These are the **only** colors used for status indication. They must be used consistently across DAG nodes, log headers, badges, and chart segments.

| Token | HSL | Purpose | Where Used |
|:---|:---|:---|:---|
| `--status-pending` | `220 15% 50%` | Awaiting execution | DAG nodes, task list |
| `--status-running` | `217 91% 60%` | Currently executing | DAG nodes (animated edge), pulsing indicator |
| `--status-success` | `142 71% 45%` | Completed successfully | DAG nodes, cost savings badge |
| `--status-error` | `0 84% 60%` | Failed or degraded | DAG nodes, health alerts, error banners |
| `--status-paused` | `38 92% 50%` | Operator-paused (HITL) | HITL controls, status bar |

### 2.4 Accent & Interactive Colors

| Token | HSL | Usage |
|:---|:---|:---|
| `--accent-primary` | `217 91% 60%` | Primary buttons, active nav, links |
| `--accent-primary-hover` | `217 91% 50%` | Button hover state |
| `--accent-subtle` | `217 91% 60% / 10%` | Subtle accent tint (badge backgrounds, highlights) |
| `--border-default` | `222 20% 22%` | Input borders, table dividers |
| `--border-focus` | `217 91% 60%` | Focus ring (matches accent) |

### 2.5 Agent Identity Colors

Each agent role has a unique identity color used in terminal headers, DAG node accents, and log prefixes. These are intentionally muted to avoid competing with status colors.

| Agent | Token | HSL | Usage |
|:---|:---|:---|:---|
| Planner | `--agent-planner` | `265 50% 60%` | Purple — strategic, cerebral |
| Executor | `--agent-executor` | `175 60% 45%` | Teal — action, execution |
| Auditor | `--agent-auditor` | `32 80% 55%` | Amber — review, judgment |

---

## 3. Typography

### 3.1 Font Stack

| Purpose | Font | Fallback | Notes |
|:---|:---|:---|:---|
| **UI / Body** | `Inter` | `system-ui, -apple-system, sans-serif` | Load weights 400, 500, 600 from Google Fonts |
| **Monospace** | `JetBrains Mono` | `ui-monospace, 'Cascadia Code', monospace` | Terminals, code, metric values. Load weight 400 only |

### 3.2 Type Scale

All sizes in `rem` (base 16px). Line heights are unitless multipliers.

| Token | Size | Weight | Line Height | Usage |
|:---|:---|:---|:---|:---|
| `--text-xs` | `0.6875rem` (11px) | 500 | 1.45 | Badges, timestamps, metadata |
| `--text-sm` | `0.8125rem` (13px) | 400 | 1.5 | Body text, descriptions, table cells |
| `--text-base` | `0.9375rem` (15px) | 500 | 1.5 | Section labels, nav items, input text |
| `--text-lg` | `1.125rem` (18px) | 600 | 1.4 | Panel titles, card headers |
| `--text-xl` | `1.5rem` (24px) | 600 | 1.3 | Page title (sidebar header) |
| `--text-metric` | `1.75rem` (28px) | 600 | 1.2 | Hero metric numbers (cost, tokens) |
| `--text-mono` | `0.8125rem` (13px) | 400 | 1.6 | Terminal output, Redis keys, code |

> [!IMPORTANT] The Airbnb Principle
> Airbnb uses only **3 font weights** across their entire platform (Regular, Medium, Semibold). We do the same: `400`, `500`, `600`. Never use `700`/`800` — it creates visual noise that competes with status colors.

### 3.3 Numeric Typography

Apply `font-variant-numeric: tabular-nums` to **all live-updating numeric values** — cost tickers, token counts, durations, and any other number that changes in real-time. Without this, proportional digit widths in Inter cause layout jitter as values tick (e.g., `$0.0129` → `$0.0134` shifts by 1-3px because `1` is narrower than `3`). Tabular-nums forces monospace digit widths while preserving the font's proportional letter spacing for surrounding text.

```css
/* Apply globally to elements with changing numbers */
.metric-value,
.cost-ticker,
.token-count,
.duration-value {
  font-variant-numeric: tabular-nums;
}
```

Inter supports this OpenType feature natively — no fallback needed. This is a zero-cost CSS declaration that eliminates an entire class of layout shifts.

> [!NOTE]
> This only applies to **live-updating** values where jitter is visible. Static numeric values (e.g., a completed task's final cost in a table row) don't need it, though applying it globally to all mono-font numbers is harmless and recommended for consistency.

---

## 4. Spacing & Layout

### 4.1 Spacing Scale

All spacing derived from a **4px base unit**. Only these values may be used.

| Token | Value | Common Usage |
|:---|:---|:---|
| `--space-1` | `4px` | Inline icon gaps, badge padding-x |
| `--space-2` | `8px` | Between related items (e.g., badge + label) |
| `--space-3` | `12px` | Inside compact components (table cells, list items) |
| `--space-4` | `16px` | Standard padding (cards, inputs) |
| `--space-5` | `20px` | Section spacing within panels |
| `--space-6` | `24px` | Between panels, sidebar section gaps |
| `--space-8` | `32px` | Page section spacing |
| `--space-10` | `40px` | Major section dividers |
| `--space-12` | `48px` | Page-level horizontal padding |

### 4.2 Border Radius

| Token | Value | Usage |
|:---|:---|:---|
| `--radius-sm` | `6px` | Badges, small buttons, chips |
| `--radius-md` | `8px` | Input fields, dropdowns |
| `--radius-lg` | `12px` | Cards, panels, modals |
| `--radius-xl` | `16px` | Large containers (terminal panes) |
| `--radius-full` | `9999px` | Pill badges, circular indicators |

### 4.3 Elevation (Shadows)

Shadows are used **sparingly** — only for floating elements (modals, popovers, command palette). Panels and cards use background elevation (§2.1), not shadows.

| Token | Value | Usage |
|:---|:---|:---|
| `--shadow-sm` | `0 1px 2px hsl(222 47% 4% / 0.3)` | Dropdowns, tooltips |
| `--shadow-md` | `0 4px 12px hsl(222 47% 4% / 0.4)` | Modals, command palette |
| `--shadow-lg` | `0 8px 24px hsl(222 47% 4% / 0.5)` | Full-screen overlays |

---

## 5. Component Patterns

These are the **reusable primitives** that every feature component must be composed from. Building feature components directly from raw HTML/Tailwind (without using these primitives) violates the design contract.

### 5.1 Panel

The fundamental container. Every feature area (DAG, Terminals, Blackboard, etc.) is wrapped in a Panel.

- **Background:** `--surface-overlay`
- **Border radius:** `--radius-lg` (12px)
- **Padding:** `--space-5` (20px) on all sides
- **Header:** Left-aligned title (`--text-lg`, `--text-primary`), optional subtitle (`--text-sm`, `--text-secondary`), optional right-aligned action buttons
- **Divider:** A 1px line (`--border-default`) separating header from body. Gap of `--space-4` above and below.
- **States:**
  - *Loading*: Header renders normally. Body replaced with skeleton (pulsing rectangles matching the expected layout).
  - *Error*: Body replaced with centered error message + retry button. Red left-border accent (`--status-error`).
  - *Empty*: Body replaced with centered icon + message (e.g., "No tasks yet — submit one to see the DAG"). Muted icon, secondary text.

> Like Notion's content blocks — clean header, clear boundary, self-contained.

### 5.2 StatusBadge

Small, inline indicator used across DAG nodes, task lists, agent health, and the top bar.

- **Shape:** Pill (`--radius-full`)
- **Size:** `--text-xs`, padding `2px 8px`
- **Colors:** Background is the status color at 15% opacity. Text is the status color at 100%.
- **Variants:** `pending`, `running`, `success`, `error`, `paused`
- **The `running` variant** has a pulsing dot animation (tiny circle that fades in/out on a 2s cycle) before the label.

### 5.3 MetricCard

Used in Cost Tracker and Telemetry. Displays a single numeric value with context.

- **Layout:** Vertical stack — label on top (`--text-sm`, `--text-secondary`), value below (`--text-metric`, mono font, `--text-primary`), optional delta/trend indicator at bottom.
- **Background:** `--surface-overlay`
- **Border radius:** `--radius-lg`
- **Hover:** Background shifts to `--surface-hover`. Cursor: default (not pointer, unless clickable).
- **Number transitions:** When the value changes, the digits morph (count up/down) over 400ms using CSS `transition` on `opacity` + a brief scale pulse (1.0 → 1.02 → 1.0).

### 5.4 TerminalPane

Wrapper for the xterm.js terminal instances. Gives each terminal an identity.

- **Header bar:** 32px tall. Background: `--surface-raised`. Contains:
  - Left: Agent identity dot (8px circle, filled with `--agent-{role}` color) + role name (`--text-sm`, `--text-primary`)
  - Right: Connection status indicator (green dot = connected, pulsing = reconnecting, red = disconnected) + "Clear" action button
- **Body:** xterm.js fills remaining height. Border: 1px `--border-default`. Border radius bottom: `--radius-xl`.
- **Border-top accent:** 2px solid line in the agent's identity color across the full width.

### 5.5 DebateList

Used by the Blackboard/Debate tab. Chronological list of debate entries from the agent deliberation process. Replaces the old SplitView (Public/Private Redis split).

- **Layout:** Vertical list of debate entries, newest at the bottom. Each entry shows: agent role identity (colored dot + name), content (text/markdown), timestamp.
- **Agent identity:** 8px circle dot in the agent's `--agent-{role}` color + role name in `--text-sm`.
- **Content:** Rendered as text or markdown in `--text-sm`. Long entries are capped with a "Show more" toggle.
- **Typing indicator:** For running tasks, a special bottom row shows "{agent} is {verb}..." with a bouncing ellipsis animation (see [07-task-detail-views.md](../docs/ui-overhaul/07-task-detail-views.md)).
- **Smart auto-scrolling:** Uses `IntersectionObserver` on a sentinel element at the bottom. When user scrolls up, auto-scroll suspends and a frosted-glass "↓ N new updates" pill appears.

### 5.6 ActionButton

All interactive buttons. Three variants:

| Variant | Background | Text | Border | Usage |
|:---|:---|:---|:---|:---|
| **Primary** | `--accent-primary` | `--text-inverse` | none | Main CTAs (Submit Task, Resume) |
| **Secondary** | `transparent` | `--text-secondary` | 1px `--border-default` | Secondary actions (Clear, Filter) |
| **Danger** | `--status-error` at 15% | `--status-error` | none | Destructive (Delete Skill, Abort) |

- **Size:** Height 36px, padding `0 16px`, `--text-sm`, `--radius-md`
- **Hover:** Background lightens 10%. Transition: 150ms ease.
- **Active (pressed):** Scale 0.98. Transition: 50ms.
- **Disabled:** Opacity 0.4. Cursor: not-allowed.
- **Loading:** Content replaced with a 16px spinner. Button width preserved (no layout shift).

### 5.7 Skeleton

Placeholder for loading states. Used inside Panel bodies.

- **Shape:** Rounded rectangles matching the expected content layout (e.g., 3 wide bars for a chart, a grid of small rectangles for a DAG).
- **Color:** `--surface-hover`
- **Animation:** Shimmer — a diagonal gradient highlight sweeps left-to-right on a 1.5s infinite loop.
- **Rule:** Skeletons must approximate the *shape* of the loaded content. A generic spinner is never acceptable.

### 5.8 EmptyState

Displayed when a Panel has no data (e.g., no tasks submitted, no skills learned).

- **Layout:** Centered vertically and horizontally within the Panel body.
- **Icon:** A lucide-react icon at 48px, color `--text-tertiary`.
- **Message:** Primary line (`--text-base`, `--text-secondary`) + optional secondary hint (`--text-sm`, `--text-tertiary`).
- **Optional CTA:** An ActionButton (Primary variant) below the message if the empty state is actionable.

---

## 6. Layout Architecture

Mission Control uses a **task-centric architecture** — the sidebar shows task history, and the main area shows either a landing page (task input) or a task detail page with tabbed sub-views. This replaces the previous monolithic dashboard grid. See [05-frontend-routing.md](../docs/ui-overhaul/05-frontend-routing.md) and [06-sidebar-and-landing.md](../docs/ui-overhaul/06-sidebar-and-landing.md) for full specs.

### 6.1 Page Structure

```
┌──────────────────────────────────────────────────┐
│  Top Bar (48px)                                  │
│  [Mission Control]  [Daemon Status]  [Cost: $0.04]│
├──────────┬───────────────────────────────────────┤
│ Sidebar  │  Main Content Area                    │
│ (240px)  │                                       │
│          │  Two views, based on route:           │
│ [+New]   │                                       │
│──────────│  A) Landing Page (route: /)            │
│ TODAY    │     Conversational task input          │
│ ● task-a │     + recent task history              │
│ ✓ task-b │                                       │
│ ✗ task-c │  B) Task Detail (route: /task/[id])    │
│──────────│     ┌─ Header: label, status, cost ─┐ │
│ YESTERDAY│     │ [Overview][DAG][Logs][Board][$$]│ │
│ ✓ task-d │     ├─────────────────────────────────┤ │
│──────────│     │  Tab content fills this area    │ │
│ ─System─ │     │  (DAG, Logs, Blackboard, Cost)  │ │
│ 📡 Infra │     └─────────────────────────────────┘ │
│ ✨ Skills│                                       │
│──────────│                                       │
│ 🟢🟢🔴  │                                       │
└──────────┴───────────────────────────────────────┘
```

### 6.2 Top Bar

- **Height:** 48px. Background: `--surface-raised`. Bottom border: 1px `--border-default`.
- **Left section:** App name "Mission Control" in `--text-lg` + a tiny `StatusBadge` showing daemon connection state (data from `useSystemStream()` SSE hook).
- **Center section:** When viewing a task: breadcrumb showing task ID and current phase (e.g., "task-a8f2 › Planning"). On the landing page: empty.
- **Right section:** Live cost ticker — total spend in `--text-mono` with `font-variant-numeric: tabular-nums` (§3.3), updating via SSE system stream. Formatted as "$0.0429" with the dollar sign in `--text-tertiary`.

### 6.3 Sidebar

Replaced from feature-navigation to **task-history sidebar**. See [06-sidebar-and-landing.md §6.1](../docs/ui-overhaul/06-sidebar-and-landing.md) for full spec.

- **Width:** 240px (collapsible to 48px icon-only mode).
- **Background:** `--surface-raised`
- **"+ New Task" button:** Top of sidebar, primary ActionButton, navigates to `/` (landing page).
- **Task list:** Date-grouped (Today, Yesterday, Last 7 Days, etc.). Each item shows status indicator + task ID + truncated label. Active item (matching `usePathname()`) styled with `--surface-active` + 2px left accent bar.
- **System section:** Divider + Infrastructure (`/infra`) and Skills (`/skills`) nav items.
- **Collapse behavior:** Toggle icon collapses to 48px. In collapsed mode: "+" icon for new task, status dots for each task (no labels).
- **Bottom:** Agent health indicators — three small dots (one per agent node), colored green/red. Data from `useSystemStream()`.
- **Mobile:** Slide-out drawer (same as before), closes on navigation.

### 6.4 Main Content Area

The main area renders Next.js App Router pages based on the URL:

| Route | Content |
|:---|:---|
| `/` | Landing page — conversational task input with auto-resize textarea, inline send button, and recent task history cards |
| `/task/[taskId]` | Task overview — result display, sub-task progress, live phase indicator |
| `/task/[taskId]/dag` | DAG visualization — React Flow canvas showing task decomposition graph |
| `/task/[taskId]/logs` | Log terminals — 3-column (or tabbed on mobile) xterm.js terminals filtered by agent role |
| `/task/[taskId]/blackboard` | Debate history — chronological list of agent debate entries with typing indicators |
| `/task/[taskId]/cost` | Cost breakdown — per-phase and per-model cost tables with MetricCard heroes |
| `/infra` | Infrastructure telemetry — Beszel Hub metrics for all nodes |
| `/skills` | Skills explorer — per-agent skill lists with view/delete actions |

All `/task/[taskId]/*` pages share a **task detail layout** (`task/[taskId]/layout.tsx`) that renders the task header + tab navigation and hosts the `TaskStreamContext.Provider` for SSE data distribution. Tab switches are instantaneous DOM swaps — the SSE connection persists in the layout.

### 6.5 Responsive Behavior

| Breakpoint | Behavior |
|:---|:---|
| ≥1440px | Full layout as designed (sidebar + main content) |
| 1024–1439px | Sidebar collapses to icon-only. |
| <1024px | Sidebar becomes a slide-out drawer. Single-column layout. Terminal panes stack vertically. |

> [!NOTE] Mission Control is a desktop-first ops dashboard. Mobile is a "read-only glance" mode — show swarm status and cost, but don't attempt full interactivity on small screens.

---

## 7. Interaction Patterns

### 7.1 Feedback on Every Action

| Action | Feedback |
|:---|:---|
| Submit task | Textarea clears, optimistic navigation to `/task/[id]` with submitted text visible immediately via `PendingTaskContext` (see [06-sidebar-and-landing.md](../docs/ui-overhaul/06-sidebar-and-landing.md)). SSE stream connects in the background. |
| Click "Pause Swarm" | Button immediately transitions to `paused` state (amber). Top bar shows "⏸ PAUSED" badge. Disable button until API confirms. |
| Click "Resume" | Button shows spinner for up to 500ms, then transitions back to default. Top bar badge disappears. |
| Submit hint | Input clears. A toast notification slides in from bottom-right: "Hint delivered to task-2a8f" (auto-dismiss 3s). |
| Delete skill | Confirmation dialog first: "Delete skill 'web-scraping'? This cannot be undone." On confirm, row fades out (200ms). |
| Task completes | DAG node pulses green once (300ms). Cost ticker morphs to new value. Toast: "Task-2a8f completed." |
| Task fails | DAG node pulses red once. Toast (persistent, requires dismiss): "Task-2a8f failed: [error summary]." |
| Switch tabs | Instantaneous — SSE stream persists in the layout, tab pages swap without reconnection. Zero latency. |

### 7.2 Keyboard Shortcuts

| Shortcut | Action |
|:---|:---|
| `Space` | Toggle pause/resume (when no input is focused) |
| `Esc` | Close any modal, navigate back to landing page |
| `⌘ K` / `Ctrl K` | Open command palette (future — reserve the pattern now) |

> [!NOTE]
> The previous `1`–`6` number key shortcuts for feature-navigation sidebar items were removed with the transition to the task-history sidebar. Those items are now full URL routes, not in-page view switches.

### 7.3 Real-time Data Transitions

- **Metric value changes:** Numbers count up/down over 400ms (not instant replacement). Use `requestAnimationFrame` for smooth interpolation. Apply `font-variant-numeric: tabular-nums` (§3.3) to prevent jitter.
- **DAG node status change:** Node background cross-fades to new color over 300ms. If transitioning to `running`, the connected edge becomes animated (dashed, flowing).
- **New log lines:** Text appears at bottom of terminal with no animation (instant — animation would feel laggy in a terminal context). Smart auto-scrolling suspends when the user scrolls up; a frosted-glass "↓ N new lines" pill appears at the bottom.
- **New debate entries:** Appended with smooth scroll if the user is near the bottom (IntersectionObserver-based). A "↓ N new updates" pill appears if auto-scroll is suspended.
- **Typing indicators:** During running tasks, the Blackboard tab shows a live "thinking" row with a bouncing ellipsis for the currently active agent (e.g., "planner is deliberating..."). Respects `prefers-reduced-motion`.
- **SSE stream events:** No visual indicator for individual events (they should feel seamless). If the SSE connection drops, the header shows a reconnecting indicator.

---

## 8. Toast Notification System

- **Position:** Bottom-right, stacked vertically (newest on top).
- **Width:** 360px.
- **Background:** `--surface-overlay` with `--shadow-md`.
- **Border-left:** 3px solid, colored by type: `--status-success` (green), `--status-error` (red), `--accent-primary` (info).
- **Content:** Icon (left) + message text (`--text-sm`) + optional dismiss button (right).
- **Animation:** Slide in from right (200ms ease-out). Slide out downward on dismiss (150ms).
- **Auto-dismiss:** Success/info toasts dismiss after 3 seconds. Error toasts persist until manually dismissed.
- **Stacking:** Maximum 3 visible. Older toasts are pushed up and auto-dismissed when a 4th arrives.

---

## 9. State Design Matrix

Every feature component must implement all five states. This matrix is the acceptance criteria.

| Component | Empty | Loading | Active | Error | Disabled |
|:---|:---|:---|:---|:---|:---|
| **DAG Visualizer** | "No active tasks. Submit a task to see the execution graph." + illustration icon | Skeleton: 3 placeholder nodes with faded edges | Interactive React Flow canvas | "Failed to load state" + retry button | N/A |
| **Log Terminal** | Gray terminal with "Waiting for agent output..." centered | Terminal header shows "Connecting..." with pulsing dot | Live scrolling output with smart auto-scroll | Header turns red, "Disconnected — reconnecting..." | N/A |
| **HITL Controls** | All buttons enabled, hint input placeholder: "Enter guidance for the swarm..." | Pause button shows spinner during API call | Amber "PAUSED" state with hint input active | "Daemon unreachable" inline error below buttons | During active task execution with no HITL support |
| **Debate History** | "No debate entries yet — submit a task to see agent deliberation." | Skeleton: 4 debate entry placeholders | Chronological debate list with typing indicators | "Failed to load debate history" + retry button | N/A |
| **Cost Tracker** | MetricCards show "$0.00" and "0 tokens" (not blank) | Skeleton: 3 MetricCard shapes + chart outline | Live updating numbers and chart | "Metrics unavailable" with last-known values grayed | N/A |
| **Skills Explorer** | Per-tab: "No skills learned yet. Skills are created as agents complete tasks." | Skeleton: 5 list items | Skill list with view/delete actions | "Agent unreachable" per-tab error | Delete button disabled during delete API call |
| **Telemetry** | "No telemetry data — verify Beszel Hub connection." | Skeleton: gauge shapes | Live gauges/numbers | "Beszel Hub unreachable" + timestamp of last successful fetch | N/A |

---

## 10. Accessibility Requirements

- **Contrast:** All text meets WCAG 2.1 AA (4.5:1 for body text, 3:1 for large text). The color tokens above are pre-validated.
- **Focus indicators:** All interactive elements show a 2px `--border-focus` ring on keyboard focus. Never remove default focus styles without providing a replacement.
- **Touch targets:** All buttons and clickable elements are at least 36px tall (desktop) / 44px (tablet/mobile).
- **Reduced motion:** Wrap all animations in `@media (prefers-reduced-motion: reduce)` and disable them. Skeleton shimmer should become a static gray when motion is reduced.
- **Screen readers:** Status changes (task complete, swarm paused) should trigger `aria-live` announcements. DAG nodes should have `aria-label` describing their status.
- **Tab order:** Follows visual layout: Top Bar → Sidebar → Main Content (left-to-right, top-to-bottom).

---

## 11. File Organization

The project uses Next.js App Router with file-based routing. Feature components live alongside their routes as page components, not in a flat `components/` directory. See [05-frontend-routing.md](../docs/ui-overhaul/05-frontend-routing.md) for the full route structure.

```
src/
├── app/
│   ├── layout.tsx                    # Root layout: TopBar + TaskSidebar + <main>{children}</main>
│   ├── page.tsx                      # Landing page: task input + recent history
│   ├── task/
│   │   └── [taskId]/
│   │       ├── layout.tsx            # Task detail layout: header + tabs + TaskStreamContext.Provider
│   │       ├── page.tsx              # Overview tab (default)
│   │       ├── dag/page.tsx          # DAG visualization
│   │       ├── logs/page.tsx         # Log terminals (3 columns)
│   │       ├── blackboard/page.tsx   # Debate history
│   │       └── cost/page.tsx         # Cost breakdown
│   ├── infra/page.tsx                # Infrastructure telemetry
│   └── skills/page.tsx               # Skills explorer
├── components/
│   ├── ui/                           # Design system primitives (this spec)
│   │   ├── Panel.tsx
│   │   ├── StatusBadge.tsx
│   │   ├── MetricCard.tsx
│   │   ├── TerminalPane.tsx
│   │   ├── ActionButton.tsx
│   │   ├── Skeleton.tsx
│   │   ├── EmptyState.tsx
│   │   └── Toast.tsx
│   ├── TopBar.tsx                    # "use client" — daemon status, cost ticker
│   ├── TaskSidebar.tsx               # "use client" — task history, system nav
│   ├── DAGVisualizer.tsx             # Feature: React Flow canvas
│   ├── LogTerminal.tsx               # Feature: xterm.js + TerminalPane
│   └── DebateList.tsx                # Feature: debate entries + typing indicator
├── contexts/
│   ├── TaskStreamContext.tsx          # Distributes SSE data from useTaskStream to tab pages
│   └── PendingTaskContext.tsx         # Optimistic UI: ephemeral submitted-text context
├── hooks/
│   ├── useTaskStream.ts              # SSE hook for /api/stream/task/[taskId] (logs, phase, debate, etc.)
│   ├── useSystemStream.ts            # SSE hook for /api/stream/system
│   └── useTaskHistory.ts             # REST fetch for GET /api/tasks + refetch on system events
└── lib/
    └── design-tokens.ts              # Programmatic access to tokens
```

> [!IMPORTANT] Primitive-First Composition
> Feature components (DAG, Logs, Debate, etc.) must **not** directly use raw CSS for layout, backgrounds, or spacing. They compose from the `ui/` primitives, which themselves use the token system. This creates one layer of indirection that makes future redesigns possible without touching feature logic.

> [!IMPORTANT] SSE Data Flow
> The `useTaskStream` hook is called **once** in `task/[taskId]/layout.tsx` and distributed via `TaskStreamContext.Provider`. Individual tab pages consume data via `useTaskData()` — they never create their own `EventSource` connections. This prevents the "tab switch tear" where SSE reconnects cause 200-500ms flickers. See [03-sse-architecture.md §3.4](../docs/ui-overhaul/03-sse-architecture.md).

---

> [!TIP] Using This Spec
> When implementing any UI feature, follow this checklist:
> 1. Read this DESIGN.md
> 2. Identify which primitives (Panel, StatusBadge, etc.) your feature needs
> 3. Compose your feature from those primitives
> 4. Verify all five states (empty, loading, active, error, disabled)
> 5. Verify animations respect `prefers-reduced-motion`
> 6. Verify keyboard navigation works without a mouse
