"use client";

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import {
  CapabilityContractError,
  findVariantCapability,
  parseCapabilities,
  type CapabilitiesDocument,
  type VariantCapability,
} from "@/lib/capabilities";
import {
  adapterSupportsCapability,
  getActiveAdapter,
  type DecodedVariantEvent,
  type VariantHydrationBundle,
  type VariantUIAdapter,
} from "@/lib/variants";

export type TaskStatus = "pending" | "running" | "completed" | "failed";

export interface SubTask {
  id: string;
  label: string;
  status: TaskStatus;
  agent: "planner" | "executor" | "auditor";
  depends_on: string[];
  result?: string;
  error?: string;
  started_at?: string;
  completed_at?: string;
}

export interface Task {
  id: string;
  label: string;
  status: TaskStatus;
  sub_tasks: SubTask[];
  created_at: string;
  updated_at: string;
}

export interface DebateEntry {
  id: string;
  agent_role: string;
  content: string;
  timestamp: string;
}

export interface LogEntry {
  id: string;
  agent_role: string;
  level: string;
  message: string;
  timestamp: string;
  node?: string;
  turn_id?: string;
  fields?: Record<string, unknown> | null;
}

export interface BoardEntry {
  id: string;
  type: string;
  title: string;
  body: string;
  author: string;
  refs: string[];
  confidence: number;
  salience: number;
  seq: number;
  created_at: string;
  round?: number;
  status?: string;
}

export interface TurnRecord {
  turn_id: string;
  task_id: string;
  actor: string;
  round_no: number;
  phase: string;
  status: string;
  started_at: string;
  ended_at?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  model?: string;
  role?: string;
  node?: string;
  rationale?: string | null;
}

export interface TraceEvent {
  id: string;
  turn_id: string;
  actor: string;
  type: string;
  content: string;
  seq: number;
  timestamp: string;
  run_id?: string;
  data?: Record<string, unknown>;
}

export interface TaskFile {
  id: string;
  name: string;
  mime: string;
  bytes: number;
  sha256: string;
  extracted_chars: number;
  created_at: string;
}

export interface TaskArtifact {
  id: string;
  rel_path: string;
  mime: string | null;
  bytes: number;
  sha256: string;
  version: number;
  author: string | null;
  turn_id: string | null;
  created_at: string;
}

export interface RejectedEntry {
  entry_id: string;
  actor: string;
  reason: string;
  timestamp: string;
}

export interface RosterEntry {
  actor: string;
  ability: string;
}

export interface CostData {
  total_cost: number;
  total_tokens: number;
  by_model: Record<string, { cost: number; tokens: number }>;
  by_phase?: { phase: string; cost_usd: number; tokens: number }[];
  by_actor?: { actor: string; cost_usd: number; tokens: number; turns: number }[];
}

export interface ApprovalRequest {
  turn_id: string;
  actor: string;
  run_id: string;
  description: string;
  timestamp: string;
}

export interface BudgetState {
  spent: number;
  ceiling: number;
  percentage: number;
}

export interface CoordinatorNarration {
  round: number;
  selected: string[];
  rationale: string | null;
  source: string;
  timestamp: string;
}

export interface TaskMeta {
  task_id: string;
  label: string;
  status: TaskStatus;
  complexity?: string;
  model?: string;
  variant?: string;
  created_at: string;
  completed_at?: string;
  duration_ms?: number;
  full_input?: string;
  run_state?: string;
  started_at?: string;
  last_heartbeat_at?: string;
  error_message?: string;
  terminal_kind?: "completed" | "failed" | "cancelled";
  failure_category?: string;
  cancel_requested_at?: string;
  resume_count?: number;
  effective_configuration?: Record<string, unknown>;
  submission_overrides?: Record<string, unknown>;
  execution_snapshot?: Record<string, unknown>;
  execution_snapshot_checksum?: string;
  benchmark?: Record<string, string>;
  event_delivery?: {
    status?: string;
    unpublished_events?: number;
    outbox_events?: number;
    publish_failures?: number;
    latest_cursor?: number;
  };
  storage?: {
    input_bytes: number;
    output_bytes: number;
    max_upload_mb: number;
    max_output_mb: number;
  };
}

