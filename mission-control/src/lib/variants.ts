/** Mission Control adapters for daemon-reported coordination variants. */

import type { ComponentType } from "react";
import { ClassicResultRenderer } from "@/components/features/ClassicResultRenderer";
import type { VariantCapability } from "@/lib/capabilities";
import {
  CLASSIC_CONTRACT_VERSIONS,
  PATCHBOARD_CONTRACT_VERSIONS,
  STIGMERGIC_CONTRACT_VERSIONS,
} from "@/lib/variant-support";
import {
  mapBoardEntry,
  mapDebate,
  mapLog,
  mapSubTask,
  mapTaskMeta,
  mapTurnRecord,
} from "@/lib/mappers";
import type {
  CoordinatorNarration,
  CostData,
  RosterEntry,
  TaskArtifact,
  TaskFile,
  TaskStreamData,
  TraceEvent,
  TurnRecord,
} from "@/hooks/useTaskStream";

export interface NodeTypeSpec {
  entryType: string;
  icon: string;
  className: string;
  showInLegend: boolean;
}

export interface EdgeSpec {
  refType: string;
  stroke: "solid" | "dashed" | "dotted";
  animated: boolean;
  label?: string;
}

export interface NavigationPanelSpec {
  id: string;
  label: string;
  segment: string | null;
  feature: string;
  featureType: "panel" | "graph";
}

export interface VariantHydrationBundle {
  detail: unknown;
  board: unknown | null;
  turns: unknown | null;
  cost: unknown | null;
  logs: unknown | null;
  traces: unknown | null;
}

export const MAX_LIVE_LOGS = 2_000;
export const MAX_TRACE_EVENTS = 5_000;
export const MAX_REJECTED_ENTRIES = 500;
export const MAX_COORDINATOR_NARRATIONS = 500;
export const MAX_LIVE_FILE_EVENTS = 500;
export const MAX_LIVE_ARTIFACT_EVENTS = 1_000;

export interface DecodedVariantEvent {
  name: string;
  payload: Record<string, unknown>;
  taskId: string;
}

export interface VariantUIAdapter {
  id: string;
  label: string;
  aliases: readonly string[];
  supportedContractVersions: readonly string[];
  nodeTypes: readonly NodeTypeSpec[];
  edgeSpecs: readonly EdgeSpec[];
  navigationPanels: readonly NavigationPanelSpec[];
  graphViews: Readonly<Record<string, "turns" | "entry-references">>;
  decodeEvent: (name: string, value: unknown, taskId: string) => DecodedVariantEvent | null;
  projectEvent: (state: TaskStreamData, event: DecodedVariantEvent) => TaskStreamData;
  projectHydration: (
    state: TaskStreamData,
    bundle: VariantHydrationBundle,
    taskId: string,
  ) => TaskStreamData;
  progressLabel: (state: TaskStreamData, advertisedFeatures: readonly string[]) => string;
  ResultRenderer: ComponentType<{ content: string; formats: readonly string[] }>;
}

const CLASSIC_NODE_TYPES: readonly NodeTypeSpec[] = [
  { entryType: "objective", icon: "Target", className: "objective", showInLegend: true },
  { entryType: "attachment", icon: "Paperclip", className: "attachment", showInLegend: true },
  { entryType: "plan", icon: "ListTree", className: "plan", showInLegend: true },
  { entryType: "finding", icon: "Lightbulb", className: "finding", showInLegend: true },
  { entryType: "critique", icon: "AlertTriangle", className: "critique", showInLegend: true },
  { entryType: "rebuttal", icon: "MessageSquareReply", className: "rebuttal", showInLegend: true },
  { entryType: "conflict", icon: "GitMerge", className: "conflict", showInLegend: true },
  { entryType: "solution", icon: "CheckCircle2", className: "solution", showInLegend: true },
  { entryType: "artifact", icon: "FileCode2", className: "artifact", showInLegend: true },
];

