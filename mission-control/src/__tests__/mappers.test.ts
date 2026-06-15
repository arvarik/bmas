/**
 * mappers.test.ts — Tests for field-mapping pure functions.
 *
 * Covers all 6 mappers in src/lib/mappers.ts:
 * - mapSubTask
 * - mapDebate
 * - mapLog
 * - mapTaskMeta
 * - mapBoardEntry
 * - mapTurnRecord
 *
 * Each mapper normalizes daemon API shapes (snake_case, field variants)
 * into stable frontend TypeScript interfaces. Tests verify:
 * - Standard field mapping
 * - Alternative field name fallbacks
 * - Missing field defaults
 */

import { describe, it, expect } from "vitest";
import {
  mapSubTask,
  mapDebate,
  mapLog,
  mapTaskMeta,
  mapBoardEntry,
  mapTurnRecord,
} from "@/lib/mappers";

// ── mapSubTask ────────────────────────────────────────────────────────

describe("mapSubTask", () => {
  it("maps all standard fields", () => {
    const raw = {
      id: "st-1",
      label: "Triage",
      status: "completed",
      agent: "planner",
      depends_on: ["st-0"],
      result: "done",
      started_at: "2026-01-01T00:00:00Z",
      completed_at: "2026-01-01T00:01:00Z",
    };
    const result = mapSubTask(raw);
    expect(result.id).toBe("st-1");
    expect(result.label).toBe("Triage");
    expect(result.status).toBe("completed");
    expect(result.agent).toBe("planner");
    expect(result.depends_on).toEqual(["st-0"]);
    expect(result.result).toBe("done");
    expect(result.started_at).toBe("2026-01-01T00:00:00Z");
  });

  it("falls back agent_role → agent", () => {
    const raw = { id: "st-2", agent_role: "critic" };
    const result = mapSubTask(raw);
    expect(result.agent).toBe("critic");
  });

  it("defaults missing fields", () => {
    const raw = { id: "st-3" };
    const result = mapSubTask(raw);
    expect(result.label).toBe("");
    expect(result.status).toBe("pending");
    expect(result.agent).toBe("planner");
    expect(result.depends_on).toEqual([]);
    expect(result.result).toBeUndefined();
    expect(result.error).toBeUndefined();
  });
});

// ── mapDebate ─────────────────────────────────────────────────────────

describe("mapDebate", () => {
  it("maps standard fields", () => {
    const raw = {
      id: "d-1",
      agent_role: "critic",
      content: "I disagree because...",
      timestamp: "2026-01-01T00:00:00Z",
    };
    const result = mapDebate(raw, 0);
    expect(result.id).toBe("d-1");
    expect(result.agent_role).toBe("critic");
    expect(result.content).toBe("I disagree because...");
    expect(result.timestamp).toBe("2026-01-01T00:00:00Z");
  });

  it("falls back created_at → timestamp", () => {
    const raw = {
      id: "d-2",
      content: "ok",
      created_at: "2026-02-01T00:00:00Z",
    };
    const result = mapDebate(raw, 1);
    expect(result.timestamp).toBe("2026-02-01T00:00:00Z");
  });

  it("generates fallback ID from index", () => {
    const raw = { content: "no id" };
    const result = mapDebate(raw, 42);
    expect(result.id).toBe("debate-42");
  });

  it("defaults missing fields", () => {
    const raw = {};
    const result = mapDebate(raw, 0);
    expect(result.agent_role).toBe("unknown");
    expect(result.content).toBe("");
    expect(result.timestamp).toBeTruthy(); // ISO string
  });
});

// ── mapLog ────────────────────────────────────────────────────────────

