# Mission Control — Design System

This document describes the visual system of Mission Control after the 2026 redesign. It is the source of truth for `mission-control/src/app/globals.css` (tokens), `ui.css` (shared components), and `views.css` (page-specific rules).

Read this document before you add a visual pattern. Reuse a token or component before you create one.

## 1. Principles

1. **One surface ladder.** Every element sits on one of five surfaces. Depth comes from the ladder and from hairline rings, not from heavy borders.
2. **Quiet by default, loud when it matters.** Most text is secondary ink. Primary ink, accent, and status color mark the things an operator must act on.
3. **The data is the interface.** Tables, chips, and metrics carry the information. Decoration never competes with them.
4. **Live state is visible.** A running task shows a pulse, a live label, and a growing execution flow. A finished task shows a result first.
5. **No native controls.** Dropdowns, toggles, and segmented controls use the app components so they match on every platform.
6. **Plain language.** Labels say what a thing does. Descriptions use one sentence. Error states say what failed, why, and what fixes it.

## 2. Tokens

All tokens live on `:root` in `globals.css`. Colors use OKLCH so the steps between surfaces stay even.

### 2.1 Surface ladder

| Token | Value | Use |
|:--|:--|:--|
| `--surface-base` | `oklch(20.5% 0.008 262)` | Page background |
| `--surface-raised` | `oklch(23.5% 0.008 262)` | Sidebar, cards, table bodies |
| `--surface-overlay` | `oklch(26.5% 0.008 264)` | Inputs, popovers, composer, save bar |
| `--surface-hover` | `oklch(29.5% 0.008 266)` | Hover state, chips, muted pills |
| `--surface-active` | `oklch(32.5% 0.008 268)` | Selected navigation, pressed controls |

Each step is about 3% lighter than the one below it. Do not invent a surface between two steps.

### 2.2 Ink

| Token | Value | Use |
|:--|:--|:--|
| `--text-primary` | `oklch(96.4% 0.002 248)` | Titles, values, selected labels |
| `--text-secondary` | `oklch(75% 0.008 260)` | Body text, descriptions |
| `--text-tertiary` | `oklch(60% 0.01 264)` | Labels, metadata, placeholders |
| `--text-inverse` | `oklch(20.5% 0.008 262)` | Text on accent or white |

### 2.3 Accent, status, and tints

| Token | Value | Use |
|:--|:--|:--|
| `--accent-primary` | `oklch(68% 0.173 253)` | Primary buttons, active tab, links |
| `--accent-primary-hover` | `oklch(74% 0.16 253)` | Primary button hover |
| `--accent-ink` | `oklch(78.8% 0.113 248)` | Accent text on dark surfaces |
| `--accent-subtle` | accent at 16% alpha | Selected table rows, count badges |
| `--status-success` | `oklch(70.5% 0.154 154)` | Completed, ready, passed |
| `--status-error` | `oklch(68% 0.18 22)` | Failed, missing, unreachable |
| `--status-paused` / `--status-warning` | `oklch(74.6% 0.156 56)` | Needs attention, unsaved, degraded |
| `--status-running` | same as accent | Running, live |
| `--status-pending` | `oklch(62% 0.012 262)` | Queued, unknown |
| `--tint-accent`, `--tint-success`, `--tint-error`, `--tint-warning` | status color at 14% alpha | Pill and badge backgrounds |

A status pill pairs one status color for text with the matching tint for background. Never put status text on a solid status background.

### 2.4 Lines and elevation

| Token | Use |
|:--|:--|
| `--border-subtle` | Row dividers inside a card |
| `--border-default` | Input borders, hairline rings |
| `--border-strong` | Hover border on inputs and buttons |
| `--border-focus`, `--focus-ring` | Keyboard focus and focused inputs |
| `--shadow-hairline` | A 1px ring with no shadow (tables) |
| `--shadow-btn` | Pressed segmented option |
| `--shadow-card` | Cards, catalog panels, settings cards |
| `--shadow-raised` | Composer, hovered cards |
| `--shadow-overlay` | Popovers, menus, the save bar, dialogs |

Cards use `border-color: transparent` plus `--shadow-card`. The ring in the shadow draws the edge. Only inputs keep a real border.

### 2.5 Radius

