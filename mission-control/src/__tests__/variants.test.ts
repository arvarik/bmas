import { describe, expect, it } from "vitest";
import { INITIAL_STREAM_DATA } from "@/hooks/useTaskStream";
import {
  CLASSIC_ADAPTER,
  MAX_LIVE_ARTIFACT_EVENTS,
  MAX_LIVE_FILE_EVENTS,
  MAX_LIVE_LOGS,
  adapterSupportsCapability,
  getActiveAdapter,
  listAdapters,
  visibleNavigationPanels,
} from "@/lib/variants";
import type { VariantCapability } from "@/lib/capabilities";

const capability: VariantCapability = {
  id: "classic",
  label: "Classic blackboard",
  available: true,
  contract_version: "1",
  configuration_schema_version: "1",
  supports_recovery: true,
  required_agent_features: ["execute"],
  aliases: ["traditional"],
  features: {
    events: ["initial_state", "board_entry", "turn_end", "trace"],
    panels: ["mission", "blackboard", "logs"],
    graphs: ["turns"],
    controls: ["pause", "resume", "directive"],
    progress: ["phase", "round", "effective_actions"],
    result: ["answer"],
  },
};

describe("variant adapter registry", () => {
  it("resolves the canonical classic adapter", () => {
    expect(getActiveAdapter("classic")?.id).toBe("classic");
  });

  it("resolves the persisted traditional alias", () => {
    expect(getActiveAdapter("traditional")?.id).toBe("classic");
  });

  it("does not fall back for an unknown persisted variant", () => {
    expect(getActiveAdapter("future-board")).toBeNull();
  });

  it("registers every implemented runtime adapter", () => {
    expect(listAdapters().map((adapter) => adapter.id)).toEqual([
      "classic",
      "patchboard",
      "stigmergic",
    ]);
  });

  it("rejects an unsupported adapter contract", () => {
    expect(adapterSupportsCapability(CLASSIC_ADAPTER, {
      ...capability,
      contract_version: "2",
    })).toBe(false);
  });

  it("derives navigation from daemon features", () => {
    expect(visibleNavigationPanels(CLASSIC_ADAPTER, capability).map((panel) => panel.label)).toEqual([
      "Summary",
      "Blackboard",
      "Execution",
      "Logs",
    ]);
  });

  it("projects duplicate board delivery as one entry", () => {
    const event = CLASSIC_ADAPTER.decodeEvent(
      "board_entry",
      { id: "entry-1", body: "A", type: "finding" },
      "task-1",
    );
    const first = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, event!);
    const second = CLASSIC_ADAPTER.projectEvent(first, {
      ...event!,
      payload: { id: "entry-1", body: "B", type: "finding" },
    });
    expect(second.boardEntries).toHaveLength(1);
    expect(second.boardEntries[0].body).toBe("B");
  });

  it("clears a pending Hermes approval after a response event", () => {
    const pending = CLASSIC_ADAPTER.projectEvent(
      INITIAL_STREAM_DATA,
      CLASSIC_ADAPTER.decodeEvent("approval_request", {
        turn_id: "turn-1",
        actor: "expert.one",
        run_id: "run-1",
        description: "Run a command",
      }, "task-1")!,
    );
    const resolved = CLASSIC_ADAPTER.projectEvent(
      pending,
      CLASSIC_ADAPTER.decodeEvent("approval_response", {
        run_id: "run-1",
        choice: "session",
      }, "task-1")!,
    );

    expect(pending.approvalRequests).toHaveLength(1);
    expect(resolved.approvalRequests).toHaveLength(0);
  });

  it("projects a live salience change from the shared status event", () => {
    const state = {
      ...INITIAL_STREAM_DATA,
      boardEntries: [{
        id: "entry-1",
        type: "finding",
        title: "Finding",
        body: "Body",
        author: "expert.one",
        refs: [],
        confidence: 0.8,
        salience: 0.4,
        seq: 1,
        created_at: "",
        status: "open",
      }],
    };
    const event = CLASSIC_ADAPTER.decodeEvent(
      "entry_status_changed",
      { entry_id: "entry-1", salience: 0.8, action: "boost" },
      "task-1",
    );
    const next = CLASSIC_ADAPTER.projectEvent(state, event!);
    expect(next.boardEntries[0]).toMatchObject({ salience: 0.8, status: "open" });
  });

  it("projects file and artifact metadata without a refresh request", () => {
    const withFile = CLASSIC_ADAPTER.projectEvent(
      INITIAL_STREAM_DATA,
      CLASSIC_ADAPTER.decodeEvent("file_added", {
        file_id: "file-1",
        name: "brief.pdf",
        mime: "application/pdf",
        bytes: 20,
      }, "task-1")!,
    );
    const withArtifact = CLASSIC_ADAPTER.projectEvent(
      withFile,
      CLASSIC_ADAPTER.decodeEvent("artifact_created", {
        artifact_id: "artifact-1",
        rel_path: "reports/final.md",
        bytes: 40,
        version: 2,
      }, "task-1")!,
    );
    expect(withArtifact.liveFiles).toEqual([
      expect.objectContaining({ id: "file-1", name: "brief.pdf" }),
    ]);
    expect(withArtifact.liveArtifacts).toEqual([
      expect.objectContaining({ id: "artifact-1", rel_path: "reports/final.md", version: 2 }),
    ]);
  });

  it("bounds projected file and artifact event collections", () => {
    let state = INITIAL_STREAM_DATA;
    const eventCount = MAX_LIVE_ARTIFACT_EVENTS + 10;
    for (let index = 0; index < eventCount; index += 1) {
      state = CLASSIC_ADAPTER.projectEvent(
        state,
        CLASSIC_ADAPTER.decodeEvent("artifact_created", {
          artifact_id: `artifact-${index}`,
          rel_path: `artifact-${index}.txt`,
        }, "task-1")!,
      );
      if (index < MAX_LIVE_FILE_EVENTS + 10) {
        state = CLASSIC_ADAPTER.projectEvent(
          state,
          CLASSIC_ADAPTER.decodeEvent("file_added", {
            file_id: `file-${index}`,
            name: `file-${index}.txt`,
          }, "task-1")!,
        );
      }
    }
    expect(state.liveFiles).toHaveLength(MAX_LIVE_FILE_EVENTS);
    expect(state.liveFiles[0].id).toBe("file-10");
    expect(state.liveArtifacts).toHaveLength(MAX_LIVE_ARTIFACT_EVENTS);
    expect(state.liveArtifacts[0].id).toBe("artifact-10");
  });

  it("projects a terminal turn without a prior start event", () => {
    const event = CLASSIC_ADAPTER.decodeEvent(
      "turn_end",
      { turn_id: "turn-1", actor: "expert.one", status: "completed", round: 2 },
      "task-1",
    );
    const state = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, event!);
    expect(state.completedTurns).toHaveLength(1);
    expect(state.completedTurns[0]).toMatchObject({ turn_id: "turn-1", round_no: 2 });
  });

  it("merges hydration without replacing a newer live board entry", () => {
    const live = {
      ...INITIAL_STREAM_DATA,
      boardEntries: [{
        id: "entry-1",
        type: "finding",
        title: "Live",
        body: "new",
        author: "expert.one",
        refs: [],
        confidence: 1,
        salience: 1,
        seq: 2,
        created_at: "",
      }],
    };
    const state = CLASSIC_ADAPTER.projectHydration(live, {
      detail: { task: { id: "task-1", label: "Task", status: "running", variant: "classic" } },
      board: { entries: [{ id: "entry-1", title: "Old", body: "old" }] },
      turns: null,
      cost: null,
      logs: null,
      traces: null,
    }, "task-1");
    expect(state.boardEntries).toHaveLength(1);
    expect(state.boardEntries[0].body).toBe("new");
  });

  it("builds progress text from advertised fields only", () => {
    const state = {
      ...INITIAL_STREAM_DATA,
      phase: "debate",
      activeTurns: [{
        turn_id: "turn-1",
        task_id: "task-1",
        actor: "expert.one",
        round_no: 3,
        phase: "debate",
        status: "active",
        started_at: "",
      }],
    };
    expect(CLASSIC_ADAPTER.progressLabel(state, ["round", "phase"])).toBe("Round 3 · debate");
    expect(CLASSIC_ADAPTER.progressLabel(state, ["phase"])).toBe("debate");
  });

  it("hydrates saved logs and traces while preserving newer live values", () => {
    const live = {
      ...INITIAL_STREAM_DATA,
      logs: [{
        id: "live-log-1",
        agent_role: "expert.one",
        level: "info",
        message: "shared log",
        timestamp: "2026-01-02T00:00:00Z",
        turn_id: "turn-1",
      }],
      traceEvents: [{
        id: "turn-1:1:reasoning",
        turn_id: "turn-1",
        actor: "expert.one",
        type: "reasoning",
        content: "live trace",
        seq: 1,
        timestamp: "2026-01-02T00:00:00Z",
      }],
    };
    const state = CLASSIC_ADAPTER.projectHydration(live, {
      detail: { task: { id: "task-1", label: "Task", status: "running", variant: "classic" } },
      board: null,
      turns: null,
      cost: null,
      logs: { entries: [
        { id: "log-0", message: "saved", created_at: "2026-01-01T00:00:00Z" },
        {
          id: 42,
          agent_role: "expert.one",
          message: "shared log",
          created_at: "2026-01-02T00:00:00Z",
          turn_id: "turn-1",
        },
      ] },
      traces: { traces: [
        { trace_id: "trace-0", content: "saved trace", seq: 0 },
        { id: 99, turn_id: "turn-1", type: "reasoning", content: "stale trace", seq: 1 },
      ] },
    }, "task-1");
    expect(state.logs.map((log) => log.message)).toEqual(["saved", "shared log"]);
    expect(state.logs[1].id).toBe("live-log-1");
    expect(state.traceEvents.map((trace) => trace.content)).toEqual(["saved trace", "live trace"]);
  });

  it("keeps only the newest bounded live log records", () => {
    let state = INITIAL_STREAM_DATA;
    for (let index = 0; index < MAX_LIVE_LOGS + 10; index += 1) {
      const event = CLASSIC_ADAPTER.decodeEvent(
        "log",
        { id: `log-${index}`, message: `message-${index}` },
        "task-1",
      );
      state = CLASSIC_ADAPTER.projectEvent(state, event!);
    }
    expect(state.logs).toHaveLength(MAX_LIVE_LOGS);
    expect(state.logs[0].id).toBe("log-10");
    expect(state.logs.at(-1)?.id).toBe(`log-${MAX_LIVE_LOGS + 9}`);
  });

  it("projects the canonical answer from a terminal event", () => {
    const event = CLASSIC_ADAPTER.decodeEvent(
      "complete",
      { id: "task-1", status: "completed", answer: "Canonical answer" },
      "task-1",
    );
    const state = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, event!);
    expect(state.result).toBe("Canonical answer");
    expect(state.taskMeta?.status).toBe("completed");
  });

  it("uses the authoritative terminal cost summary during hydration", () => {
    const live = {
      ...INITIAL_STREAM_DATA,
      taskMeta: {
        task_id: "task-1",
        label: "Task",
        status: "completed" as const,
        variant: "classic",
        created_at: "",
      },
      cost: { total_cost: 9, total_tokens: 900, by_model: {} },
    };
    const state = CLASSIC_ADAPTER.projectHydration(live, {
      detail: { task: { id: "task-1", label: "Task", status: "completed", variant: "classic" } },
      board: null,
      turns: null,
      cost: {
        total_cost_usd: 2,
        total_tokens: 200,
        by_model: [{ model: "model-a", cost_usd: 2, input_tokens: 100, output_tokens: 100 }],
      },
      logs: null,
      traces: null,
    }, "task-1");
    expect(state.cost).toMatchObject({ total_cost: 2, total_tokens: 200 });
  });
});