export interface ConsensusState {
  signal: number;
  decider_state: string;
}

export type VariantRuntimeStatus =
  | "loading"
  | "ready"
  | "capabilities-unavailable"
  | "malformed-capabilities"
  | "unsupported-api-version"
  | "unsupported-variant"
  | "unsupported-contract"
  | "variant-unavailable"
  | "hydration-unavailable";

export interface VariantRuntimeState {
  status: VariantRuntimeStatus;
  message: string;
  requestedVariant: string | null;
  adapterId: string | null;
  capability: VariantCapability | null;
}

export interface TaskStreamData {
  phase: string | null;
  subTasks: SubTask[];
  debates: DebateEntry[];
  logs: LogEntry[];
  cost: CostData | null;
  result: string | null;
  error: string | null;
  isLive: boolean;
  taskMeta: TaskMeta | null;
  boardEntries: BoardEntry[];
  removedEntryIds: string[];
  consensus: ConsensusState | null;
  activeTurns: TurnRecord[];
  completedTurns: TurnRecord[];
  traceEvents: TraceEvent[];
  rejectedEntries: RejectedEntry[];
  approvalRequests: ApprovalRequest[];
  isPaused: boolean;
  budgetState: BudgetState | null;
  coordinatorNarrations: CoordinatorNarration[];
  roster: RosterEntry[];
  liveFiles: TaskFile[];
  liveArtifacts: TaskArtifact[];
  hydrationError: string | null;
  runtime: VariantRuntimeState;
}

export const INITIAL_STREAM_DATA: TaskStreamData = {
  phase: null,
  subTasks: [],
  debates: [],
  logs: [],
  cost: null,
  result: null,
  error: null,
  isLive: false,
  taskMeta: null,
  boardEntries: [],
  removedEntryIds: [],
  consensus: null,
  activeTurns: [],
  completedTurns: [],
  traceEvents: [],
  rejectedEntries: [],
  approvalRequests: [],
  isPaused: false,
  budgetState: null,
  coordinatorNarrations: [],
  roster: [],
  liveFiles: [],
  liveArtifacts: [],
  hydrationError: null,
  runtime: {
    status: "loading",
    message: "Loading daemon capabilities…",
    requestedVariant: null,
    adapterId: null,
    capability: null,
  },
};

export interface TaskBoundStreamData {
  taskId: string;
  data: TaskStreamData;
}

export interface PendingRawEvent {
  name: string;
  value: unknown;
}

export const MAX_PENDING_RAW_EVENTS = 500;

export function appendPendingRawEvent(
  events: readonly PendingRawEvent[],
  event: PendingRawEvent,
): PendingRawEvent[] {
  return [...events, event].slice(-MAX_PENDING_RAW_EVENTS);
}

export function streamDataForTask(
  state: TaskBoundStreamData,
  taskId: string,
): TaskStreamData {
  return state.taskId === taskId ? state.data : INITIAL_STREAM_DATA;
}

function readPersistedVariant(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return null;
  const envelope = value as Record<string, unknown>;
  const taskValue = envelope.task;
  const task = typeof taskValue === "object" && taskValue !== null
    ? taskValue as Record<string, unknown>
    : envelope;
  return typeof task.variant === "string" && task.variant.trim() ? task.variant : null;
}

function runtimeError(
  status: VariantRuntimeStatus,
  message: string,
  requestedVariant: string | null = null,
): VariantRuntimeState {
  return { status, message, requestedVariant, adapterId: null, capability: null };
}

export class TaskHydrationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TaskHydrationError";
  }
}

