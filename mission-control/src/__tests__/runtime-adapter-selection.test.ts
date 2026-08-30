/**
 * Foundation Stage 0B: interface adapter selection uses the exact
 * runtime pair. An unknown pair renders through the registered generic
 * trace and artifact view, and no path selects an adapter for another
 * contract version.
 */
import { describe, expect, it } from "vitest";

import type { VariantCapability } from "@/lib/capabilities";
import {
  GENERIC_ADAPTER,
  getActiveAdapter,
  listAdapters,
  resolveAdapterPair,
  selectAdapterForCapability,
  visibleNavigationPanels,
} from "@/lib/variants";
import { resolveRuntime } from "@/hooks/useTaskStream";

function capability(overrides: Partial<VariantCapability>): VariantCapability {
  return {
    id: "classic",
    label: "Classic blackboard",
    available: true,
    contract_version: "1",
    aliases: [],
    features: {
      events: [],
      panels: ["mission", "logs", "artifacts"],
      graphs: ["turns"],
      controls: [],
      progress: [],
      result: [],
    },
    configuration_schema_version: "1",
    supports_recovery: true,
    required_agent_features: [],
    ...overrides,
  } as VariantCapability;
}

describe("exact pair adapter selection", () => {
  it("selects each known runtime pair exactly", () => {
    for (const id of ["classic", "patchboard", "stigmergic"]) {
      const adapter = getActiveAdapter(id, "1");
      expect(adapter?.id).toBe(id);
      expect(adapter?.generic).toBeUndefined();
    }
  });

  it("resolves an alias to one complete pair", () => {
    expect(resolveAdapterPair("traditional")).toEqual({
      id: "classic",
      contractVersion: "1",
    });
    expect(getActiveAdapter("traditional", "1")?.id).toBe("classic");
    expect(resolveAdapterPair("unheard-of")).toBeNull();
  });

  it("never selects an adapter for another contract version", () => {
    expect(getActiveAdapter("classic", "2")).toBeNull();
    expect(getActiveAdapter("patchboard", "0")).toBeNull();
    expect(getActiveAdapter("traditional", "2")).toBeNull();
  });

  it("keeps the generic view out of the runtime adapter list", () => {
    expect(listAdapters().map((adapter) => adapter.id)).toEqual([
      "classic",
      "patchboard",
      "stigmergic",
    ]);
  });
});

describe("generic interface fallback", () => {
  it("falls back to the generic view for an unknown pair", () => {
    const unknownPair = capability({
      id: "classic",
      contract_version: "2",
    });
    expect(selectAdapterForCapability(unknownPair)).toBe(GENERIC_ADAPTER);
    const unknownRuntime = capability({
      id: "surprise-runtime",
      label: "Surprise runtime",
    });
    expect(selectAdapterForCapability(unknownRuntime)).toBe(GENERIC_ADAPTER);
  });

  it("keeps the exact adapter for a supported pair", () => {
    const supported = capability({});
    expect(selectAdapterForCapability(supported).id).toBe("classic");
  });

  it("shows every generic panel for unknown feature names", () => {
    const foreign = capability({
      id: "surprise-runtime",
      features: {
        events: [],
        panels: ["holograms"],
        graphs: [],
        controls: [],
        progress: [],
        result: [],
      },
    });
    const panels = visibleNavigationPanels(GENERIC_ADAPTER, foreign);
    expect(panels.map((panel) => panel.id)).toEqual([
      "overview",
      "logs",
      "files",
    ]);
  });
});

describe("resolveRuntime routing boundary", () => {
  const document = {
    api_version: "1",
    variants: [
      capability({}),
      capability({
        id: "surprise-runtime",
        label: "Surprise runtime",
        contract_version: "7",
      }),
      capability({
        id: "offline-runtime",
        label: "Offline runtime",
        available: false,
        reason: "The runtime is not installed.",
      }),
    ],
  };

  it("keeps the exact adapter for a supported pair", () => {
    const { runtime, adapter } = resolveRuntime(document as never, "classic");
    expect(runtime.status).toBe("ready");
    expect(runtime.adapterId).toBe("classic");
    expect(adapter?.generic).toBeUndefined();
  });

  it("renders an unknown pair with the generic view", () => {
    const { runtime, adapter } = resolveRuntime(
      document as never,
      "surprise-runtime",
    );
    expect(runtime.status).toBe("ready");
    expect(runtime.adapterId).toBe("generic");
    expect(adapter).toBe(GENERIC_ADAPTER);
    expect(runtime.message).toContain("generic trace and artifact view");
    expect(getActiveAdapter(runtime.adapterId)).toBe(GENERIC_ADAPTER);
  });

  it("still fails closed for a runtime the daemon does not publish", () => {
    const { runtime, adapter } = resolveRuntime(
      document as never,
      "never-published",
    );
    expect(runtime.status).toBe("unsupported-variant");
    expect(adapter).toBeNull();
  });

  it("still fails closed for an unavailable runtime", () => {
    const { runtime, adapter } = resolveRuntime(
      document as never,
      "offline-runtime",
    );
    expect(runtime.status).toBe("variant-unavailable");
    expect(adapter).toBeNull();
  });
});
