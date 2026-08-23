import { describe, expect, it } from "vitest";
import { parseCapabilities } from "@/lib/capabilities";
import { createTaskSubmissionRequest } from "@/lib/task-submission";
import { describeStopReason } from "@/lib/runtime-presentation";
import { parsePreferences } from "@/lib/preferences";
import { CLASSIC_ADAPTER } from "@/lib/variants";
import { INITIAL_STREAM_DATA } from "@/hooks/useTaskStream";

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
