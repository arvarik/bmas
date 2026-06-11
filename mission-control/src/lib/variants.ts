/**
 * Variant UI Adapter Registry (doc 08 §2.1)
 *
 * Each coordination variant registers its UI adapter here.
 * The adapter defines: graph node types, edge mappings, extra panels,
 * and event handlers for the blackboard view.
 *
 * Seam rule 8: "The UI is registry-driven: variant dropdown options
 * come from the daemon's capabilities endpoint, and each variant
 * registers its panels/graph adapters instead of being hard-wired
 * into Mission Control."
 */

import type { LucideIcon } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

/** Describes how an entry type renders as a graph node. */
export interface NodeTypeSpec {
  /** Entry type this spec matches (e.g., "finding", "critique"). */
  entryType: string;
  /** Lucide icon name (must exist in the imported set). */
  icon: string;
  /** CSS class suffix for the node wrapper (e.g., "finding"). */
  className: string;
  /** Whether this node type is shown in the graph legend. */
  showInLegend: boolean;
}

/** Describes how a ref-edge renders between two nodes. */
export interface EdgeSpec {
  /** The relationship type from the entry's `refs` array. */
  refType: string;
  /** CSS stroke style: "solid" | "dashed" | "dotted". */
  stroke: "solid" | "dashed" | "dotted";
  /** Whether the edge is animated (marching ants). */
  animated: boolean;
  /** Edge label (optional). */
  label?: string;
}

/** A panel specification that a variant can inject into the UI. */
export interface PanelSpec {
  /** Unique panel ID. */
  id: string;
  /** Display label for the panel tab. */
  label: string;
  /** Lucide icon for the panel tab. */
  icon: LucideIcon;
  /** React component to mount in the panel. */
  component: React.ComponentType<{ taskId: string }>;
}

/** The full adapter interface every variant must satisfy. */
export interface VariantUIAdapter {
  /** Variant name (matches daemon's variant string). */
  name: string;
  /** Human-readable label for the UI. */
  label: string;
  /** Node type specs for the blackboard graph. */
  nodeTypes: NodeTypeSpec[];
  /** Edge mapping specs for refs-based connections. */
  edgeSpecs: EdgeSpec[];
  /** Extra mission panels this variant registers (beyond core tabs). */
  missionPanels: PanelSpec[];
  /** Extra fields to render on the composer (future). */
  composerExtras: React.ComponentType | null;
}

// ── Registry ──────────────────────────────────────────────────────────

const VARIANT_ADAPTERS: Record<string, VariantUIAdapter> = {};

/**
 * Register a variant UI adapter.
 * Call at import time from the adapter module.
 */
export function registerAdapter(adapter: VariantUIAdapter): void {
  VARIANT_ADAPTERS[adapter.name] = adapter;
}

/**
 * Get the active variant's adapter.
 * Falls back to "traditional" if the requested variant is unknown.
 */
export function getActiveAdapter(variantId: string): VariantUIAdapter | null {
  return VARIANT_ADAPTERS[variantId] ?? VARIANT_ADAPTERS["traditional"] ?? null;
}

/** List all registered variant adapters. */
export function listAdapters(): VariantUIAdapter[] {
  return Object.values(VARIANT_ADAPTERS);
}

// ── Traditional Adapter (doc 08 §2.1, doc 05) ────────────────────────

const TRADITIONAL_NODE_TYPES: NodeTypeSpec[] = [
  { entryType: "objective",  icon: "Target",              className: "objective",  showInLegend: true  },
  { entryType: "attachment", icon: "Paperclip",           className: "attachment", showInLegend: true  },
  { entryType: "plan",       icon: "ListTree",            className: "plan",       showInLegend: true  },
  { entryType: "finding",    icon: "Lightbulb",           className: "finding",    showInLegend: true  },
  { entryType: "critique",   icon: "AlertTriangle",       className: "critique",   showInLegend: true  },
  { entryType: "rebuttal",   icon: "MessageSquareReply",  className: "rebuttal",   showInLegend: true  },
  { entryType: "conflict",   icon: "GitMerge",            className: "conflict",   showInLegend: true  },
  { entryType: "solution",   icon: "CheckCircle2",        className: "solution",   showInLegend: true  },
  { entryType: "artifact",   icon: "FileCode2",           className: "artifact",   showInLegend: true  },
];

const TRADITIONAL_EDGE_SPECS: EdgeSpec[] = [
  { refType: "supports",   stroke: "solid",   animated: false, label: "supports"   },
  { refType: "critiques",  stroke: "dashed",  animated: false, label: "critiques"  },
  { refType: "rebuts",     stroke: "dotted",  animated: false, label: "rebuts"     },
  { refType: "conflicts",  stroke: "dashed",  animated: true,  label: "conflicts"  },
  { refType: "resolves",   stroke: "solid",   animated: false, label: "resolves"   },
  { refType: "refines",    stroke: "solid",   animated: false, label: "refines"    },
  { refType: "attachment", stroke: "dotted",  animated: false                       },
];

registerAdapter({
  name:           "traditional",
  label:          "Blackboard (bMAS)",
  nodeTypes:      TRADITIONAL_NODE_TYPES,
  edgeSpecs:      TRADITIONAL_EDGE_SPECS,
  missionPanels:  [],
  composerExtras: null,
});

// ── Dev-only: dummy adapter (doc 08 §2.1 acceptance test) ────────────
// Loaded conditionally so it never ships to production.
if (process.env.NODE_ENV === "development") {
  try {
    // Dynamic import so the file is only required in dev
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    require("./dummy-adapter.tsx");
  } catch {
    // dummy-adapter may not exist yet — that's fine
  }
}
