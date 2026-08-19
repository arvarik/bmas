"use client";

import { useCallback, useRef, useState } from "react";

export type TaskOperatorAction =
  | "pause"
  | "resume"
  | "abort"
  | "inject-hint"
  | "approval"
  | "run-steer";

export interface TaskOperatorRequest {
  action: TaskOperatorAction;
  task_id: string;
  hint_text?: string;
  reason?: string;
  run_id?: string;
  choice?: "once" | "session" | "always" | "deny";
  input?: string;
}

export interface TaskOperatorResult {
  ok: boolean;
  data?: Record<string, unknown>;
  error?: string;
}

export interface TaskOperatorActionState {
  action: TaskOperatorAction | null;
  status: "idle" | "pending" | "success" | "error";
  message: string;
}

const DEFAULT_TIMEOUT_MS = 15_000;

function responseError(
  status: number,
  body: Record<string, unknown>,
): string {
  const detail = typeof body.detail === "string" ? body.detail : null;
  const error = typeof body.error === "string" ? body.error : null;
  return detail || error || `Task control returned HTTP ${status}`;
}

function requestFailure(error: unknown, timeoutMs: number): string {
  if (error instanceof DOMException && error.name === "TimeoutError") {
    return `The request timed out after ${Math.round(timeoutMs / 1_000)} seconds. Check the task state before you retry.`;
  }
  return error instanceof Error ? error.message : "The task action failed.";
}

function pendingMessage(action: TaskOperatorAction): string {
  if (action === "pause") return "Pause request pending.";
  if (action === "resume") return "Resume request pending.";
  if (action === "abort") return "Stop request pending.";
  if (action === "approval") return "Approval response pending.";
  return "Guidance request pending.";
}

export async function requestTaskOperatorAction(
  request: TaskOperatorRequest,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<Record<string, unknown>> {
  try {
    const response = await fetch("/api/hitl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const body = await response.json().catch(() => ({})) as Record<string, unknown>;
    if (!response.ok) throw new Error(responseError(response.status, body));
    return body;
  } catch (error) {
    throw new Error(requestFailure(error, timeoutMs), { cause: error });
  }
}

export function useTaskOperatorAction(timeoutMs = DEFAULT_TIMEOUT_MS) {
  const [state, setState] = useState<TaskOperatorActionState>({
    action: null,
    status: "idle",
    message: "",
  });
  const lastRequest = useRef<TaskOperatorRequest | null>(null);
  const pendingRequest = useRef(false);

  const execute = useCallback(async (
    request: TaskOperatorRequest,
    successMessage: string,
  ): Promise<TaskOperatorResult> => {
    if (pendingRequest.current) {
      return { ok: false, error: "Another task action is still pending." };
    }
    pendingRequest.current = true;
    lastRequest.current = request;
    setState({
      action: request.action,
      status: "pending",
      message: pendingMessage(request.action),
    });
    try {
      const data = await requestTaskOperatorAction(request, timeoutMs);
      setState({ action: request.action, status: "success", message: successMessage });
      return { ok: true, data };
    } catch (error) {
      const message = error instanceof Error ? error.message : "The task action failed.";
      setState({ action: request.action, status: "error", message });
      return { ok: false, error: message };
    } finally {
      pendingRequest.current = false;
    }
  }, [timeoutMs]);

  const retry = useCallback(async (successMessage: string): Promise<TaskOperatorResult> => {
    if (!lastRequest.current) return { ok: false, error: "No task action is available to retry." };
    return execute(lastRequest.current, successMessage);
  }, [execute]);

  const clear = useCallback(() => {
    setState({ action: null, status: "idle", message: "" });
  }, []);

  return { state, execute, retry, clear };
}
