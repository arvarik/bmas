import { describe, expect, it } from "vitest";
import {
  createTaskSubmissionRequest,
  submissionErrorMessage,
  validateAttachment,
} from "@/lib/task-submission";

describe("task submission attachments", () => {
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

  it("reads a structured daemon attachment error", () => {
    expect(submissionErrorMessage({
      detail: { message: "Storage is not enabled" },
    }, 422)).toBe("Storage is not enabled");
  });
});
