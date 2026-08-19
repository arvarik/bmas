import { describe, expect, it } from "vitest";
import {
  filterHermesMessages,
  filterHermesSessions,
  sessionLineage,
  taskIdForSession,
} from "@/lib/hermes-session-presentation";

const sessions = [
  { id: "task-1:planner", title: "Root", model: "planner" },
  { id: "child", title: "Cost review", parent_session_id: "task-1:planner" },
  { id: "grandchild", title: "Alternate", parent_session_id: "child" },
];

describe("Hermes session presentation", () => {
  it("searches all loaded session fields", () => {
    expect(filterHermesSessions(sessions, "cost").map((session) => session.id)).toEqual(["child"]);
    expect(filterHermesSessions(sessions, "planner").map((session) => session.id)).toEqual([
      "task-1:planner",
      "child",
    ]);
  });

  it("searches structured message content and tool names", () => {
    const messages = [
      { role: "assistant", content: { answer: "Revenue grew" } },
      { role: "tool", tool_name: "terminal", content: "done" },
    ];
    expect(filterHermesMessages(messages, "revenue")).toEqual([messages[0]]);
    expect(filterHermesMessages(messages, "terminal")).toEqual([messages[1]]);
  });

  it("builds ancestors and direct children without cycles", () => {
    expect(sessionLineage(sessions, "child")).toEqual({
      ancestors: [sessions[0]],
      children: [sessions[2]],
    });
  });

  it("links explicit, metadata, source, and role-scoped task IDs", () => {
    expect(taskIdForSession({ id: "session", task_id: "task-explicit" }, "planner")).toBe("task-explicit");
    expect(taskIdForSession({ id: "session", metadata: { task_id: "task-meta" } }, "planner")).toBe("task-meta");
    expect(taskIdForSession({ id: "session", source: "bmas:task-source" }, "planner")).toBe("task-source");
    expect(taskIdForSession(sessions[0], "planner")).toBe("task-1");
    expect(taskIdForSession(sessions[0], "executor")).toBeNull();
  });
});
