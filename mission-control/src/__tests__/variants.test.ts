/**
 * variants.test.ts — Tests for the variant UI adapter registry.
 *
 * Covers:
 * - registerAdapter / getActiveAdapter / listAdapters
 * - Traditional adapter shape validation
 * - Fallback behavior for unknown variants
 */

import { describe, it, expect } from "vitest";
import {
  getActiveAdapter,
  listAdapters,
} from "@/lib/variants";

// ── Registry ──────────────────────────────────────────────────────────

describe("Variant Registry", () => {
  it("traditional adapter is registered by default", () => {
    const adapter = getActiveAdapter("traditional");
    expect(adapter).not.toBeNull();
    expect(adapter!.name).toBe("traditional");
  });

  it("traditional adapter has correct label", () => {
    const adapter = getActiveAdapter("traditional");
    expect(adapter!.label).toBe("Blackboard (bMAS)");
  });

  it("traditional adapter has node types", () => {
    const adapter = getActiveAdapter("traditional");
    expect(adapter!.nodeTypes.length).toBeGreaterThan(0);
  });

  it("traditional adapter has edge specs", () => {
    const adapter = getActiveAdapter("traditional");
    expect(adapter!.edgeSpecs.length).toBeGreaterThan(0);
  });

  it("traditional adapter node types cover core entry types", () => {
    const adapter = getActiveAdapter("traditional");
    const types = adapter!.nodeTypes.map((n) => n.entryType);
    expect(types).toContain("objective");
    expect(types).toContain("finding");
    expect(types).toContain("critique");
    expect(types).toContain("solution");
  });

  it("traditional adapter edge specs cover core ref types", () => {
    const adapter = getActiveAdapter("traditional");
    const refs = adapter!.edgeSpecs.map((e) => e.refType);
    expect(refs).toContain("supports");
    expect(refs).toContain("critiques");
    expect(refs).toContain("rebuts");
    expect(refs).toContain("resolves");
  });

  it("unknown variant falls back to traditional", () => {
    const adapter = getActiveAdapter("nonexistent");
    expect(adapter).not.toBeNull();
    expect(adapter!.name).toBe("traditional");
  });

  it("listAdapters returns at least the traditional adapter", () => {
    const adapters = listAdapters();
    expect(adapters.length).toBeGreaterThanOrEqual(1);
    expect(adapters.some((a) => a.name === "traditional")).toBe(true);
  });

  it("node type specs have required properties", () => {
    const adapter = getActiveAdapter("traditional");
    for (const nt of adapter!.nodeTypes) {
      expect(nt.entryType).toBeTruthy();
      expect(nt.icon).toBeTruthy();
      expect(nt.className).toBeTruthy();
      expect(typeof nt.showInLegend).toBe("boolean");
    }
  });

  it("edge specs have required properties", () => {
    const adapter = getActiveAdapter("traditional");
    for (const es of adapter!.edgeSpecs) {
      expect(es.refType).toBeTruthy();
      expect(["solid", "dashed", "dotted"]).toContain(es.stroke);
      expect(typeof es.animated).toBe("boolean");
    }
  });
});