const CLASSIC_EDGE_SPECS: readonly EdgeSpec[] = [
  { refType: "supports", stroke: "solid", animated: false, label: "supports" },
  { refType: "critiques", stroke: "dashed", animated: false, label: "critiques" },
  { refType: "rebuts", stroke: "dotted", animated: false, label: "rebuts" },
  { refType: "conflicts", stroke: "dashed", animated: true, label: "conflicts" },
  { refType: "resolves", stroke: "solid", animated: false, label: "resolves" },
  { refType: "refines", stroke: "solid", animated: false, label: "refines" },
  { refType: "attachment", stroke: "dotted", animated: false },
];

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function arrayFromEnvelope(value: unknown, field: string): Record<string, unknown>[] {
  if (Array.isArray(value)) {
    return value.filter((item) => asRecord(item)) as Record<string, unknown>[];
  }
  const record = asRecord(value);
  const items = record?.[field];
  return Array.isArray(items)
    ? items.filter((item) => asRecord(item)) as Record<string, unknown>[]
    : [];
}

function upsertBy<T>(items: readonly T[], item: T, getId: (value: T) => string): T[] {
  const id = getId(item);
  const index = items.findIndex((value) => getId(value) === id);
  if (index < 0) return [...items, item];
  return items.map((value, itemIndex) => itemIndex === index ? item : value);
}

function mergeBy<T>(current: readonly T[], incoming: readonly T[], getId: (value: T) => string): T[] {
  return incoming.reduce((items, item) => upsertBy(items, item, getId), [...current]);
}

function keepNewest<T>(items: readonly T[], limit: number): T[] {
  return items.length > limit ? items.slice(items.length - limit) : [...items];
}

function boundedUpsertBy<T>(
  items: readonly T[],
  item: T,
  getId: (value: T) => string,
  limit: number,
): T[] {
  return keepNewest(upsertBy(items, item, getId), limit);
}

function mapCost(value: unknown): CostData | null {
  const raw = asRecord(value);
  if (!raw) return null;
  const byModel: Record<string, { cost: number; tokens: number }> = {};
  let computedTokens = 0;
  let computedCost = 0;
  const modelRows = Array.isArray(raw.by_model) ? raw.by_model : [];
  for (const valueRow of modelRows) {
    const row = asRecord(valueRow);
    if (!row) continue;
    const model = typeof row.model === "string" ? row.model : "unknown";
    const tokens = Number(row.input_tokens ?? 0) + Number(row.output_tokens ?? 0);
    const cost = Number(row.cost_usd ?? 0);
    byModel[model] = { cost, tokens };
    computedTokens += tokens;
    computedCost += cost;
  }
  return {
    total_cost: Number(raw.total_cost_usd ?? raw.total_cost ?? computedCost),
    total_tokens: Number(
      raw.total_tokens
        ?? (raw.total_input_tokens != null
          ? Number(raw.total_input_tokens ?? 0) + Number(raw.total_output_tokens ?? 0)
          : computedTokens),
    ),
    by_model: byModel,
    by_phase: Array.isArray(raw.by_phase) ? raw.by_phase as CostData["by_phase"] : undefined,
    by_actor: Array.isArray(raw.by_actor) ? raw.by_actor as CostData["by_actor"] : undefined,
  };
}

function mergeCostSnapshots(current: CostData | null, hydrated: CostData | null): CostData | null {
  if (!current) return hydrated;
  if (!hydrated) return current;
  const byModel = { ...hydrated.by_model };
  for (const [model, value] of Object.entries(current.by_model)) {
    const saved = byModel[model];
    byModel[model] = saved
      ? { cost: Math.max(saved.cost, value.cost), tokens: Math.max(saved.tokens, value.tokens) }
      : value;
  }
  return {
    total_cost: Math.max(current.total_cost, hydrated.total_cost),
    total_tokens: Math.max(current.total_tokens, hydrated.total_tokens),
    by_model: byModel,
    by_phase: hydrated.by_phase ?? current.by_phase,
    by_actor: hydrated.by_actor ?? current.by_actor,
  };
}

