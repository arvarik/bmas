/**
 * Design Tokens — Programmatic Access
 *
 * These constants mirror the CSS custom properties in globals.css.
 * Use them where CSS variables aren't practical: React Flow node
 * coloring, Recharts chart segments, canvas drawing, etc.
 */

// ── Status Colors ────────────────────────────────────────────────────

export type StatusType = "pending" | "running" | "success" | "error" | "paused";

export const STATUS_COLORS: Record<StatusType, string> = {
  pending: "hsl(220, 15%, 50%)",
  running: "hsl(217, 91%, 60%)",
  success: "hsl(142, 71%, 45%)",
  error: "hsl(0, 84%, 60%)",
  paused: "hsl(38, 92%, 50%)",
} as const;

// ── Agent Identity Colors (doc 08 §8) ────────────────────────────────
// NOTE: "executor"/"auditor" are LEGACY aliases (the paper has neither —
// doc 12 §2 / doc 04 §4). Kept so existing tasks/colors keep rendering;
// new roles use the paper-faithful set.
// Per seam rule 3 this enum is a *display convenience* — back it with the
// deterministic fallback color generator so dynamic authors render.

export type AgentRole =
  | "planner" | "executor" | "auditor"
  | "critic" | "conflict_resolver" | "cleaner" | "decider";

export const AGENT_COLORS: Record<AgentRole, string> = {
  planner:            "hsl(265, 50%, 60%)",
  executor:           "hsl(175, 60%, 45%)",
  auditor:            "hsl(32, 80%, 55%)",
  critic:             "hsl(350, 60%, 58%)",
  conflict_resolver:  "hsl(280, 45%, 58%)",
  cleaner:            "hsl(200, 25%, 55%)",
  decider:            "hsl(150, 45%, 50%)",
} as const;

/**
 * User-facing display labels for agent roles.
 * Internal keys (planner, executor, auditor) are kept for backend compatibility.
 * UI shows these human-friendly labels instead.
 */
export const NODE_LABELS: Record<AgentRole, string> = {
  planner:            "Node 1",
  executor:           "Node 2",
  auditor:            "Node 3",
  critic:             "Critic",
  conflict_resolver:  "Conflict Resolver",
  cleaner:            "Cleaner",
  decider:            "Decider",
} as const;

/**
 * Deterministic author-color fallback (doc 13 §7).
 *
 * Known roles → fixed AGENT_COLORS entry.
 * Unknown authors (expert.<slug>, worker.<id>, universal-<n>) →
 * stable HSL from a hash of the author string, with fixed S/L
 * matching the muted palette in DESIGN.md §2.5.
 */
export function authorColor(author: string): string {
  if (author in AGENT_COLORS) return AGENT_COLORS[author as AgentRole];
  let hash = 0;
  for (let i = 0; i < author.length; i++) {
    hash = ((hash << 5) - hash + author.charCodeAt(i)) | 0;
  }
  const hue = ((hash % 360) + 360) % 360;
  return `hsl(${hue}, 45%, 58%)`;
}

// ── Entry Type → Lucide Icon Name (doc 08 §3) ───────────────────────

export const ENTRY_TYPE_ICONS: Record<string, string> = {
  objective:  "Target",
  attachment: "Paperclip",
  plan:       "ListTree",
  finding:    "Lightbulb",
  critique:   "AlertTriangle",
  rebuttal:   "MessageSquareReply",
  conflict:   "GitMerge",
  solution:   "CheckCircle2",
  artifact:   "FileCode2",
} as const;

// ── Heat Ramp (doc 13 §7) ────────────────────────────────────────────
// Low→high intensity via existing status hues (blue→amber→red).
// Used for salience encoding and the future stigmergic pressure overlay.

export const HEAT_RAMP = [
  "hsl(217, 91%, 60%)",  // low — accent blue
  "hsl(38, 92%, 50%)",   // mid — status paused/amber
  "hsl(0, 84%, 60%)",    // high — status error/red
] as const;

// ── Surface Colors ───────────────────────────────────────────────────

export const SURFACE_COLORS = {
  base: "hsl(222, 47%, 6%)",
  raised: "hsl(222, 44%, 9%)",
  overlay: "hsl(222, 40%, 12%)",
  hover: "hsl(222, 36%, 16%)",
  active: "hsl(222, 32%, 20%)",
} as const;

// ── Text Colors ──────────────────────────────────────────────────────

export const TEXT_COLORS = {
  primary: "hsl(210, 20%, 96%)",
  secondary: "hsl(215, 15%, 65%)",
  tertiary: "hsl(220, 10%, 45%)",
  inverse: "hsl(222, 47%, 6%)",
} as const;

// ── Accent Colors ────────────────────────────────────────────────────

export const ACCENT_COLORS = {
  primary: "hsl(217, 91%, 60%)",
  primaryHover: "hsl(217, 91%, 50%)",
  subtle: "hsl(217, 91%, 60%, 0.1)",
} as const;
