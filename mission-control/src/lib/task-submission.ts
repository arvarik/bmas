export interface UploadCandidate {
  name: string;
  size: number;
}

export function validateAttachment(
  file: UploadCandidate,
  allowedTypes: readonly string[],
  maxUploadMb: number,
): string | null {
  const extension = file.name.includes(".")
    ? file.name.split(".").pop()?.toLowerCase() ?? ""
    : "";
  if (!extension || !allowedTypes.includes(extension)) {
    return `${file.name} has an unsupported file type.`;
  }
  const maxBytes = maxUploadMb * 1024 * 1024;
  if (file.size === 0) return `${file.name} is empty.`;
  if (file.size > maxBytes) {
    return `${file.name} exceeds the ${maxUploadMb} MB upload limit.`;
  }
  return null;
}

export function createTaskSubmissionRequest(
  task: string,
  variant: string,
  files: readonly File[],
): RequestInit {
  if (files.length === 0) {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, variant }),
    };
  }

  const body = new FormData();
  body.append("task", task);
  body.append("variant", variant);
  for (const file of files) body.append("files", file, file.name);
  return { method: "POST", body };
}

export function submissionErrorMessage(
  body: unknown,
  status: number,
): string {
  if (typeof body !== "object" || body === null) return `HTTP ${status}`;
  const record = body as Record<string, unknown>;
  if (typeof record.error === "string") return record.error;
  if (typeof record.detail === "string") return record.detail;
  if (typeof record.detail === "object" && record.detail !== null) {
    const detail = record.detail as Record<string, unknown>;
    if (typeof detail.message === "string") return detail.message;
  }
  return `HTTP ${status}`;
}