function rosterFromBoard(value: unknown): RosterEntry[] {
  const board = asRecord(value);
  const meta = asRecord(board?.meta);
  const roster = Array.isArray(meta?.roster) ? meta.roster : [];
  return roster.flatMap((item) => {
    const record = asRecord(item);
    if (!record || typeof record.actor !== "string" || !record.actor) return [];
    return [{ actor: record.actor, ability: typeof record.ability === "string" ? record.ability : "" }];
  });
}

function rosterFromLog(raw: Record<string, unknown>): RosterEntry[] {
  let fields = raw.fields;
  if (typeof fields === "string") {
    try {
      fields = JSON.parse(fields) as unknown;
    } catch {
      return [];
    }
  }
  const record = asRecord(fields);
  if (record?.event !== "genesis" || !Array.isArray(record.roster)) return [];
  return record.roster.flatMap((item) => {
    const entry = asRecord(item);
    if (!entry || typeof entry.actor !== "string" || !entry.actor) return [];
    return [{ actor: entry.actor, ability: typeof entry.ability === "string" ? entry.ability : "" }];
  });
}

function mapTrace(raw: Record<string, unknown>): TraceEvent {
  const data = asRecord(raw.data);
  let content = typeof raw.content === "string"
    ? raw.content
    : typeof raw.message === "string" ? raw.message : "";
  if (!content && typeof data?.text === "string") content = data.text;
  if (!content && typeof data?.tool === "string") {
    content = `${data.tool}(${JSON.stringify(data.args ?? {}).slice(0, 200)})`;
  }
  if (!content && data) content = JSON.stringify(data).slice(0, 300);
  const turnId = String(raw.turn_id ?? "");
  const seq = Number(raw.seq ?? 0);
  const type = String(raw.type ?? raw.trace_type ?? "reasoning");
  const sourceId = String(raw.trace_id ?? turnId);
  const dataRunId = typeof data?.run_id === "string" ? data.run_id : undefined;
  return {
    id: `${sourceId}:${seq}:${type}`,
    turn_id: turnId,
    actor: String(raw.actor ?? raw.role ?? raw.agent_role ?? "unknown"),
    type,
    content,
    seq,
    timestamp: String(raw.timestamp ?? raw.ts ?? raw.created_at ?? ""),
    run_id: typeof raw.run_id === "string" ? raw.run_id : dataRunId,
    data: data ?? undefined,
  };
}

function mapLiveFile(raw: Record<string, unknown>): TaskFile {
  return {
    id: String(raw.file_id ?? raw.id ?? ""),
    name: String(raw.name ?? ""),
    mime: String(raw.mime ?? "application/octet-stream"),
    bytes: Number(raw.bytes ?? 0),
    sha256: String(raw.sha256 ?? ""),
    extracted_chars: Number(raw.extracted_chars ?? 0),
    created_at: String(raw.created_at ?? raw.timestamp ?? ""),
  };
}

function mapLiveArtifact(raw: Record<string, unknown>): TaskArtifact {
  return {
    id: String(raw.artifact_id ?? raw.id ?? ""),
    rel_path: String(raw.rel_path ?? ""),
    mime: typeof raw.mime === "string" ? raw.mime : null,
    bytes: Number(raw.bytes ?? 0),
    sha256: String(raw.sha256 ?? ""),
    version: Number(raw.version ?? 1),
    author: typeof raw.author === "string" ? raw.author : null,
    turn_id: typeof raw.turn_id === "string" ? raw.turn_id : null,
    created_at: String(raw.created_at ?? raw.timestamp ?? ""),
  };
}

function logIdentity(log: ReturnType<typeof mapLog>): string {
  return [
    log.agent_role,
    log.timestamp,
    log.turn_id ?? "",
    log.message,
  ].join("\u001f");
}

function startTurn(raw: Record<string, unknown>, taskId: string): TurnRecord {
  const turn = mapTurnRecord({ ...raw, task_id: raw.task_id ?? taskId });
  return {
    ...turn,
    status: String(raw.status ?? "active"),
    role: typeof raw.role === "string" ? raw.role : undefined,
    node: typeof raw.node === "string" ? raw.node : undefined,
    rationale: typeof raw.rationale === "string" ? raw.rationale : null,
  };
}

