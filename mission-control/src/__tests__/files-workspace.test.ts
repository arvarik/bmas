import { describe, expect, it } from "vitest";
import { groupArtifactVersions, previewKind, readTextPreview } from "@/components/features/FilesWorkspace";
import type { TaskArtifact } from "@/hooks/useTaskStream";

function artifact(id: string, version: number, path = "report.md"): TaskArtifact {
  return {
    id,
    rel_path: path,
    mime: "text/markdown",
    bytes: version * 10,
    sha256: id,
    version,
    author: "planner",
    turn_id: null,
    created_at: `2026-01-0${version}T00:00:00Z`,
  };
}

describe("Files workspace", () => {
  it.each([
    ["brief.pdf", "application/pdf", "pdf"],
    ["chart.png", "image/png", "image"],
    ["data.json", "application/octet-stream", "json"],
    ["notes.md", "text/plain", "markdown"],
    ["worker.ts", "text/plain", "code"],
    ["output.txt", "text/plain", "text"],
    ["archive.zip", "application/zip", "unsupported"],
  ])("maps %s to a %s preview", (name, mime, expected) => {
    expect(previewKind(name, mime)).toBe(expected);
  });

  it("groups output history and sorts newest versions first", () => {
    const groups = groupArtifactVersions([
      artifact("v1", 1),
      artifact("other", 1, "data.json"),
      artifact("v3", 3),
      artifact("v2", 2),
    ]);

    expect(groups.size).toBe(2);
    expect(groups.get("report.md")?.map((item) => item.version)).toEqual([3, 2, 1]);
  });

  it("limits a large text preview before it reaches the page", async () => {
    const preview = await readTextPreview(new Response("abcdefgh"), 4);

    expect(preview).toEqual({ text: "abcd", truncated: true });
  });
});
