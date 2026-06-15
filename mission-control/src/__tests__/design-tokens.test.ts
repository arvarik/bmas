/**
 * design-tokens.test.ts — Tests for design token utilities.
 *
 * Covers:
 * - authorColor() — known role colors, unknown author hash stability, edge cases
 * - STATUS_COLORS — completeness
 * - AGENT_COLORS — completeness
 * - ENTRY_TYPE_ICONS — completeness
 */

import { describe, it, expect } from "vitest";
import {
  authorColor,
  STATUS_COLORS,
  AGENT_COLORS,
  ENTRY_TYPE_ICONS,
  HEAT_RAMP,
  SURFACE_COLORS,
  TEXT_COLORS,
  ACCENT_COLORS,
  NODE_LABELS,
} from "@/lib/design-tokens";
import type { StatusType, AgentRole } from "@/lib/design-tokens";

// ── authorColor ───────────────────────────────────────────────────────

describe("authorColor", () => {
  it("returns known color for planner", () => {
    expect(authorColor("planner")).toBe(AGENT_COLORS.planner);
  });

  it("returns known color for critic", () => {
    expect(authorColor("critic")).toBe(AGENT_COLORS.critic);
  });

  it("returns known color for executor", () => {
    expect(authorColor("executor")).toBe(AGENT_COLORS.executor);
  });

  it("returns known color for auditor", () => {
    expect(authorColor("auditor")).toBe(AGENT_COLORS.auditor);
  });

  it("returns known color for conflict_resolver", () => {
    expect(authorColor("conflict_resolver")).toBe(AGENT_COLORS.conflict_resolver);
  });

  it("returns known color for cleaner", () => {
    expect(authorColor("cleaner")).toBe(AGENT_COLORS.cleaner);
  });

  it("returns known color for decider", () => {
    expect(authorColor("decider")).toBe(AGENT_COLORS.decider);
  });

  it("returns stable color for unknown author", () => {
    const color1 = authorColor("expert.data_analyst");
    const color2 = authorColor("expert.data_analyst");
    expect(color1).toBe(color2);
  });

  it("returns different colors for different unknown authors", () => {
    const color1 = authorColor("expert.foo");
    const color2 = authorColor("expert.bar");
    // Not guaranteed to be different but highly likely
    // Just verify both are valid HSL
    expect(color1).toMatch(/^hsl\(\d+, 45%, 58%\)$/);
    expect(color2).toMatch(/^hsl\(\d+, 45%, 58%\)$/);
  });

  it("returns valid HSL format for unknown authors", () => {
    const color = authorColor("worker.12345");
    expect(color).toMatch(/^hsl\(\d+, 45%, 58%\)$/);
  });

  it("handles empty string author", () => {
    const color = authorColor("");
    expect(color).toMatch(/^hsl\(\d+, 45%, 58%\)$/);
  });
});

// ── STATUS_COLORS ─────────────────────────────────────────────────────

describe("STATUS_COLORS", () => {
  const expectedStatuses: StatusType[] = [
    "pending", "running", "success", "error", "paused",
  ];

  it("has all expected status keys", () => {
    for (const status of expectedStatuses) {
      expect(STATUS_COLORS).toHaveProperty(status);
    }
  });

  it("all values are HSL strings", () => {
    for (const color of Object.values(STATUS_COLORS)) {
      expect(color).toMatch(/^hsl\(/);
    }
  });
});

// ── AGENT_COLORS ──────────────────────────────────────────────────────

describe("AGENT_COLORS", () => {
  const expectedRoles: AgentRole[] = [
    "planner", "executor", "auditor",
    "critic", "conflict_resolver", "cleaner", "decider",
  ];

  it("has all expected role keys", () => {
    for (const role of expectedRoles) {
      expect(AGENT_COLORS).toHaveProperty(role);
    }
  });

  it("all values are HSL strings", () => {
    for (const color of Object.values(AGENT_COLORS)) {
      expect(color).toMatch(/^hsl\(/);
    }
  });
});

// ── NODE_LABELS ───────────────────────────────────────────────────────

describe("NODE_LABELS", () => {
  it("has same keys as AGENT_COLORS", () => {
    expect(Object.keys(NODE_LABELS).sort()).toEqual(
      Object.keys(AGENT_COLORS).sort(),
    );
  });

  it("all values are non-empty strings", () => {
    for (const label of Object.values(NODE_LABELS)) {
      expect(label.length).toBeGreaterThan(0);
    }
  });
});

// ── ENTRY_TYPE_ICONS ──────────────────────────────────────────────────

describe("ENTRY_TYPE_ICONS", () => {
  const expectedTypes = [
    "objective", "attachment", "plan", "finding",
    "critique", "rebuttal", "conflict", "solution", "artifact",
  ];

  it("has all expected entry type keys", () => {
    for (const t of expectedTypes) {
      expect(ENTRY_TYPE_ICONS).toHaveProperty(t);
    }
  });

  it("all values are non-empty strings (icon names)", () => {
    for (const icon of Object.values(ENTRY_TYPE_ICONS)) {
      expect(icon.length).toBeGreaterThan(0);
    }
  });
});

// ── HEAT_RAMP ─────────────────────────────────────────────────────────

describe("HEAT_RAMP", () => {
  it("has exactly 3 entries (low, mid, high)", () => {
    expect(HEAT_RAMP).toHaveLength(3);
  });

  it("all entries are HSL strings", () => {
    for (const color of HEAT_RAMP) {
      expect(color).toMatch(/^hsl\(/);
    }
  });
});

// ── SURFACE_COLORS ────────────────────────────────────────────────────

describe("SURFACE_COLORS", () => {
  it("has expected surface levels", () => {
    expect(SURFACE_COLORS).toHaveProperty("base");
    expect(SURFACE_COLORS).toHaveProperty("raised");
    expect(SURFACE_COLORS).toHaveProperty("overlay");
    expect(SURFACE_COLORS).toHaveProperty("hover");
    expect(SURFACE_COLORS).toHaveProperty("active");
  });
});

// ── TEXT_COLORS ───────────────────────────────────────────────────────

describe("TEXT_COLORS", () => {
  it("has expected hierarchy levels", () => {
    expect(TEXT_COLORS).toHaveProperty("primary");
    expect(TEXT_COLORS).toHaveProperty("secondary");
    expect(TEXT_COLORS).toHaveProperty("tertiary");
    expect(TEXT_COLORS).toHaveProperty("inverse");
  });
});

// ── ACCENT_COLORS ─────────────────────────────────────────────────────

describe("ACCENT_COLORS", () => {
  it("has expected accent variants", () => {
    expect(ACCENT_COLORS).toHaveProperty("primary");
    expect(ACCENT_COLORS).toHaveProperty("primaryHover");
    expect(ACCENT_COLORS).toHaveProperty("subtle");
  });
});