export async function fetchTaskHydrationBundle(
  taskId: string,
  signal?: AbortSignal,
): Promise<VariantHydrationBundle> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/hydrate`, {
    cache: "no-store",
    ...(signal ? { signal } : {}),
  });
  if (!response.ok) {
    throw new TaskHydrationError(`Task hydration returned HTTP ${response.status}`);
  }
  try {
    return await response.json() as VariantHydrationBundle;
  } catch {
    throw new TaskHydrationError("Task hydration returned invalid JSON");
  }
}

export async function fetchTaskRuntimeDetail(
  taskId: string,
  signal?: AbortSignal,
): Promise<unknown> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
    cache: "no-store",
    ...(signal ? { signal } : {}),
  });
  if (!response.ok) {
    throw new TaskHydrationError(`Task detail returned HTTP ${response.status}`);
  }
  try {
    return await response.json() as unknown;
  } catch {
    throw new TaskHydrationError("Task detail returned invalid JSON");
  }
}

export async function prepareTaskRuntime(
  document: CapabilitiesDocument,
  taskId: string,
  signal?: AbortSignal,
): Promise<{
  adapter: VariantUIAdapter | null;
  detail: unknown;
  runtime: VariantRuntimeState;
}> {
  const detail = await fetchTaskRuntimeDetail(taskId, signal);
  const requestedVariant = readPersistedVariant(detail);
  const resolved = resolveRuntime(document, requestedVariant);
  return { ...resolved, detail };
}

export function compareEventCursor(left: string, right: string): number {
  if (/^\d+$/.test(left) && /^\d+$/.test(right)) {
    const leftCursor = BigInt(left);
    const rightCursor = BigInt(right);
    return leftCursor === rightCursor ? 0 : leftCursor > rightCursor ? 1 : -1;
  }
  const leftParts = left.split("-");
  const rightParts = right.split("-");
  if (
    leftParts.length === 2
    && rightParts.length === 2
    && leftParts.every((part) => /^\d+$/.test(part))
    && rightParts.every((part) => /^\d+$/.test(part))
  ) {
    const leftTime = BigInt(leftParts[0]);
    const rightTime = BigInt(rightParts[0]);
    if (leftTime !== rightTime) return leftTime > rightTime ? 1 : -1;
    const leftSequence = BigInt(leftParts[1]);
    const rightSequence = BigInt(rightParts[1]);
    return leftSequence === rightSequence ? 0 : leftSequence > rightSequence ? 1 : -1;
  }
  return left.localeCompare(right);
}

export function shouldApplyEventCursor(lastCursor: string, nextCursor: string): boolean {
  return !nextCursor || !lastCursor || compareEventCursor(nextCursor, lastCursor) > 0;
}

export function taskEventListenerNames(document: CapabilitiesDocument): string[] {
  const variantEvents = new Set(document.variants.flatMap((variant) => variant.features.events));
  variantEvents.delete("initial_state");
  variantEvents.delete("error");
  return ["initial_state", ...variantEvents, "error"];
}

export function resolveRuntime(
  document: CapabilitiesDocument,
  requestedVariant: string | null,
): { runtime: VariantRuntimeState; adapter: VariantUIAdapter | null } {
  const capability = findVariantCapability(document, requestedVariant);
  const displayVariant = requestedVariant ?? "classic";
  if (!capability) {
    return {
      runtime: runtimeError(
        "unsupported-variant",
        `Mission Control does not support the saved variant “${displayVariant}”.`,
        requestedVariant,
      ),
      adapter: null,
    };
  }
  if (!capability.available) {
    return {
      runtime: runtimeError(
        "variant-unavailable",
        capability.reason || `The ${capability.label} runtime is unavailable.`,
        requestedVariant,
      ),
      adapter: null,
    };
  }
  const adapter = getActiveAdapter(capability.id);
  if (!adapter) {
    return {
      runtime: runtimeError(
        "unsupported-variant",
        `Mission Control has no interface adapter for “${capability.id}”.`,
        requestedVariant,
      ),
      adapter: null,
    };
  }
  if (!adapterSupportsCapability(adapter, capability)) {
    return {
      runtime: runtimeError(
        "unsupported-contract",
        `Mission Control does not support ${capability.label} contract version ${capability.contract_version}.`,
        requestedVariant,
      ),
      adapter: null,
    };
  }
  return {
    runtime: {
      status: "ready",
      message: "",
      requestedVariant,
      adapterId: adapter.id,
      capability,
    },
    adapter,
  };
}

export function useTaskStream(taskId: string): TaskStreamData {
  const [streamState, setStreamState] = useState<TaskBoundStreamData>({
    taskId,
    data: INITIAL_STREAM_DATA,
  });
  const data = streamDataForTask(streamState, taskId);
  const setData = useCallback((update: SetStateAction<TaskStreamData>) => {
    setStreamState((current) => {
      const currentData = streamDataForTask(current, taskId);
      const nextData = typeof update === "function" ? update(currentData) : update;
      return { taskId, data: nextData };
    });
  }, [taskId]);
  const eventSourceRef = useRef<EventSource | null>(null);
  const adapterRef = useRef<VariantUIAdapter | null>(null);
  const capabilityRef = useRef<VariantCapability | null>(null);
  const pendingRawEventsRef = useRef<PendingRawEvent[]>([]);
  const frameEventsRef = useRef<DecodedVariantEvent[]>([]);
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    if (!taskId) return;
    adapterRef.current = null;
    capabilityRef.current = null;
    pendingRawEventsRef.current = [];
    frameEventsRef.current = [];
    let cancelled = false;
    let lastEventCursor = "";
    let hydrationInFlight: Promise<void> | null = null;
    let hydrationRefreshQueued = false;
    let initialHydrationStarted = false;
    const abortController = new AbortController();

    const flushEvents = () => {
      frameRef.current = null;
      const adapter = adapterRef.current;
      const events = frameEventsRef.current;
      frameEventsRef.current = [];
      if (!adapter || events.length === 0) return;
      setData((current) => events.reduce(adapter.projectEvent, current));
    };

    const applyEvent = (event: DecodedVariantEvent, terminal: boolean) => {
      frameEventsRef.current.push(event);
      if (terminal) {
        if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
        flushEvents();
        return;
      }
      if (frameRef.current === null) frameRef.current = requestAnimationFrame(flushEvents);
    };

    const installRuntime = (
      resolved: ReturnType<typeof resolveRuntime>,
      bundle?: VariantHydrationBundle,
    ): VariantUIAdapter | null => {
      const { adapter, runtime } = resolved;
      adapterRef.current = adapter;
      capabilityRef.current = runtime.capability;
      setData((current) => {
        let next: TaskStreamData = { ...current, runtime, hydrationError: null };
        if (adapter && runtime.capability) {
          for (const pending of pendingRawEventsRef.current) {
            if (
              pending.name !== "initial_state"
              && !runtime.capability.features.events.includes(pending.name)
            ) {
              continue;
            }
            const decoded = adapter.decodeEvent(pending.name, pending.value, taskId);
            if (decoded) next = adapter.projectEvent(next, decoded);
          }
          if (bundle) next = adapter.projectHydration(next, bundle, taskId);
        }
        pendingRawEventsRef.current = [];
        return next;
      });
      return adapter;
    };

    const refreshHydration = (queueIfBusy = false): Promise<void> => {
      if (hydrationInFlight) {
        if (queueIfBusy) hydrationRefreshQueued = true;
        return hydrationInFlight;
      }
      const request = (async () => {
        try {
          const bundle = await fetchTaskHydrationBundle(taskId, abortController.signal);
          if (cancelled) return;
          const adapter = adapterRef.current;
          if (!adapter) return;
          setData((current) => adapter.projectHydration(
            { ...current, hydrationError: null },
            bundle,
            taskId,
          ));
        } catch (error) {
          if (cancelled) return;
          const message = error instanceof Error ? error.message : "Task hydration failed";
          setData((current) => ({ ...current, hydrationError: message }));
        }
      })();
      hydrationInFlight = request;
      void request.finally(() => {
        if (hydrationInFlight === request) hydrationInFlight = null;
        if (hydrationRefreshQueued && !cancelled) {
          hydrationRefreshQueued = false;
          void refreshHydration();
        }
      });
      return request;
    };

    const start = async () => {
      let document: CapabilitiesDocument;
      try {
        const response = await fetch("/api/capabilities", {
          cache: "no-store",
          signal: abortController.signal,
        });
        if (!response.ok) throw new Error(`Capabilities returned HTTP ${response.status}`);
        document = parseCapabilities(await response.json() as unknown);
      } catch (error) {
        if (cancelled) return;
        const status: VariantRuntimeStatus = error instanceof CapabilityContractError
          ? error.code === "unsupported-api-version"
            ? "unsupported-api-version"
            : "malformed-capabilities"
          : "capabilities-unavailable";
        const message = error instanceof Error ? error.message : "Daemon capabilities are unavailable.";
        setData((current) => ({ ...current, runtime: runtimeError(status, message) }));
        return;
      }
      if (cancelled) return;

      try {
        const prepared = await prepareTaskRuntime(document, taskId, abortController.signal);
        if (cancelled) return;
        const adapter = installRuntime(prepared);
        if (!adapter) return;
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : "Task hydration failed";
        setData((current) => ({
          ...current,
          hydrationError: message,
          runtime: runtimeError("hydration-unavailable", message),
        }));
        return;
      }

      const eventNames = taskEventListenerNames(document);
      const eventSource = new EventSource(`/api/stream/task/${taskId}`);
      eventSourceRef.current = eventSource;

      eventSource.addEventListener("open", () => {
        setData((current) => {
          const runState = current.taskMeta?.run_state ?? "";
          const blocked = ["blocked", "paused", "pause_requested"].includes(runState);
          const live = current.taskMeta
            ? current.taskMeta.status === "running" && !blocked
            : true;
          return { ...current, isLive: live };
        });
        if (!initialHydrationStarted) {
          initialHydrationStarted = true;
          void refreshHydration();
        }
      });

      const receive = (name: string, message: MessageEvent) => {
        if (!shouldApplyEventCursor(lastEventCursor, message.lastEventId)) return;
        let value: unknown;
        try {
          value = JSON.parse(message.data) as unknown;
        } catch {
          return;
        }
        const variant = readPersistedVariant(value);
        if (
          !adapterRef.current
          && (name === "initial_state" || ((name === "complete" || name === "error") && variant))
        ) {
          installRuntime(resolveRuntime(document, variant));
        }
        const adapter = adapterRef.current;
        const capability = capabilityRef.current;
        const terminal = name === "complete" || name === "error";
        if (!adapter || !capability) {
          pendingRawEventsRef.current = appendPendingRawEvent(
            pendingRawEventsRef.current,
            { name, value },
          );
          if (terminal) {
            eventSource.close();
            void refreshHydration(true);
          }
          return;
        }
        if (name !== "initial_state" && !capability.features.events.includes(name)) return;
        const decoded = adapter.decodeEvent(name, value, taskId);
        if (!decoded) return;
        if (message.lastEventId) lastEventCursor = message.lastEventId;
        applyEvent(decoded, terminal);
        if (terminal) {
          eventSource.close();
          void refreshHydration(true);
        }
      };

      eventSource.addEventListener("initial_state", (event) => {
        receive("initial_state", event as MessageEvent);
      });
      for (const eventName of eventNames.slice(1, -1)) {
        eventSource.addEventListener(eventName, (event) => {
          receive(eventName, event as MessageEvent);
        });
      }
      eventSource.addEventListener("error", (event) => {
        const message = event as MessageEvent;
        if (typeof message.data === "string" && message.data) {
          receive("error", message);
          return;
        }
        if (eventSource.readyState === EventSource.CLOSED) {
          setData((current) => ({ ...current, isLive: false }));
          void refreshHydration(true);
        }
      });
    };

    void start();
    return () => {
      cancelled = true;
      abortController.abort();
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
      frameEventsRef.current = [];
      pendingRawEventsRef.current = [];
    };
  }, [setData, taskId]);

  return data;
}