describe("mapLog", () => {
  it("maps standard fields", () => {
    const raw = {
      id: "log-1",
      agent_role: "planner",
      level: "warn",
      message: "Something happened",
      timestamp: "2026-01-01T00:00:00Z",
      node: "node-a",
      turn_id: "t-1",
      fields: { key: "val" },
    };
    const result = mapLog(raw, 0);
    expect(result.id).toBe("log-1");
    expect(result.agent_role).toBe("planner");
    expect(result.level).toBe("warn");
    expect(result.message).toBe("Something happened");
    expect(result.timestamp).toBe("2026-01-01T00:00:00Z");
    expect(result.node).toBe("node-a");
    expect(result.turn_id).toBe("t-1");
    expect(result.fields).toEqual({ key: "val" });
  });

  it("falls back ts → timestamp", () => {
    const raw = { id: "log-2", message: "msg", ts: "2026-03-01T00:00:00Z" };
    const result = mapLog(raw, 0);
    expect(result.timestamp).toBe("2026-03-01T00:00:00Z");
  });

  it("generates fallback ID from index", () => {
    const raw = { message: "no id" };
    const result = mapLog(raw, 7);
    expect(result.id).toBe("log-7");
  });

  it("defaults missing fields", () => {
    const raw = {};
    const result = mapLog(raw, 0);
    expect(result.agent_role).toBe("daemon");
    expect(result.level).toBe("info");
    expect(result.message).toBe("");
    expect(result.fields).toBeNull();
  });
});

// ── mapTaskMeta ───────────────────────────────────────────────────────

describe("mapTaskMeta", () => {
  it("maps standard fields", () => {
    const raw = {
      id: "task-abc",
      label: "Test task",
      status: "running",
      complexity: "complex",
      model: "gpt-4",
      variant: "traditional",
      created_at: "2026-01-01T00:00:00Z",
      duration_ms: 5000,
      full_input: "What is the meaning of life?",
    };
    const result = mapTaskMeta(raw);
    expect(result.task_id).toBe("task-abc");
    expect(result.label).toBe("Test task");
    expect(result.status).toBe("running");
    expect(result.complexity).toBe("complex");
    expect(result.model).toBe("gpt-4");
    expect(result.variant).toBe("traditional");
    expect(result.duration_ms).toBe(5000);
    expect(result.full_input).toBe("What is the meaning of life?");
  });

  it("falls back task_id → task_id when id is missing", () => {
    const raw = { task_id: "task-xyz" };
    const result = mapTaskMeta(raw);
    expect(result.task_id).toBe("task-xyz");
  });

  it("prefers id over task_id", () => {
    const raw = { id: "task-1", task_id: "task-2" };
    const result = mapTaskMeta(raw);
    expect(result.task_id).toBe("task-1");
  });

  it("falls back model_used → model", () => {
    const raw = { id: "t", model_used: "claude-3" };
    const result = mapTaskMeta(raw);
    expect(result.model).toBe("claude-3");
  });

  it("prefers model over model_used", () => {
    const raw = { id: "t", model: "gpt-4", model_used: "claude-3" };
    const result = mapTaskMeta(raw);
    expect(result.model).toBe("gpt-4");
  });

  it("defaults missing fields", () => {
    const raw = {};
    const result = mapTaskMeta(raw);
    expect(result.label).toBe("");
    expect(result.status).toBe("pending");
    expect(result.created_at).toBe("");
  });
});

// ── mapBoardEntry ─────────────────────────────────────────────────────

