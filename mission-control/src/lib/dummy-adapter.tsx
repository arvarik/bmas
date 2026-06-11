/**
 * Dummy Adapter — acceptance test for the panel registry (doc 08 §2.1).
 *
 * Proves that a variant can register a panel with ZERO edits outside
 * variants.ts + this file.  This file is only loaded in development
 * (via the conditional require in variants.ts).
 *
 * To verify:
 *   1. `npm run dev`
 *   2. Navigate to any task detail page
 *   3. The dummy panel should appear in the panel list
 *   4. No other files were modified
 */

"use client";

import React from "react";
import { registerAdapter } from "./variants";
import { Puzzle } from "lucide-react";
import { Panel } from "@/components/ui/Panel";
import type { VariantUIAdapter } from "./variants";

function DummyPanel({ taskId }: { taskId: string }) {
  return (
    <Panel title="Dummy Adapter Panel" subtitle="Acceptance test — doc 08 §2.1">
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-3)",
          padding: "var(--space-4)",
          color: "var(--text-secondary)",
          fontSize: "var(--text-sm)",
        }}
      >
        <p>
          This panel was injected by the <code>dummy</code> adapter with zero
          edits outside <code>variants.ts</code> and{" "}
          <code>dummy-adapter.ts</code>.
        </p>
        <p style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>
          Task: {taskId}
        </p>
      </div>
    </Panel>
  );
}

const dummyAdapter: VariantUIAdapter = {
  name:           "dummy",
  label:          "Dummy (Dev Test)",
  nodeTypes:      [],
  edgeSpecs:      [],
  missionPanels:  [
    {
      id:        "dummy-panel",
      label:     "Dummy Panel",
      icon:      Puzzle,
      component: DummyPanel,
    },
  ],
  composerExtras: null,
};

registerAdapter(dummyAdapter);
