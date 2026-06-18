/**
 * ProcessFlowGraph.test.ts — unit tests for the buildFlowGraph function.
 *
 * Validates that the graph builder correctly constructs round nodes,
 * forward edges, and cycle-back edges from real turn data patterns.
 */

import { describe, it, expect } from "vitest";
import { buildFlowGraph } from "@/components/features/ProcessFlowGraph";
import type { TurnRecord, CoordinatorNarration } from "@/hooks/useTaskStream";

// ── Helpers ────────────────────────────────────────────────────────────────────

function makeTurn(overrides: Partial<TurnRecord> & { actor: string; round_no: number }): TurnRecord {
  return {
    turn_id: `turn-${Math.random().toString(36).slice(2)}`,
    task_id: "task-test",
    round_no: overrides.round_no,
    role: overrides.role ?? (overrides.actor.includes(".") ? "expert" : overrides.actor),
    actor: overrides.actor,
    status: overrides.status ?? "completed",
    node: "http://node-1:8000",
    model: "gemini-pro",
    started_at: new Date(Date.now() + overrides.round_no * 10000).toISOString(),
    ended_at: new Date(Date.now() + overrides.round_no * 10000 + 5000).toISOString(),
    ...overrides,
  } as TurnRecord;
}

// ── Tests ──────────────────────────────────────────────────────────────────────

describe("buildFlowGraph", () => {
  it("returns empty layout when no turns", () => {
    const result = buildFlowGraph([], []);
    expect(result.nodes).toHaveLength(0);
    expect(result.edges).toHaveLength(0);
    expect(result.cycleTargets.size).toBe(0);
  });

  it("builds one node per unique round number", () => {
    const turns = [
      makeTurn({ actor: "planner", round_no: 1, phase: "discovery" }),
      makeTurn({ actor: "expert.security_expert", round_no: 1, phase: "discovery" }),
      makeTurn({ actor: "critic", round_no: 2, phase: "debate" }),
      makeTurn({ actor: "decider", round_no: 3, phase: "convergence" }),
    ];
    const { nodes } = buildFlowGraph(turns, []);
    expect(nodes).toHaveLength(3);
    expect(nodes[0].round).toBe(1);
    expect(nodes[1].round).toBe(2);
    expect(nodes[2].round).toBe(3);
  });

  it("captures all unique actors in correct order within a round", () => {
    const turns = [
      makeTurn({ actor: "planner", round_no: 1, phase: "discovery", started_at: "2024-01-01T10:00:00Z" }),
      makeTurn({ actor: "expert.foo", round_no: 1, phase: "discovery", started_at: "2024-01-01T10:00:01Z" }),
      makeTurn({ actor: "expert.bar", round_no: 1, phase: "discovery", started_at: "2024-01-01T10:00:02Z" }),
    ];
    const { nodes } = buildFlowGraph(turns, []);
    expect(nodes[0].actors).toEqual(["planner", "expert.foo", "expert.bar"]);
  });

  it("creates forward edges between consecutive rounds", () => {
    const turns = [
      makeTurn({ actor: "planner", round_no: 1, phase: "discovery" }),
      makeTurn({ actor: "expert.foo", round_no: 2, phase: "debate" }),
      makeTurn({ actor: "decider", round_no: 3, phase: "convergence" }),
    ];
    const { edges } = buildFlowGraph(turns, []);
    const fwdEdges = edges.filter((e) => !e.isCycle);
    expect(fwdEdges).toHaveLength(2);
    expect(fwdEdges[0]).toMatchObject({ from: 0, to: 1, isCycle: false });
    expect(fwdEdges[1]).toMatchObject({ from: 1, to: 2, isCycle: false });
  });

  it("detects cycle edge when debate round revisits earlier expert actors (task-27f98eae pattern)", () => {
    // R1: Discovery — planner + 3 experts
    // R2: Debate — IRC expert + critic
    // R3: Debate — same 3 experts + IRC (revisit: cycle to R1)
    // R4: Convergence — decider
    const experts = ["expert.api_security_architect", "expert.iam_security_specialist", "expert.secops_detection_lead"];
    const turns = [
      makeTurn({ actor: "planner", round_no: 1, phase: "discovery" }),
      ...experts.map((a) => makeTurn({ actor: a, round_no: 1, phase: "discovery" })),
      makeTurn({ actor: "expert.incident_response_commander", round_no: 2, phase: "debate" }),
      makeTurn({ actor: "critic", round_no: 2, phase: "debate" }),
      ...experts.map((a) => makeTurn({ actor: a, round_no: 3, phase: "debate" })),
      makeTurn({ actor: "expert.incident_response_commander", round_no: 3, phase: "debate" }),
      makeTurn({ actor: "decider", round_no: 4, phase: "convergence" }),
    ];
    const { nodes, edges, cycleTargets } = buildFlowGraph(turns, []);
    expect(nodes).toHaveLength(4);

    const cycleEdges = edges.filter((e) => e.isCycle);
    expect(cycleEdges.length).toBeGreaterThan(0);
    expect(cycleTargets.size).toBeGreaterThan(0);
  });

  it("uses coordinator narration as rationale for a round", () => {
    const turns = [
      makeTurn({ actor: "planner", round_no: 1, phase: "discovery" }),
    ];
    const narrations: CoordinatorNarration[] = [
      { round: 1, rationale: "Start the analysis.", model: "gemini-pro" },
    ];
    const { nodes } = buildFlowGraph(turns, narrations);
    expect(nodes[0].rationale).toBe("Start the analysis.");
  });

  it("picks dominant phase when a round has mixed phases", () => {
    const turns = [
      makeTurn({ actor: "expert.a", round_no: 1, phase: "debate" }),
      makeTurn({ actor: "expert.b", round_no: 1, phase: "debate" }),
      makeTurn({ actor: "critic", round_no: 1, phase: "critique" }),
    ];
    const { nodes } = buildFlowGraph(turns, []);
    expect(nodes[0].phase).toBe("debate");
  });

  it("sets status to running when any turn in round is active", () => {
    const turns = [
      makeTurn({ actor: "planner", round_no: 1, phase: "discovery", status: "completed" }),
      makeTurn({ actor: "expert.foo", round_no: 1, phase: "discovery", status: "running" }),
    ];
    const { nodes } = buildFlowGraph(turns, []);
    expect(nodes[0].status).toBe("running");
  });

  it("sets status to failed when any turn in round failed", () => {
    const turns = [
      makeTurn({ actor: "planner", round_no: 1, phase: "discovery", status: "failed" }),
    ];
    const { nodes } = buildFlowGraph(turns, []);
    expect(nodes[0].status).toBe("failed");
  });

  it("correctly counts turns per round", () => {
    const turns = [
      makeTurn({ actor: "expert.a", round_no: 1, phase: "debate" }),
      makeTurn({ actor: "expert.b", round_no: 1, phase: "debate" }),
      makeTurn({ actor: "expert.c", round_no: 1, phase: "debate" }),
    ];
    const { nodes } = buildFlowGraph(turns, []);
    expect(nodes[0].turnCount).toBe(3);
  });

  it("handles a single-round task with no edges", () => {
    const turns = [makeTurn({ actor: "decider", round_no: 1, phase: "convergence" })];
    const { nodes, edges } = buildFlowGraph(turns, []);
    expect(nodes).toHaveLength(1);
    expect(edges).toHaveLength(0);
  });
});
