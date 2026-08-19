import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ClassicResultRenderer } from "@/components/features/ClassicResultRenderer";
import { ArtifactBrowser, mergeTaskArtifacts } from "@/components/features/ArtifactBrowser";
import { AttachmentRail, mergeTaskFiles } from "@/components/features/AttachmentRail";
import { UnsupportedVariantState } from "@/components/features/UnsupportedVariantState";
import type { VariantRuntimeState } from "@/hooks/useTaskStream";

function runtime(
  status: VariantRuntimeState["status"],
  message: string,
): VariantRuntimeState {
  return {
    status,
    message,
    requestedVariant: "future-board",
    adapterId: null,
    capability: null,
  };
}

describe("capability-driven components", () => {
  it("renders an explicit unknown-variant state", () => {
    const html = renderToStaticMarkup(
      <UnsupportedVariantState
        runtime={runtime("unsupported-variant", "The saved variant is unsupported.")}
      />,
    );
    expect(html).toContain("Coordination interface unavailable");
    expect(html).toContain("The saved variant is unsupported.");
    expect(html).toContain('role="alert"');
  });

  it("renders an explicit unsupported-feature state", () => {
    const html = renderToStaticMarkup(
      <UnsupportedVariantState
        runtime={runtime("ready", "")}
        feature="execution graph"
      />,
    );
    expect(html).toContain("Interface feature unavailable");
    expect(html).toContain("execution graph");
  });

  it("extracts the classic answer from a board-entry result", () => {
    const content = "```json\n{\"type\":\"solution\",\"body\":\"# Verified answer\"}\n```";
    const html = renderToStaticMarkup(
      <ClassicResultRenderer content={content} formats={["answer"]} />,
    );
    expect(html).toContain("Verified answer");
    expect(html).not.toContain("solution");
  });

  it("does not apply a renderer that the daemon did not advertise", () => {
    const content = "```json\n{\"body\":\"Answer\"}\n```";
    const html = renderToStaticMarkup(
      <ClassicResultRenderer content={content} formats={[]} />,
    );
    expect(html).toContain("{&quot;body&quot;:&quot;Answer&quot;}");
  });

  it("renders projected file and artifact events without another request", () => {
    const files = [{
      id: "file-1",
      name: "brief.pdf",
      mime: "application/pdf",
      bytes: 1_024,
      sha256: "abc",
      extracted_chars: 50,
      created_at: "",
    }];
    const artifacts = [{
      id: "artifact-1",
      rel_path: "reports/final.md",
      mime: null,
      bytes: 512,
      sha256: "def",
      version: 1,
      author: "writer.one",
      turn_id: "turn-1",
      created_at: "",
    }];
    const fileHtml = renderToStaticMarkup(
      <AttachmentRail taskId="task-1" liveFiles={files} />,
    );
    const artifactHtml = renderToStaticMarkup(
      <ArtifactBrowser taskId="task-1" liveArtifacts={artifacts} />,
    );
    expect(fileHtml).toContain("brief.pdf");
    expect(artifactHtml).toContain("final.md");
  });

  it("merges live collection events without removing saved metadata", () => {
    const savedFile = {
      id: "file-1",
      name: "brief.pdf",
      mime: "application/pdf",
      bytes: 100,
      sha256: "saved",
      extracted_chars: 20,
      created_at: "2026-01-01T00:00:00Z",
    };
    const savedArtifact = {
      id: "artifact-1",
      rel_path: "final.md",
      mime: "text/markdown",
      bytes: 100,
      sha256: "saved",
      version: 1,
      author: "writer.one",
      turn_id: "turn-1",
      created_at: "2026-01-01T00:00:00Z",
    };
    const files = mergeTaskFiles([savedFile], [{
      ...savedFile,
      bytes: 200,
      sha256: "",
      created_at: "",
    }]);
    const artifacts = mergeTaskArtifacts([savedArtifact], [{
      ...savedArtifact,
      bytes: 200,
      mime: null,
      sha256: "",
      created_at: "",
    }]);
    expect(files[0]).toMatchObject({ bytes: 200, sha256: "saved" });
    expect(artifacts[0]).toMatchObject({
      bytes: 200,
      mime: "text/markdown",
      sha256: "saved",
    });
  });

  it("sorts artifact versions newest first within each path", () => {
    const base = {
      mime: "text/plain",
      bytes: 10,
      sha256: "hash",
      author: "planner",
      turn_id: "turn-1",
      created_at: "2026-01-01T00:00:00Z",
    };
    const artifacts = mergeTaskArtifacts([
      { ...base, id: "v1", rel_path: "report.txt", version: 1 },
      { ...base, id: "other", rel_path: "notes.txt", version: 1 },
      { ...base, id: "v2", rel_path: "report.txt", version: 2 },
    ], []);

    expect(artifacts.map((artifact) => artifact.id)).toEqual([
      "other",
      "v2",
      "v1",
    ]);
  });

  it("keys file views by task so navigation resets local state", () => {
    const firstRail = AttachmentRail({ taskId: "task-one" });
    const secondRail = AttachmentRail({ taskId: "task-two" });
    const firstBrowser = ArtifactBrowser({ taskId: "task-one" });
    const secondBrowser = ArtifactBrowser({ taskId: "task-two" });

    expect(firstRail.key).toBe("task-one");
    expect(secondRail.key).toBe("task-two");
    expect(firstBrowser.key).toBe("task-one");
    expect(secondBrowser.key).toBe("task-two");
  });
});