| Token | Value | Use |
|:--|:--|:--|
| `--radius-sm` | 6px | Chips, code, small pills |
| `--radius-md` | 8px | Buttons, inputs, menus, segmented controls |
| `--radius-lg` | 10px | Cards, panels, popovers |
| `--radius-xl` | 14px | Dialogs |
| `--radius-full` | 9999px | Status pills, toggles, avatars |

The composer uses a 22px radius on purpose. It is the one soft shape on the home page.

### 2.6 Typography

System sans (`--font-sans`) for all text. System mono (`--font-mono`) for identifiers, hosts, costs, and code.

| Token | Size | Use |
|:--|:--|:--|
| `--text-xs` | 12px | Metadata, table headers, descriptions |
| `--text-sm` | 14px | Body, inputs, buttons |
| `--text-base` | 15px | Composer text, card titles |
| `--text-lg` | 18px | Section headings |
| `--text-xl` | 24px | Page titles |
| `--text-metric` | 28px | Large numbers |

Rules:

- Headings use `letter-spacing: -0.01em`.
- Small uppercase labels (eyebrows, table headers, fact labels) use `letter-spacing: 0.06em` and `--text-xs` or 10px.
- `body` sets `font-variant-numeric: tabular-nums`, so numbers align in tables and metrics.
- Weights: 400 body, 500 labels and selected items, 600 headings and titles.

### 2.7 Spacing and motion

Spacing uses the 4px scale `--space-1` (4) to `--space-12` (48). Page content uses `--space-6` padding on desktop and `--space-4` on small screens.

| Token | Value | Use |
|:--|:--|:--|
| `--ease-out-strong` | `cubic-bezier(.23, 1, .32, 1)` | Hover, press, menus, save bar |
| `--ease-out` | `cubic-bezier(0, 0, .2, 1)` | Fades |
| `--duration-fast` | 120ms | Color and background changes |
| `--duration-base` | 180ms | Layout changes |

Buttons and chips scale to 0.99 when pressed. The Workspace setting **Reduce motion** sets `body[data-reduced-motion="true"]`, which removes every animation and transition.

## 3. Layout

```
┌ topbar (48px) ───────────────────────────────────────────────┐
│ Stigmergic / Breadcrumb                      ● System ready  │
├ sidebar (232px) ─┬ main ────────────────────────────────────┤
│ WORK             │                                           │
│  New task        │  Page content (scrolls)                   │
│  Tasks ●         │                                           │
│ EVALUATE         │                                           │
│  Tests Runs …    │                                           │
│ OBSERVE          │                                           │
│  Operations      │                                           │
│  Agents          │                                           │
│  Analytics       │                                           │
│ CONFIGURE        │                                           │
│  Settings        │                                           │
│ Agents online 3/3│                                           │
└──────────────────┴───────────────────────────────────────────┘
```

- The top bar holds the product name, one breadcrumb, and the **system status** button. The button opens a popover with the readiness checks, provider credentials, a test-task action, and diagnostics. Nothing else lives in the top bar.
- The sidebar has four groups: Work, Evaluate, Observe, Configure. The Tasks link shows an amber count when tasks need attention and a pulsing dot when tasks run.
- Under 1024px the sidebar becomes a drawer. Under 640px pages use single-column layouts.

### 3.1 Page header

Every list page starts with the same header: an eyebrow (`.page-eyebrow`, the group name), an `h2` title, an optional one-sentence lede, and primary actions on the right.

## 4. Components

Shared components live in `mission-control/src/components/ui`. Their styles live in `ui.css`.

| Component | File | Notes |
|:--|:--|:--|
| Button | `.button`, `.button--primary`, `.button--danger`, `.button--danger-ghost`, `.button--active` | 36px tall, 8px radius |
| SelectMenu | `SelectMenu.tsx` | Listbox in a portal. Keyboard: arrows, Home/End, type-ahead, Escape. `variant="pill"` for inline pickers |
| Select | `Select.tsx` | Drop-in for a native `<select>`; accepts `<option>` children |
| Status pill | `.task-state-label`, `.agent-status`, `.benchmark-status`, `.settings-state` | Status color on its tint |
| Chip | `.tasks-chip` | Status views on Tasks; saved views with a delete affordance on hover |
| Count badge | `.tasks-chip__count`, `.task-sidebar__count` | Amber for attention counts |
| Card | `.agent-card`, `.settings-card`, `.dataset-card` | `--shadow-card`, 10px radius |
| Table | `.tasks-table`, `.settings-table`, `.benchmark-table` | Uppercase 10–12px headers, row hover on `--surface-hover` |
| Empty / error state | `ResourceState.tsx` | Centered for `empty`; icon left for errors with details and retry |
| Toast | `Toast.tsx` | Bottom right; success and error only |
| Dialog | `SettingsChangeDialog.tsx` | Native `<dialog>` with a change list |

