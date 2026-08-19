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
