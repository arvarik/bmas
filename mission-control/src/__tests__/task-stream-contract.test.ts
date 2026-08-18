import { afterEach, describe, expect, it, vi } from "vitest";
import { parseCapabilities } from "@/lib/capabilities";
import {
  compareEventCursor,
  appendPendingRawEvent,
  MAX_PENDING_RAW_EVENTS,
  INITIAL_STREAM_DATA,
  prepareTaskRuntime,
  resolveRuntime,
  shouldApplyEventCursor,
  streamDataForTask,
  taskEventListenerNames,
} from "@/hooks/useTaskStream";

const document = parseCapabilities({
  api_version: "1",
  variants: [{
    id: "classic",
    label: "Classic blackboard",
    available: true,
    contract_version: "1",
    configuration_schema_version: "1",
    supports_recovery: true,
    required_agent_features: ["execute"],
    aliases: ["traditional"],
    features: {
      events: ["initial_state", "phase", "board_entry", "complete", "error"],
      panels: ["mission"],
      graphs: ["turns"],
      controls: [],
      progress: ["phase"],
      result: ["answer"],
    },
  }],
});

describe("task stream contract", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("registers each transport event once", () => {
    const events = taskEventListenerNames(document);
    expect(events.filter((event) => event === "initial_state")).toHaveLength(1);
    expect(events.filter((event) => event === "error")).toHaveLength(1);
    expect(new Set(events).size).toBe(events.length);
  });

  it("compares journal and legacy Redis cursors exactly", () => {
    expect(compareEventCursor("10", "2")).toBeGreaterThan(0);
    expect(compareEventCursor("100-10", "100-2")).toBeGreaterThan(0);
    expect(compareEventCursor("101-0", "100-99")).toBeGreaterThan(0);
    expect(compareEventCursor("100-2", "100-2")).toBe(0);
  });

  it("rejects duplicate and older SSE deliveries", () => {
    expect(shouldApplyEventCursor("100-2", "100-2")).toBe(false);
    expect(shouldApplyEventCursor("100-2", "100-1")).toBe(false);
    expect(shouldApplyEventCursor("100-2", "100-3")).toBe(true);
  });

  it("accepts events without a cursor for compatibility", () => {
    expect(shouldApplyEventCursor("100-2", "")).toBe(true);
  });

  it("returns an explicit state for an unknown persisted variant", () => {
    const resolved = resolveRuntime(document, "future-board");
    expect(resolved.adapter).toBeNull();
    expect(resolved.runtime.status).toBe("unsupported-variant");
    expect(resolved.runtime.message).toContain("future-board");
  });

  it("returns an explicit state for an unsupported adapter contract", () => {
    const unsupported = {
      ...document,
      variants: [{ ...document.variants[0], contract_version: "2" }],
    };
    const resolved = resolveRuntime(unsupported, "classic");
    expect(resolved.adapter).toBeNull();
    expect(resolved.runtime.status).toBe("unsupported-contract");
  });

  it("returns clean stream data when the task ID changes", () => {
    const previous = {
      taskId: "task-one",
      data: { ...INITIAL_STREAM_DATA, result: "prior result", isLive: true },
    };
    const next = streamDataForTask(previous, "task-two");
    expect(next.result).toBeNull();
    expect(next.isLive).toBe(false);
    expect(next.runtime.status).toBe("loading");
  });

  it("bounds events that arrive before runtime activation", () => {
    let pending: ReturnType<typeof appendPendingRawEvent> = [];
    for (let index = 0; index < MAX_PENDING_RAW_EVENTS + 20; index += 1) {
      pending = appendPendingRawEvent(pending, {
        name: index === MAX_PENDING_RAW_EVENTS + 19 ? "complete" : "log",
        value: { index },
      });
    }
    expect(pending).toHaveLength(MAX_PENDING_RAW_EVENTS);
    expect(pending[0].value).toEqual({ index: 20 });
    expect(pending.at(-1)?.name).toBe("complete");
  });

  it("prepares the persisted runtime from task detail before streaming", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      task: { id: "task-1", variant: "traditional" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const prepared = await prepareTaskRuntime(document, "task-1");

    expect(prepared.adapter?.id).toBe("classic");
    expect(prepared.runtime.status).toBe("ready");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/task-1",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining("/hydrate"),
      expect.anything(),
    );
  });

  it("rejects a failed hydration bootstrap", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      Response.json({ error: "unavailable" }, { status: 503 }),
    ));
    await expect(prepareTaskRuntime(document, "task-1")).rejects.toThrow(
      "Task detail returned HTTP 503",
    );
  });
});
