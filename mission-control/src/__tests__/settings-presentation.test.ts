import { describe, expect, it } from "vitest";
import {
  buildYamlPatch,
  getResetChanges,
  type SettingsSnapshot,
} from "@/lib/settings-presentation";

const defaults: SettingsSnapshot = {
  routing: {
    simple: "fast-model",
    complex: "careful-model",
  },
  role_registry: {
    planner: {
      preferred_host: null,
      profile: "planner",
      dispatch_port: 8000,
    },
  },
  defaults: {
    routing: {
      simple: "fast-model",
      complex: "careful-model",
    },
    role_registry: {
      planner: {
        preferred_host: null,
        profile: "planner",
        dispatch_port: 8000,
      },
    },
  },
};

describe("settings presentation", () => {
  it("reports no reset changes for YAML defaults", () => {
    expect(getResetChanges(defaults)).toEqual([]);
    expect(buildYamlPatch(defaults)).toBe(
      "# Merge these session overrides into bmas.yaml, then restart bMAS."
    );
  });

  it("builds a YAML patch for active routing and role overrides", () => {
    const settings: SettingsSnapshot = {
      ...defaults,
      routing: { ...defaults.routing, complex: "large-model" },
      role_registry: {
        planner: {
          preferred_host: "agent-a",
          profile: "lead-planner",
          dispatch_port: 8100,
        },
      },
    };

    expect(buildYamlPatch(settings)).toBe(
      [
        "# Merge these session overrides into bmas.yaml, then restart bMAS.",
        "routing:",
        '  complex: "large-model"',
        "coordination:",
        "  role_registry:",
        "    planner:",
        '      preferred_host: "agent-a"',
        '      profile: "lead-planner"',
        "      dispatch_port: 8100",
      ].join("\n")
    );
    expect(getResetChanges(settings)).toHaveLength(4);
  });

  it("shows that reset removes roles without YAML defaults", () => {
    const settings: SettingsSnapshot = {
      ...defaults,
      role_registry: {
        ...defaults.role_registry,
        custom: {
          preferred_host: null,
          profile: "custom",
          dispatch_port: 8000,
        },
      },
    };

    expect(getResetChanges(settings)).toContainEqual({
      label: "custom role",
      before: "custom on any host:8000",
      after: "Removed",
    });
  });
});

describe("settings draft validation", () => {
  const fields = [
    { key: "max_rounds", label: "Maximum rounds", type: "integer" as const, group: "limits" as const, description: "", min: 1, max: 50 },
    { key: "budget_ceiling_usd", label: "Budget", type: "number" as const, group: "limits" as const, description: "", min: 0.01, max: 1000, unit: "USD" },
    { key: "experts_per_tier", label: "Experts", type: "tier_map" as const, group: "roster" as const, description: "", min: 0, max: 12 },
    { key: "cu_mode", label: "Mode", type: "enum" as const, group: "control" as const, description: "", options: ["llm", "heuristic_first"] },
  ];
  const snapshot = {
    routing: { simple: "local" },
    role_registry: { planner: { preferred_host: null, profile: "planner", dispatch_port: 8000 } },
    classic: { max_rounds: 4, budget_ceiling_usd: 0.5, experts_per_tier: { simple: 0 }, cu_mode: "llm" },
    defaults: { routing: { simple: "local" }, role_registry: { planner: { preferred_host: null, profile: "planner", dispatch_port: 8000 } }, classic: { max_rounds: 4 } },
  };

  it("accepts a valid draft", async () => {
    const { draftFromSnapshot, validateDraft } = await import("@/lib/settings-presentation");
    expect(validateDraft(draftFromSnapshot(snapshot), fields)).toEqual([]);
  });

  it("reports out-of-range, fractional, empty, and enum problems with row keys", async () => {
    const { draftFromSnapshot, validateDraft } = await import("@/lib/settings-presentation");
    const draft = draftFromSnapshot(snapshot);
    draft.classic.max_rounds = 0;
    draft.classic.experts_per_tier = { simple: 1.5 };
    draft.classic.cu_mode = "random";
    draft.role_registry.planner.profile = " ";
    draft.role_registry.planner.dispatch_port = 70000;
    draft.routing.simple = "";
    const keys = validateDraft(draft, fields).map((issue) => `${issue.section}.${issue.key}`).sort();
    expect(keys).toEqual([
      "classic.cu_mode",
      "classic.experts_per_tier",
      "classic.max_rounds",
      "role_registry.planner.dispatch_port",
      "role_registry.planner.profile",
      "routing.simple",
    ]);
  });

  it("carries unsaved changes onto a refreshed snapshot", async () => {
    const { carryDraftChanges, draftFromSnapshot } = await import("@/lib/settings-presentation");
    const draft = draftFromSnapshot(snapshot);
    draft.classic.max_rounds = 9;
    draft.role_registry.planner.dispatch_port = 9000;
    const refreshed = { ...snapshot, classic: { ...snapshot.classic, budget_ceiling_usd: 0.75 } };
    const carried = carryDraftChanges(snapshot, draft, refreshed);
    expect(carried.classic.max_rounds).toBe(9);
    expect(carried.classic.budget_ceiling_usd).toBe(0.75);
    expect(carried.role_registry.planner.dispatch_port).toBe(9000);
  });
});
