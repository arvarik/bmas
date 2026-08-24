import { describe, expect, it } from "vitest";

import {
  logsToCsv,
  matchesLogQuery,
  matchesTraceQuery,
  matchesTurnQuery,
  parseListParam,
  updateUrlParams,
} from "@/lib/task-detail-tools";
import {
  countBoardBacklinks,
  sortBoardEntries,
  type MergedBoardEntry,
} from "@/components/features/board/boardModel";

const entry = (id: string, overrides: Partial<MergedBoardEntry> = {}): MergedBoardEntry => ({
  id,
  type: "finding",
  title: id,
  body: "",
  author: "expert.test",
  sources: [],
  refs: [],
  confidence: 0.5,
  salience: 0.5,
  seq: Number(id.replace(/\D/g, "")) || 0,
  round: 1,
  status: "open",
  created_at: "2026-08-19T00:00:00.000Z",
  ...overrides,
});

describe("task detail search and URL tools", () => {
  it("searches structured log payloads and correlation fields", () => {
    const line = {
      agent: "critic",
      level: "warning",
      message: "Review needed",
      node: "edge-2",
      turnId: "turn-7",
      fields: { tool: "web_search", result: "contradiction" },
    };

    expect(matchesLogQuery(line, "contradiction")).toBe(true);
    expect(matchesLogQuery(line, "turn-7")).toBe(true);
    expect(matchesLogQuery(line, "missing")).toBe(false);
  });

  it("searches trace payloads and all turn metadata", () => {
    expect(matchesTraceQuery({
      id: "trace-1",
      turn_id: "turn-1",
      actor: "planner",
      type: "tool_call",
      content: "Called a tool",
      seq: 1,
      timestamp: "2026-08-19T00:00:00.000Z",
      data: { query: "market share" },
    }, "market share")).toBe(true);

    expect(matchesTurnQuery({
      turn_id: "turn-2",
      task_id: "task-1",
      actor: "expert.finance",
      round_no: 3,
      phase: "debate",
      status: "completed",
      started_at: "2026-08-19T00:00:00.000Z",
      node: "edge-3",
      rationale: "Review the valuation",
    }, "valuation")).toBe(true);
  });

  it("preserves unrelated URL values and sorts list parameters", () => {
    expect(updateUrlParams("?keep=1&log_q=old", {
      log_q: "new value",
      log_agents: ["zeta", "alpha"],
      log_levels: null,
    })).toBe("?keep=1&log_q=new+value&log_agents=alpha%2Czeta");
    expect(parseListParam("alpha,zeta")).toEqual(new Set(["alpha", "zeta"]));
  });

  it("escapes commas and quotes in CSV exports", () => {
    const csv = logsToCsv([{
      agent: "critic",
      level: "error",
      message: "A \"quoted\", message",
    }]);
    expect(csv).toContain('"A ""quoted"", message"');
  });

  it("prevents spreadsheet formulas in CSV exports", () => {
    const csv = logsToCsv([{
      agent: "critic",
      level: "error",
      message: "=WEBSERVICE(\"https://example.invalid\")",
    }]);
    expect(csv).toContain('"\'=WEBSERVICE(""https://example.invalid"")"');
  });
});

describe("Blackboard sorting and backlinks", () => {
  const entries = [
    entry("entry-1", { salience: 0.2 }),
    entry("entry-2", { salience: 0.9, refs: ["entry-1"] }),
    entry("entry-3", { salience: 0.6, refs: ["entry-1"] }),
  ];

  it("counts incoming references as backlinks", () => {
    expect(countBoardBacklinks(entries).get("entry-1")).toBe(2);
  });

  it("sorts by backlinks and salience without mutating the source", () => {
    expect(sortBoardEntries(entries, "backlinks")[0].id).toBe("entry-1");
    expect(sortBoardEntries(entries, "salience")[0].id).toBe("entry-2");
    expect(entries.map((item) => item.id)).toEqual(["entry-1", "entry-2", "entry-3"]);
  });
});
