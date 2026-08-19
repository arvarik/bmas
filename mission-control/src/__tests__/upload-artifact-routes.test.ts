import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/config", () => ({
  DAEMON_BASE_URL: "http://daemon",
  DAEMON_SUBMIT_URL: "http://daemon/submit",
}));

import { POST as submitTask } from "@/app/api/submit/route";
import { GET as listFiles } from "@/app/api/tasks/[taskId]/files/route";
import { GET as downloadFile } from "@/app/api/tasks/[taskId]/files/[fileId]/route";
import { GET as previewFile } from "@/app/api/tasks/[taskId]/files/[fileId]/text/route";
import { GET as inlineFile } from "@/app/api/tasks/[taskId]/files/[fileId]/preview/route";
import { GET as listArtifacts } from "@/app/api/tasks/[taskId]/artifacts/route";
import { GET as downloadArtifact } from "@/app/api/tasks/[taskId]/artifacts/[artifactId]/route";
import { GET as inlineArtifact } from "@/app/api/tasks/[taskId]/artifacts/[artifactId]/preview/route";

describe("upload and artifact proxy routes", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.BMAS_API_KEY;
  });

  it("streams one multipart submission to the atomic daemon endpoint", async () => {
    process.env.BMAS_API_KEY = "operator-key";
    const upstreamFetch = vi.fn(async (
      input: string | URL | Request,
      init?: RequestInit,
    ) => {
      expect(String(input)).toBe("http://daemon/submit-with-files");
      expect(init?.headers).toMatchObject({
        Authorization: "Bearer operator-key",
      });
      expect(init?.body).toBeInstanceOf(ReadableStream);
      return Response.json({
        task_id: "task-1",
        variant: "classic",
        status: "queued",
      }, { status: 202 });
    });
    vi.stubGlobal("fetch", upstreamFetch);
    const form = new FormData();
    form.append("task", "Read this brief");
    form.append("variant", "classic");
    form.append("files", new File(["brief"], "brief.txt"));

    const response = await submitTask(new Request(
      "http://mission-control/api/submit",
      { method: "POST", body: form },
    ));

    expect(response.status).toBe(202);
    await expect(response.json()).resolves.toMatchObject({ task_id: "task-1" });
    expect(upstreamFetch).toHaveBeenCalledOnce();
  });

  it("encodes identifiers and returns extracted preview text", async () => {
    const upstreamFetch = vi.fn(async (input: string | URL | Request) => {
      expect(String(input)).toBe(
        "http://daemon/tasks/task%2Fone/files/file%3Fone/text",
      );
      return Response.json({ extracted_text: "Verified preview" });
    });
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await previewFile(new Request("http://mission-control"), {
      params: Promise.resolve({ taskId: "task/one", fileId: "file?one" }),
    });

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("private, no-store");
    await expect(response.json()).resolves.toMatchObject({
      extracted_text: "Verified preview",
    });
  });

  it("proxies file lists and file downloads", async () => {
    const upstreamFetch = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith("/files")) {
        return Response.json({ files: [{ id: "file-1" }] });
      }
      return new Response("original bytes", {
        headers: {
          "Content-Type": "text/plain",
          "Content-Disposition": "attachment; filename=brief.txt",
        },
      });
    });
    vi.stubGlobal("fetch", upstreamFetch);

    const listResponse = await listFiles(
      new Request("http://mission-control"),
      { params: Promise.resolve({ taskId: "task-1" }) },
    );
    const downloadResponse = await downloadFile(
      new Request("http://mission-control"),
      { params: Promise.resolve({ taskId: "task-1", fileId: "file-1" }) },
    );

    await expect(listResponse.json()).resolves.toMatchObject({
      files: [{ id: "file-1" }],
    });
    await expect(downloadResponse.text()).resolves.toBe("original bytes");
    expect(downloadResponse.headers.get("cache-control")).toBe(
      "private, no-store",
    );
  });

  it("proxies artifact lists and immutable downloads", async () => {
    const upstreamFetch = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith("/artifacts")) {
        return Response.json({ artifacts: [{ id: "artifact-v1", version: 1 }] });
      }
      return new Response("version one", {
        headers: {
          "Content-Type": "text/plain",
          "Content-Disposition": "attachment; filename=result.txt",
        },
      });
    });
    vi.stubGlobal("fetch", upstreamFetch);

    const listResponse = await listArtifacts(
      new Request("http://mission-control"),
      { params: Promise.resolve({ taskId: "task-1" }) },
    );
    const downloadResponse = await downloadArtifact(
      new Request("http://mission-control"),
      {
        params: Promise.resolve({
          taskId: "task-1",
          artifactId: "artifact-v1",
        }),
      },
    );

    await expect(listResponse.json()).resolves.toMatchObject({
      artifacts: [{ id: "artifact-v1", version: 1 }],
    });
    await expect(downloadResponse.text()).resolves.toBe("version one");
    expect(downloadResponse.headers.get("cache-control")).toBe(
      "private, no-store",
    );
  });

  it("forces safe inline headers for file and artifact previews", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("preview bytes", {
      headers: {
        "Content-Type": "text/markdown",
        "Content-Disposition": "attachment; filename=result.md",
      },
    })));

    const fileResponse = await inlineFile(new Request("http://mission-control"), {
      params: Promise.resolve({ taskId: "task-1", fileId: "file-1" }),
    });
    const artifactResponse = await inlineArtifact(new Request("http://mission-control"), {
      params: Promise.resolve({ taskId: "task-1", artifactId: "artifact-1" }),
    });

    expect(fileResponse.headers.get("content-disposition")).toBe("inline");
    expect(artifactResponse.headers.get("content-disposition")).toBe("inline");
    expect(fileResponse.headers.get("x-content-type-options")).toBe("nosniff");
    expect(artifactResponse.headers.get("cache-control")).toBe("private, no-store");
  });
});