function finishTurn(
  state: TaskStreamData,
  raw: Record<string, unknown>,
  taskId: string,
): TaskStreamData {
  const turnId = String(raw.turn_id ?? raw.id ?? "");
  const active = state.activeTurns.find((turn) => turn.turn_id === turnId);
  const mapped = startTurn({ ...active, ...raw }, taskId);
  const completed = {
    ...mapped,
    status: String(raw.status ?? "completed"),
    ended_at: String(raw.ended_at ?? raw.completed_at ?? ""),
  };
  return {
    ...state,
    activeTurns: state.activeTurns.filter((turn) => turn.turn_id !== turnId),
    completedTurns: upsertBy(state.completedTurns, completed, (turn) => turn.turn_id),
  };
}

function projectClassicEvent(
  state: TaskStreamData,
  event: DecodedVariantEvent,
): TaskStreamData {
  const raw = event.payload;
  switch (event.name) {
    case "initial_state": {
      const task = asRecord(raw.task);
      const subTasks = arrayFromEnvelope(raw.sub_tasks, "sub_tasks").map(mapSubTask);
      return {
        ...state,
        taskMeta: task ? mapTaskMeta(task) : state.taskMeta,
        subTasks: subTasks.length ? mergeBy(state.subTasks, subTasks, (item) => item.id) : state.subTasks,
        result: typeof task?.result_summary === "string" ? task.result_summary : state.result,
        error: typeof task?.error_message === "string" ? task.error_message : state.error,
        isLive: true,
      };
    }
    case "phase":
      return { ...state, phase: String(raw.phase ?? state.phase ?? "") };
    case "subtask": {
      const subTask = mapSubTask(raw);
      return { ...state, subTasks: upsertBy(state.subTasks, subTask, (item) => item.id) };
    }
    case "debate": {
      const debate = mapDebate(raw, state.debates.length);
      return { ...state, debates: upsertBy(state.debates, debate, (item) => item.id) };
    }
    case "log": {
      const log = mapLog(raw, state.logs.length);
      const roster = rosterFromLog(raw);
      return {
        ...state,
        logs: boundedUpsertBy(state.logs, log, logIdentity, MAX_LIVE_LOGS),
        roster: roster.length ? roster : state.roster,
      };
    }
    case "cost": {
      const current = state.cost ?? { total_cost: 0, total_tokens: 0, by_model: {} };
      const model = String(raw.model ?? "unknown");
      const tokens = Number(raw.input_tokens ?? 0) + Number(raw.output_tokens ?? 0);
      const cost = Number(raw.cost_usd ?? 0);
      const existing = current.by_model[model] ?? { cost: 0, tokens: 0 };
      return {
        ...state,
        cost: {
          ...current,
          total_cost: current.total_cost + cost,
          total_tokens: current.total_tokens + tokens,
          by_model: {
            ...current.by_model,
            [model]: { cost: existing.cost + cost, tokens: existing.tokens + tokens },
          },
        },
      };
    }
    case "complete": {
      const taskMeta = state.taskMeta
        ? { ...state.taskMeta, status: "completed" as const }
        : mapTaskMeta({ ...raw, status: "completed" });
      return {
        ...state,
        isLive: false,
        result: typeof raw.answer === "string"
          ? raw.answer
          : typeof raw.result_summary === "string" ? raw.result_summary : state.result,
        taskMeta,
      };
    }
    case "error": {
      const taskMeta = state.taskMeta
        ? { ...state.taskMeta, status: "failed" as const }
        : mapTaskMeta({ ...raw, status: "failed" });
      return {
        ...state,
        isLive: false,
        error: String(raw.error_message ?? raw.error ?? "Task failed"),
        taskMeta,
      };
    }
    case "board_entry": {
      const entry = mapBoardEntry(raw, state.boardEntries.length);
      return { ...state, boardEntries: upsertBy(state.boardEntries, entry, (item) => item.id) };
    }
    case "entry_removed": {
      const id = String(raw.entry_id ?? raw.id ?? "");
      if (!id || state.removedEntryIds.includes(id)) return state;
      return { ...state, removedEntryIds: [...state.removedEntryIds, id] };
    }
    case "entry_status_changed": {
      const id = String(raw.entry_id ?? raw.id ?? "");
      if (!id) return state;
      const salience = Number(raw.salience);
      return {
        ...state,
        boardEntries: state.boardEntries.map((entry) => entry.id === id
          ? {
            ...entry,
            ...(typeof raw.status === "string" ? { status: raw.status } : {}),
            ...(Number.isFinite(salience) ? { salience } : {}),
          }
          : entry),
      };
    }
    case "consensus":
      return {
        ...state,
        consensus: {
          signal: Number(raw.signal ?? raw.convergence ?? 0),
          decider_state: String(raw.decider_state ?? raw.state ?? "evaluating"),
        },
      };
    case "turn_start": {
      const turn = startTurn(raw, event.taskId);
      return { ...state, activeTurns: upsertBy(state.activeTurns, turn, (item) => item.turn_id) };
    }
    case "turn_end":
      return finishTurn(state, raw, event.taskId);
    case "agent_turn": {
      const status = String(raw.status ?? "");
      return status === "completed" || status === "failed"
        ? finishTurn(state, raw, event.taskId)
        : projectClassicEvent(state, { ...event, name: "turn_start" });
    }
    case "trace": {
      const trace = mapTrace(raw);
      return {
        ...state,
        traceEvents: boundedUpsertBy(
          state.traceEvents,
          trace,
          (item) => item.id,
          MAX_TRACE_EVENTS,
        ),
      };
    }
    case "entry_rejected":
      return {
        ...state,
        rejectedEntries: boundedUpsertBy(
          state.rejectedEntries,
          {
            entry_id: String(raw.entry_id ?? raw.id ?? ""),
            actor: String(raw.actor ?? "unknown"),
            reason: String(raw.reason ?? "Unknown reason"),
            timestamp: String(raw.timestamp ?? ""),
          },
          (item) => `${item.entry_id}:${item.actor}:${item.reason}`,
          MAX_REJECTED_ENTRIES,
        ),
      };
    case "approval_request":
      return {
        ...state,
        approvalRequests: upsertBy(
          state.approvalRequests,
          {
            turn_id: String(raw.turn_id ?? ""),
            actor: String(raw.actor ?? "unknown"),
            run_id: String(raw.run_id ?? ""),
            description: String(raw.description ?? ""),
            timestamp: String(raw.timestamp ?? ""),
          },
          (item) => `${item.turn_id}:${item.run_id}`,
        ),
      };
    case "approval_response": {
      const runId = String(raw.run_id ?? "");
      return {
        ...state,
        approvalRequests: state.approvalRequests.filter(
          (item) => item.run_id !== runId,
        ),
      };
    }
    case "paused":
      return { ...state, isPaused: true };
    case "resumed":
      return { ...state, isPaused: false };
    case "budget":
      return {
        ...state,
        budgetState: {
          spent: Number(raw.spent ?? 0),
          ceiling: Number(raw.ceiling ?? 0),
          percentage: Number(raw.percentage ?? 0),
        },
      };
    case "coordinator_narration": {
      const narration: CoordinatorNarration = {
        round: Number(raw.round ?? 0),
        selected: Array.isArray(raw.selected) ? raw.selected.map(String) : [],
        rationale: typeof raw.rationale === "string" ? raw.rationale : null,
        source: String(raw.source ?? "unknown"),
        timestamp: String(raw.timestamp ?? ""),
      };
      return {
        ...state,
        coordinatorNarrations: boundedUpsertBy(
          state.coordinatorNarrations,
          narration,
          (item) => `${item.round}:${item.source}`,
          MAX_COORDINATOR_NARRATIONS,
        ),
      };
    }
    case "file_added": {
      const file = mapLiveFile(raw);
      if (!file.id) return state;
      return {
        ...state,
        liveFiles: boundedUpsertBy(
          state.liveFiles,
          file,
          (item) => item.id,
          MAX_LIVE_FILE_EVENTS,
        ),
      };
    }
    case "artifact_created": {
      const artifact = mapLiveArtifact(raw);
      if (!artifact.id) return state;
      return {
        ...state,
        liveArtifacts: boundedUpsertBy(
          state.liveArtifacts,
          artifact,
          (item) => item.id,
          MAX_LIVE_ARTIFACT_EVENTS,
        ),
      };
    }
    default:
      return state;
  }
}

