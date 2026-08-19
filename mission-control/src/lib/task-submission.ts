export interface UploadCandidate {
  name: string;
  size: number;
  lastModified?: number;
}

export const MAX_TASK_ATTACHMENTS = 10;

export function buildTaskObjective(
  objective: string,
  constraints: string,
  expectedOutput: string,
  quickMode: boolean,
): string {
  const cleanObjective = objective.trim();
  if (quickMode) return cleanObjective;
  const sections = [`Objective\n${cleanObjective}`];
  if (constraints.trim()) sections.push(`Constraints\n${constraints.trim()}`);
  if (expectedOutput.trim()) sections.push(`Expected output\n${expectedOutput.trim()}`);
  return sections.join("\n\n");
}

export interface AttachmentSelection<T extends UploadCandidate> {
  files: T[];
  errors: string[];
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

function attachmentKey(file: UploadCandidate): string {
  return `${file.name}:${file.size}:${file.lastModified ?? 0}`;
}

export function addAttachments<T extends UploadCandidate>(
  current: readonly T[],
  candidates: readonly T[],
  allowedTypes: readonly string[],
  maxUploadMb: number,
  maxFiles = MAX_TASK_ATTACHMENTS,
): AttachmentSelection<T> {
  const files = [...current];
  const errors: string[] = [];
  const knownFiles = new Set(current.map(attachmentKey));

  for (const file of candidates) {
    const validationError = validateAttachment(file, allowedTypes, maxUploadMb);
    if (validationError) {
      errors.push(validationError);
      continue;
    }
    if (knownFiles.has(attachmentKey(file))) {
      errors.push(`${file.name} is already attached.`);
      continue;
    }
    if (files.length >= maxFiles) {
      errors.push(`You can attach up to ${maxFiles} files to one task.`);
      break;
    }
    files.push(file);
    knownFiles.add(attachmentKey(file));
  }

  return { files, errors };
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
