import { describe, expect, it } from "vitest";
import { parseCapabilities } from "@/lib/capabilities";
import { createTaskSubmissionRequest } from "@/lib/task-submission";
import { classicBudgetCeiling, describeStopReason } from "@/lib/runtime-presentation";
import { parsePreferences } from "@/lib/preferences";
import { CLASSIC_ADAPTER } from "@/lib/variants";
import { INITIAL_STREAM_DATA } from "@/hooks/useTaskStream";
import { mapBoardEntry } from "@/lib/mappers";

const BASE_VARIANT = {
  id: "classic",
  label: "Classic blackboard",
  available: true,
  contract_version: "1",
  aliases: [],
  configuration_schema_version: "1",
  supports_recovery: true,
  required_agent_features: [],
  benchmark: {},
  features: {
    events: ["initial_state"],
    panels: ["mission"],
    graphs: ["turns"],
    controls: [],
    progress: ["round"],
    result: ["answer"],
  },
};

describe("effort profiles in capabilities", () => {
  it("parses declared profiles and tolerates bad entries", () => {
    const document = parseCapabilities({
      api_version: "1",
      variants: [{
        ...BASE_VARIANT,
        effort_profiles: {
          thorough: { label: "Thorough", description: "More rounds", settings: { max_rounds: 12 } },
          broken: "nonsense",
          bare: {},
        },
      }],
    });
    const profiles = document.variants[0].effort_profiles;
    expect(profiles?.thorough.settings.max_rounds).toBe(12);
    expect(profiles?.broken).toBeUndefined();
    expect(profiles?.bare).toEqual({ label: "bare", description: "", settings: {} });
  });

  it("keeps profiles optional", () => {
    const document = parseCapabilities({ api_version: "1", variants: [BASE_VARIANT] });
    expect(document.variants[0].effort_profiles).toBeUndefined();
  });
});

describe("submission with effort", () => {
  it("adds effort to the JSON body only when it changes behaviour", () => {
    const withEffort = createTaskSubmissionRequest("Task", "classic", [], "thorough");
    expect(JSON.parse(withEffort.body as string)).toEqual({
      task: "Task", variant: "classic", effort: "thorough",
    });
    const standard = createTaskSubmissionRequest("Task", "classic", [], "standard");
    expect(JSON.parse(standard.body as string)).toEqual({ task: "Task", variant: "classic" });
    const missing = createTaskSubmissionRequest("Task", "classic", []);
    expect(JSON.parse(missing.body as string)).toEqual({ task: "Task", variant: "classic" });
  });

  it("adds effort to the multipart form", () => {
    const file = new File(["x"], "a.txt", { type: "text/plain" });
    const request = createTaskSubmissionRequest("Task", "classic", [file], "exhaustive");
    const body = request.body as FormData;
    expect(body.get("effort")).toBe("exhaustive");
  });
});

describe("stop reason", () => {
  it("labels a verified solution", () => {
    const reason = describeStopReason({ status: "completed", terminated_by: "solution", answer_source: "decider" });
    expect(reason?.tone).toBe("verified");
  });

  it("labels a limit stop with an unverified answer", () => {
    const reason = describeStopReason({
      status: "completed", terminated_by: "max_rounds", answer_source: "decider_unverified",
    });
    expect(reason?.label).toContain("round limit");
    expect(reason?.tone).toBe("unverified");
  });

  it("labels the fallback vote", () => {
    const reason = describeStopReason({
      status: "completed", terminated_by: "budget", answer_source: "sole_unverified",
    });
    expect(reason?.label).toContain("fallback vote");
  });

  it("labels cancellation and ignores running tasks", () => {
    expect(describeStopReason({ status: "failed", terminal_kind: "cancelled" })?.tone).toBe("cancelled");
    expect(describeStopReason({ status: "running" })).toBeNull();
  });
});

describe("round display", () => {
  it("appends the configured round ceiling", () => {
    const state = {
      ...INITIAL_STREAM_DATA,
      activeTurns: [{ turn_id: "t1", task_id: "x", actor: "critic", round_no: 6, phase: "Debate", status: "active", started_at: "" }],
      taskMeta: {
        task_id: "x", label: "", status: "running" as const, created_at: "",
        effective_configuration: { settings: { classic: { max_rounds: 12 } } },
      },
    };
    expect(CLASSIC_ADAPTER.progressLabel(state, ["round"])).toContain("Round 6/12");
  });
});

describe("preferences", () => {
  it("stores and defaults the effort level", () => {
    expect(parsePreferences("").defaultEffort).toBe("standard");
    expect(parsePreferences(JSON.stringify({ defaultEffort: "thorough" })).defaultEffort).toBe("thorough");
    expect(parsePreferences(JSON.stringify({ defaultEffort: 3 })).defaultEffort).toBe("standard");
  });
});

// ── Phase 3: per-task limit overrides and budget display ─────────────

describe("createTaskSubmissionRequest with classic overrides", () => {
  it("includes overrides.classic in the JSON body when limits are set", () => {
    const request = createTaskSubmissionRequest(
      "Task", "classic", [], "exhaustive",
      { max_rounds: 20, budget_ceiling_usd: 5 },
    );
    const body = JSON.parse(request.body as string);
    expect(body.overrides).toEqual({
      classic: { max_rounds: 20, budget_ceiling_usd: 5 },
    });
  });

  it("omits overrides when the map is empty", () => {
    const request = createTaskSubmissionRequest("Task", "classic", [], "thorough", {});
    const body = JSON.parse(request.body as string);
    expect(body.overrides).toBeUndefined();
  });

  it("appends overrides JSON to multipart bodies", () => {
    const file = new File(["x"], "notes.txt", { type: "text/plain" });
    const request = createTaskSubmissionRequest(
      "Task", "classic", [file], "exhaustive", { max_rounds: 12 },
    );
    const form = request.body as FormData;
    expect(JSON.parse(form.get("overrides") as string)).toEqual({
      classic: { max_rounds: 12 },
    });
  });
});

describe("classicBudgetCeiling", () => {
  it("reads the ceiling from the captured configuration", () => {
    expect(classicBudgetCeiling({
      settings: { classic: { budget_ceiling_usd: 2.5 } },
    })).toBe(2.5);
  });

  it("returns null for missing or invalid shapes", () => {
    expect(classicBudgetCeiling(undefined)).toBeNull();
    expect(classicBudgetCeiling({})).toBeNull();
    expect(classicBudgetCeiling({ settings: { classic: { budget_ceiling_usd: 0 } } })).toBeNull();
  });
});

describe("board entry sources", () => {
  it("maps string sources and drops junk", () => {
    const entry = mapBoardEntry(
      { id: "e-1", type: "finding", body: "b", author: "expert.a",
        sources: ["https://a.example", 7, "tool:web_search"] },
      0,
    );
    expect(entry.sources).toEqual(["https://a.example", "tool:web_search"]);
  });

  it("leaves sources undefined when absent", () => {
    const entry = mapBoardEntry(
      { id: "e-1", type: "finding", body: "b", author: "expert.a" },
      0,
    );
    expect(entry.sources).toBeUndefined();
  });
});
