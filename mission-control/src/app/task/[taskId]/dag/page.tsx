"use client";

/**
 * Graph Tab — /task/[taskId]/dag
 *
 * Renders the actual execution graph built from agent turn records.
 * Each turn (agent activation) becomes a node grouped by round number.
 * Agent roles and models are discovered dynamically from turn data.
 *
 * This replaces the old static sub_task placeholder graph which always
 * showed 4 hardcoded nodes (triage/plan/execute/audit) that never updated.
 */

import dynamic from "next/dynamic";
import { useTaskData } from "../TaskStreamContext";
import { Skeleton } from "@/components/ui/Skeleton";
import { Panel } from "@/components/ui/Panel";

const TurnGraph = dynamic(
  () => import("@/components/features/TurnGraph").then((m) => m.TurnGraph),
  {
    ssr: false,
    loading: () => (
      <div style={{
        height: "100%", display: "flex", alignItems: "center",
        justifyContent: "center", background: "var(--surface-overlay)",
        borderRadius: "var(--radius-lg)",
      }}>
        <Skeleton variant="dag" />
      </div>
    ),
  }
);

export default function DAGPage() {
  const { activeTurns, completedTurns, isLive, coordinatorNarrations } = useTaskData();

  const totalTurns = activeTurns.length + completedTurns.length;
  const rounds = new Set(
    [...activeTurns, ...completedTurns].map((t) => t.round_no ?? 0),
  );
  const actors = [...new Set([...activeTurns, ...completedTurns].map((t) => t.actor))];
  const subtitle = totalTurns > 0
    ? `${totalTurns} turn${totalTurns !== 1 ? "s" : ""} across ${rounds.size} round${
        rounds.size !== 1 ? "s" : ""
      } · ${actors.length} agent${actors.length !== 1 ? "s" : ""} · click a node for who/what/when/why`
    : isLive
    ? "Waiting for agent turns…"
    : "No turn data";

  return (
    <div className="view-container dag-view" style={{ overflow: "hidden" }}>
      <div className="dag-layout">
        <div className="dag-canvas">
          <Panel
            title="Execution Graph"
            subtitle={subtitle}
          >
            <TurnGraph
              activeTurns={activeTurns}
              completedTurns={completedTurns}
              isLive={isLive}
              narrations={coordinatorNarrations}
            />
          </Panel>
        </div>
      </div>
    </div>
  );
}
