import { describe, expect, it } from "vitest";
import { buildDelegationTree } from "@/components/features/DelegationTree";
import type { TraceEvent } from "@/hooks/useTaskStream";

function trace(
  type: "subagent_start" | "subagent_complete",
  data: Record<string, unknown>,
  seq: number,
): TraceEvent {
  return {
    id: `trace-${seq}`,
    turn_id: "turn-1",
    actor: "expert",
    type,
    content: "",
    data,
    seq,
    timestamp: `2026-08-18T00:00:0${seq}Z`,
  };
}

describe("Hermes delegation tree", () => {
  it("merges lifecycle events and nests child agents", () => {
    const roots = buildDelegationTree([
      trace("subagent_start", { subagent_id: "parent", status: "running" }, 1),
      trace("subagent_start", { subagent_id: "child", parent_id: "parent", depth: 1 }, 2),
      trace("subagent_complete", {
        subagent_id: "child",
        parent_id: "parent",
        status: "completed",
        input_tokens: 10,
        output_tokens: 20,
        reasoning_tokens: 5,
        cost_usd: 0.01,
        duration_seconds: 2.5,
        tool_count: 3,
      }, 3),
    ]);

    expect(roots).toHaveLength(1);
    expect(roots[0].id).toBe("parent");
    expect(roots[0].children[0]).toMatchObject({
      id: "child",
      status: "completed",
      inputTokens: 10,
      outputTokens: 20,
      reasoningTokens: 5,
      costUsd: 0.01,
      durationSeconds: 2.5,
      toolCount: 3,
    });
  });
});
