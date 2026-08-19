import { describe, expect, it } from "vitest";
import {
  addAttachments,
  buildTaskObjective,
  createTaskSubmissionRequest,
  submissionErrorMessage,
  validateAttachment,
} from "@/lib/task-submission";

describe("task submission attachments", () => {
  it("builds a guided objective without changing quick mode text", () => {
    expect(buildTaskObjective("Investigate", "Use primary sources", "A table", false)).toBe(
      "Objective\nInvestigate\n\nConstraints\nUse primary sources\n\nExpected output\nA table",
    );
    expect(buildTaskObjective("  Investigate  ", "Ignored", "Ignored", true)).toBe("Investigate");
  });
  it("uses JSON when the task has no files", () => {
    const request = createTaskSubmissionRequest("Question", "classic", []);

    expect(request.headers).toEqual({ "Content-Type": "application/json" });
    expect(request.body).toBe(JSON.stringify({
      task: "Question",
      variant: "classic",
    }));
  });

  it("uses one multipart request when the task has files", () => {
    const file = new File(["brief"], "brief.txt", { type: "text/plain" });
    const request = createTaskSubmissionRequest("Question", "classic", [file]);

    expect(request.headers).toBeUndefined();
    expect(request.body).toBeInstanceOf(FormData);
    const body = request.body as FormData;
    expect(body.get("task")).toBe("Question");
    expect(body.get("variant")).toBe("classic");
    expect((body.get("files") as File).name).toBe("brief.txt");
  });

  it("rejects unsupported, empty, and oversized files before submission", () => {
    expect(validateAttachment(
      { name: "script.exe", size: 1 },
      ["txt"],
      1,
    )).toContain("unsupported");
    expect(validateAttachment(
      { name: "empty.txt", size: 0 },
      ["txt"],
      1,
    )).toContain("empty");
    expect(validateAttachment(
      { name: "large.txt", size: 1_048_577 },
      ["txt"],
      1,
    )).toContain("1 MB");
  });

  it("accepts dropped or pasted files without duplicates", () => {
    const brief = { name: "brief.txt", size: 10, lastModified: 1 };
    const notes = { name: "notes.txt", size: 20, lastModified: 2 };
    const selection = addAttachments(
      [brief],
      [brief, notes],
      ["txt"],
      1,
      10,
    );

    expect(selection.files).toEqual([brief, notes]);
    expect(selection.errors).toEqual(["brief.txt is already attached."]);
  });

  it("reports the attachment count limit", () => {
    const selection = addAttachments(
      [{ name: "one.txt", size: 10 }],
      [{ name: "two.txt", size: 10 }, { name: "three.txt", size: 10 }],
      ["txt"],
      1,
      2,
    );

    expect(selection.files.map((file) => file.name)).toEqual(["one.txt", "two.txt"]);
    expect(selection.errors).toContain("You can attach up to 2 files to one task.");
  });

  it("reads a structured daemon attachment error", () => {
    expect(submissionErrorMessage({
      detail: { message: "Storage is not enabled" },
    }, 422)).toBe("Storage is not enabled");
  });
});