function projectClassicHydration(
  state: TaskStreamData,
  bundle: VariantHydrationBundle,
  taskId: string,
): TaskStreamData {
  const detailEnvelope = asRecord(bundle.detail);
  const task = asRecord(detailEnvelope?.task);
  const subTasks = arrayFromEnvelope(detailEnvelope?.sub_tasks, "sub_tasks").map(mapSubTask);
  const boardEntries = arrayFromEnvelope(bundle.board, "entries").map(mapBoardEntry);
  const turns = arrayFromEnvelope(bundle.turns, "turns").map((turn) =>
    mapTurnRecord({ ...turn, task_id: turn.task_id ?? taskId }),
  );
  const roster = rosterFromBoard(bundle.board);
  const logs = arrayFromEnvelope(bundle.logs, "entries").map((log, index) =>
    mapLog({ ...log, timestamp: log.timestamp ?? log.created_at }, index),
  );
  const traces = arrayFromEnvelope(bundle.traces, "traces").map(mapTrace);
  const hydratedMeta = task ? mapTaskMeta(task) : null;
  const liveTerminal = state.taskMeta?.status === "completed" || state.taskMeta?.status === "failed";
  const hydratedCost = mapCost(bundle.cost);
  const terminalHydration = hydratedMeta?.status === "completed" || hydratedMeta?.status === "failed";
  return {
    ...state,
    taskMeta: liveTerminal ? state.taskMeta : hydratedMeta ?? state.taskMeta,
    subTasks: mergeBy(subTasks, state.subTasks, (item) => item.id),
    result: state.result ?? (typeof task?.result_summary === "string" ? task.result_summary : null),
    error: state.error ?? (typeof task?.error_message === "string" ? task.error_message : null),
    cost: terminalHydration ? hydratedCost ?? state.cost : mergeCostSnapshots(state.cost, hydratedCost),
    boardEntries: mergeBy(boardEntries, state.boardEntries, (item) => item.id),
    completedTurns: mergeBy(turns, state.completedTurns, (item) => item.turn_id),
    logs: keepNewest(mergeBy(logs, state.logs, logIdentity), MAX_LIVE_LOGS),
    traceEvents: keepNewest(
      mergeBy(traces, state.traceEvents, (item) => item.id),
      MAX_TRACE_EVENTS,
    ),
    roster: state.roster.length ? state.roster : roster,
    isLive: hydratedMeta?.status === "running"
      && !["blocked", "paused", "pause_requested"].includes(hydratedMeta.run_state ?? "")
      ? state.isLive
      : false,
  };
}

