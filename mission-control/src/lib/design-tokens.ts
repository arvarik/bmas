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

// ── Agent Identity Colors ────────────────────────────────────────────

export type AgentRole = "planner" | "executor" | "auditor";

export const AGENT_COLORS: Record<AgentRole, string> = {
  planner: "hsl(265, 50%, 60%)",
  executor: "hsl(175, 60%, 45%)",
  auditor: "hsl(32, 80%, 55%)",
} as const;

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