describe("mapBoardEntry", () => {
  it("maps all standard fields", () => {
    const raw = {
      id: "e-1",
      type: "finding",
      title: "Key Insight",
      body: "The data shows...",
      author: "planner",
      refs: ["e-0"],
      confidence: 0.9,
      salience: 0.8,
      seq: 1,
      created_at: "2026-01-01T00:00:00Z",
      round: 2,
      status: "open",
    };
    const result = mapBoardEntry(raw, 0);
    expect(result.id).toBe("e-1");
    expect(result.type).toBe("finding");
    expect(result.title).toBe("Key Insight");
    expect(result.body).toBe("The data shows...");
    expect(result.author).toBe("planner");
    expect(result.refs).toEqual(["e-0"]);
    expect(result.confidence).toBe(0.9);
    expect(result.salience).toBe(0.8);
    expect(result.round).toBe(2);
    expect(result.status).toBe("open");
  });

  it("falls back entry_id → id", () => {
    const raw = { entry_id: "ee-1" };
    const result = mapBoardEntry(raw, 0);
    expect(result.id).toBe("ee-1");
  });

  it("falls back content → body", () => {
    const raw = { id: "e", content: "Content text" };
    const result = mapBoardEntry(raw, 0);
    expect(result.body).toBe("Content text");
  });

  it("falls back actor → author", () => {
    const raw = { id: "e", actor: "critic" };
    const result = mapBoardEntry(raw, 0);
    expect(result.author).toBe("critic");
  });

  it("falls back entry_type → type", () => {
    const raw = { id: "e", entry_type: "critique" };
    const result = mapBoardEntry(raw, 0);
    expect(result.type).toBe("critique");
  });

  it("generates fallback ID from index", () => {
    const raw = {};
    const result = mapBoardEntry(raw, 5);
    expect(result.id).toBe("e-5");
  });

  it("defaults missing fields", () => {
    const raw = {};
    const result = mapBoardEntry(raw, 0);
    expect(result.type).toBe("finding");
    expect(result.title).toBe("");
    expect(result.body).toBe("");
    expect(result.author).toBe("unknown");
    expect(result.refs).toEqual([]);
    expect(result.confidence).toBe(0);
    expect(result.salience).toBe(0);
  });
});

// ── mapTurnRecord ─────────────────────────────────────────────────────

describe("mapTurnRecord", () => {
  it("maps all standard fields", () => {
    const raw = {
      turn_id: "t-1",
      task_id: "task-1",
      actor: "planner",
      round_no: 2,
      phase: "control_plane:cu",
      status: "completed",
      started_at: "2026-01-01T00:00:00Z",
      ended_at: "2026-01-01T00:01:00Z",
      tokens_in: 500,
      tokens_out: 200,
      cost_usd: 0.0012,
      model: "gpt-4",
    };
    const result = mapTurnRecord(raw);
    expect(result.turn_id).toBe("t-1");
    expect(result.task_id).toBe("task-1");
    expect(result.actor).toBe("planner");
    expect(result.round_no).toBe(2);
    expect(result.phase).toBe("control_plane:cu");
    expect(result.status).toBe("completed");
    expect(result.tokens_in).toBe(500);
    expect(result.tokens_out).toBe(200);
    expect(result.cost_usd).toBe(0.0012);
    expect(result.model).toBe("gpt-4");
  });

  it("falls back round → round_no", () => {
    const raw = { turn_id: "t", round: 3 };
    const result = mapTurnRecord(raw);
    expect(result.round_no).toBe(3);
  });

  it("prefers round_no over round", () => {
    const raw = { turn_id: "t", round_no: 5, round: 3 };
    const result = mapTurnRecord(raw);
    expect(result.round_no).toBe(5);
  });

  it("falls back input_tokens/output_tokens → tokens_in/tokens_out", () => {
    const raw = { turn_id: "t", input_tokens: 100, output_tokens: 50 };
    const result = mapTurnRecord(raw);
    expect(result.tokens_in).toBe(100);
    expect(result.tokens_out).toBe(50);
  });

  it("falls back role → actor", () => {
    const raw = { turn_id: "t", role: "critic" };
    const result = mapTurnRecord(raw);
    expect(result.actor).toBe("critic");
  });

  it("falls back id → turn_id", () => {
    const raw = { id: "turn-99" };
    const result = mapTurnRecord(raw);
    expect(result.turn_id).toBe("turn-99");
  });

  it("falls back created_at → started_at", () => {
    const raw = { turn_id: "t", created_at: "2026-06-01T00:00:00Z" };
    const result = mapTurnRecord(raw);
    expect(result.started_at).toBe("2026-06-01T00:00:00Z");
  });

  it("defaults missing fields", () => {
    const raw = {};
    const result = mapTurnRecord(raw);
    expect(result.turn_id).toBe("");
    expect(result.task_id).toBe("");
    expect(result.actor).toBe("unknown");
    expect(result.round_no).toBe(0);
    expect(result.phase).toBe("completed");
    expect(result.status).toBe("completed");
  });
});