export const CLASSIC_ADAPTER: VariantUIAdapter = {
  id: "classic",
  label: "Classic blackboard",
  aliases: ["traditional"],
  supportedContractVersions: CLASSIC_CONTRACT_VERSIONS,
  nodeTypes: CLASSIC_NODE_TYPES,
  edgeSpecs: CLASSIC_EDGE_SPECS,
  navigationPanels: [
    { id: "overview", label: "Summary", segment: null, feature: "mission", featureType: "panel" },
    { id: "blackboard", label: "Blackboard", segment: "mission", feature: "blackboard", featureType: "panel" },
    { id: "graph", label: "Execution", segment: "dag", feature: "turns", featureType: "graph" },
    { id: "logs", label: "Logs", segment: "logs", feature: "logs", featureType: "panel" },
    { id: "files", label: "Files", segment: "files", feature: "artifacts", featureType: "panel" },
  ],
  graphViews: { turns: "turns", entry_references: "entry-references" },
  decodeEvent(name, value, taskId) {
    const payload = asRecord(value);
    return payload ? { name, payload, taskId } : null;
  },
  projectEvent: projectClassicEvent,
  projectHydration: projectClassicHydration,
  progressLabel(state, advertisedFeatures) {
    const parts: string[] = [];
    if (advertisedFeatures.includes("round")) {
      const round = Math.max(0, ...state.activeTurns.map((turn) => turn.round_no));
      if (round > 0) parts.push(`Round ${round}`);
    }
    if (advertisedFeatures.includes("phase") && state.phase) parts.push(state.phase);
    if (advertisedFeatures.includes("effective_actions")) {
      parts.push(`${state.completedTurns.length} effective actions`);
    }
    return parts.join(" · ") || "Initializing…";
  },
  ResultRenderer: ClassicResultRenderer,
};