### 4.1 Composer (home)

One card with a growing text area, an attach button, a runtime pill, and a round send button. Enter sends (or ⌘ Enter, a Workspace setting). Attachments appear as chips above the text. Drag-and-drop covers the card. The only text outside the card is a one-line notice for blockers.

### 4.2 Settings controls

`src/components/settings/controls.tsx` provides `SettingsRow`, `Toggle`, `NumberField`, `TextField`, `SegmentedControl`, and `SettingsCard`.

- A row is label and description on the left, the control on the right (stacked under 640px).
- A **Session** pill marks a saved value that differs from `bmas.yaml`. An **Unsaved** pill marks a draft change. Rows with a draft change get a faint amber background.
- Enum values with three options or fewer use a segmented control; more use SelectMenu.
- The save bar is fixed at the bottom. It lists every change (before → after) and applies them with one Save.

### 4.3 Live task surfaces

- The task header shows the status pill, runtime chip, cost, elapsed time, phase, and active agent. Stage markers (Queued → Running → Completed) sit under it. Operator actions sit on the right.
- Tabs (Summary, Blackboard, Execution, Logs, Files) are buttons inside one page; the active tab lives in `?tab=`. Switching tabs never reloads the page.
- The live dashboard uses a pulsing dot, a `LIVE` label, and metric tiles with tabular numbers.

## 5. States

| State | Treatment |
|:--|:--|
| Loading (first paint) | `Skeleton` inside the target card. No page-level loading boundary — it flickers on navigation. |
| Empty | `ResourceState kind="empty"`, centered, one sentence that says what to do |
| Unavailable / error | `ResourceState` with detail, diagnostics copy, Retry, and an Operations link |
| Live | Pulse dot plus the `LIVE` label; never a spinner |
| Unsaved | Amber pill and row tint; the save bar counts the changes |
| Session override | Blue `Session` pill with a one-click reset to the `bmas.yaml` value |

## 6. Accessibility

- Every control has an accessible name. Icon-only buttons use `aria-label`.
- SelectMenu is a `combobox` + `listbox`; tabs use `role="tablist"`; toggles use `role="switch"`; segmented controls use `radiogroup`.
- Focus trap in popovers and dialogs (`useFocusTrap`). Escape closes them and returns focus.
- Color never carries meaning alone: status pills include text, rings include labels.
- Minimum control height is 36px on desktop and 44px under 768px.

## 7. File map

```
mission-control/src/app/
  globals.css          tokens, reset, focus ring, utilities
  ui.css               composer, select menu, tasks workspace, agents, settings, evaluate polish
  views.css            older page-specific rules (task detail, blackboard, files, benchmarks)
  ClientShell.tsx      top bar, sidebar, providers, preferences
  LandingPageClient.tsx   composer
  tasks/TasksPageClient.tsx
  task/[taskId]/TaskDetailClient.tsx + panels/   one-page task detail with tabs
  agents/ + agents/[role]/
  settings/SettingsPageClient.tsx
mission-control/src/components/
  layout/TopBar.tsx, layout/SystemStatusPanel.tsx
  ui/SelectMenu.tsx, ui/Select.tsx, ui/ResourceState.tsx, ui/Toast.tsx
  settings/controls.tsx
mission-control/src/lib/
  preferences.ts       browser-local settings
  settings-presentation.ts   settings draft, diff, YAML patch
```

## 8. Checklist for a new surface

1. Use a surface from the ladder and `--shadow-card` for any card.
2. Use `.page-header` with an eyebrow, title, and lede.
3. Use SelectMenu or Select for any choice. Never render a native `<select>`.
4. Give every list an empty state and an error state through `ResourceState`.
5. Put numbers in tabular figures and identifiers in mono.
6. Test at 1440px and at 390px. Test keyboard navigation through every control.