describe("live status projection", () => {
  const queuedInitialState = {
    name: "initial_state",
    taskId: "task-1",
    payload: {
      task: {
        id: "task-1",
        label: "Task",
        status: "pending",
        run_state: "running",
        variant: "classic",
      },
      sub_tasks: [],
    },
  };

  it("treats a leased task as running before triage saves the status", () => {
    const state = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, queuedInitialState);
    expect(state.taskMeta?.status).toBe("running");
    expect(state.isLive).toBe(true);
  });

  it("keeps a queued task pending until activity arrives", () => {
    const queued = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, {
      ...queuedInitialState,
      payload: { task: { ...queuedInitialState.payload.task, run_state: "queued" }, sub_tasks: [] },
    });
    expect(queued.taskMeta?.status).toBe("pending");
    const running = CLASSIC_ADAPTER.projectEvent(queued, {
      name: "phase",
      taskId: "task-1",
      payload: { phase: "genesis" },
    });
    expect(running.taskMeta?.status).toBe("running");
    expect(running.isLive).toBe(true);
  });

  it("does not let a stale saved row demote a live running task", () => {
    const live = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, queuedInitialState);
    const state = CLASSIC_ADAPTER.projectHydration(live, {
      detail: { task: { id: "task-1", label: "Task", status: "pending", run_state: "queued", variant: "classic" } },
      board: null,
      turns: null,
      cost: null,
      logs: null,
      traces: null,
    }, "task-1");
    expect(state.taskMeta?.status).toBe("running");
    expect(state.isLive).toBe(true);
  });

  it("ends the live view when hydration reports a terminal task", () => {
    const live = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, queuedInitialState);
    const state = CLASSIC_ADAPTER.projectHydration(live, {
      detail: { task: { id: "task-1", label: "Task", status: "completed", variant: "classic" } },
      board: null,
      turns: null,
      cost: null,
      logs: null,
      traces: null,
    }, "task-1");
    expect(state.taskMeta?.status).toBe("completed");
    expect(state.isLive).toBe(false);
  });

  it("ends the live view when the task is blocked", () => {
    const live = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, queuedInitialState);
    const state = CLASSIC_ADAPTER.projectHydration(live, {
      detail: { task: { id: "task-1", label: "Task", status: "running", run_state: "blocked", variant: "classic" } },
      board: null,
      turns: null,
      cost: null,
      logs: null,
      traces: null,
    }, "task-1");
    expect(state.isLive).toBe(false);
  });
});

describe("terminal replay hydration", () => {
  it("fills missing live fields from the saved row after a complete event", () => {
    const completed = CLASSIC_ADAPTER.projectEvent(INITIAL_STREAM_DATA, {
      name: "complete",
      taskId: "task-1",
      payload: { answer: "Done", variant: "classic" },
    });
    expect(completed.taskMeta?.status).toBe("completed");
    expect(completed.taskMeta?.label).toBe("");
    const state = CLASSIC_ADAPTER.projectHydration(completed, {
      detail: { task: { id: "task-1", label: "Saved label", status: "completed", created_at: "2026-01-01T00:00:00Z", variant: "classic" } },
      board: null,
      turns: null,
      cost: null,
      logs: null,
      traces: null,
    }, "task-1");
    expect(state.taskMeta?.label).toBe("Saved label");
    expect(state.taskMeta?.created_at).toBe("2026-01-01T00:00:00Z");
    expect(state.taskMeta?.status).toBe("completed");
    expect(state.isLive).toBe(false);
  });
});