const WORKFLOW_PANELS: readonly NavigationPanelSpec[] = [
  { id: "overview", label: "Summary", segment: null, feature: "mission", featureType: "panel" },
  { id: "graph", label: "Execution", segment: "dag", feature: "turns", featureType: "graph" },
  { id: "logs", label: "Logs", segment: "logs", feature: "logs", featureType: "panel" },
  { id: "files", label: "Files", segment: "files", feature: "artifacts", featureType: "panel" },
];

function workflowAdapter(
  id: string,
  label: string,
  supportedContractVersions: readonly string[],
): VariantUIAdapter {
  return {
    id,
    label,
    aliases: [],
    supportedContractVersions,
    nodeTypes: [],
    edgeSpecs: [],
    navigationPanels: WORKFLOW_PANELS,
    graphViews: { turns: "turns" },
    decodeEvent(name, value, taskId) {
      const payload = asRecord(value);
      return payload ? { name, payload, taskId } : null;
    },
    projectEvent: projectClassicEvent,
    projectHydration: projectClassicHydration,
    progressLabel(state, advertisedFeatures) {
      const parts: string[] = [];
      if (advertisedFeatures.includes("phase") && state.phase) parts.push(state.phase);
      if (advertisedFeatures.includes("effective_actions")) {
        parts.push(`${state.completedTurns.length} completed turns`);
      }
      return parts.join(" · ") || "Initializing…";
    },
    ResultRenderer: ClassicResultRenderer,
  };
}

export const PATCHBOARD_ADAPTER = workflowAdapter(
  "patchboard",
  "Patchboard",
  PATCHBOARD_CONTRACT_VERSIONS,
);

export const STIGMERGIC_ADAPTER = workflowAdapter(
  "stigmergic",
  "Stigmergic workspace",
  STIGMERGIC_CONTRACT_VERSIONS,
);

const ADAPTERS = new Map<string, VariantUIAdapter>();

export function registerAdapter(adapter: VariantUIAdapter): void {
  ADAPTERS.set(adapter.id, adapter);
}

registerAdapter(CLASSIC_ADAPTER);
registerAdapter(PATCHBOARD_ADAPTER);
registerAdapter(STIGMERGIC_ADAPTER);

export function getActiveAdapter(variantId: string | null | undefined): VariantUIAdapter | null {
  if (!variantId) return null;
  const direct = ADAPTERS.get(variantId);
  if (direct) return direct;
  return [...ADAPTERS.values()].find((adapter) => adapter.aliases.includes(variantId)) ?? null;
}

export function listAdapters(): VariantUIAdapter[] {
  return [...ADAPTERS.values()];
}

export function adapterSupportsCapability(
  adapter: VariantUIAdapter,
  capability: VariantCapability,
): boolean {
  return adapter.id === capability.id
    && adapter.supportedContractVersions.includes(capability.contract_version);
}

export function visibleNavigationPanels(
  adapter: VariantUIAdapter,
  capability: VariantCapability,
): NavigationPanelSpec[] {
  return adapter.navigationPanels.filter((panel) => {
    const features = panel.featureType === "graph"
      ? capability.features.graphs
      : capability.features.panels;
    return features.includes(panel.feature);
  });
}
