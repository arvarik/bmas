/**
 * Field mappers — converts daemon API shapes to frontend types.
 *
 * Centralizes all field-name translation between the daemon's snake_case
 * response shapes and the frontend TypeScript interfaces. Extracted from
 * useTaskStream for testability and reuse.
 *
 * All mappers are pure functions with no side effects.
 */

import type {
  SubTask,
  TaskStatus,
  DebateEntry,
  LogEntry,
  TaskMeta,
  BoardEntry,
  TurnRecord,
} from "@/hooks/useTaskStream";

// ── SubTask mapper ──────────────────────────────────────────────────────

/** Map daemon sub-task shape (agent_role) → frontend SubTask (agent). */
export function mapSubTask(raw: Record<string, unknown>): SubTask {
  return {
    id: raw.id as string,
    label: (raw.label as string) ?? "",
    status: (raw.status as TaskStatus) ?? "pending",
    agent: ((raw.agent ?? raw.agent_role) as SubTask["agent"]) ?? "planner",
    depends_on: (raw.depends_on as string[]) ?? [],
    result: raw.result as string | undefined,
    error: raw.error as string | undefined,
    started_at: raw.started_at as string | undefined,
    completed_at: raw.completed_at as string | undefined,
  };
}

// ── DebateEntry mapper ──────────────────────────────────────────────────

/**
 * Map daemon debate shape (created_at) → frontend DebateEntry (timestamp).
 *
 * @param index - Fallback index for generating a stable id when the daemon
 *   omits one (older daemon versions before Phase 3).
 */
export function mapDebate(
  raw: Record<string, unknown>,
  index: number,
): DebateEntry {
  return {
    id: (raw.id as string) ?? `debate-${index}`,
    agent_role: (raw.agent_role as string) ?? "unknown",
    content: (raw.content as string) ?? "",
    timestamp:
      ((raw.timestamp ?? raw.created_at) as string) ?? new Date().toISOString(),
  };
}

// ── LogEntry mapper ─────────────────────────────────────────────────────

/**
 * Map daemon log event (ts) → frontend LogEntry (timestamp).
 *
 * The daemon uses both `ts` (Redis stream field) and `timestamp`
 * (pub/sub payload field) — both are normalised here.
 */
export function mapLog(
  raw: Record<string, unknown>,
  index: number,
): LogEntry {
  return {
    id: (raw.id as string) ?? `log-${index}`,
    agent_role: (raw.agent_role as string) ?? "daemon",
    level: (raw.level as string) ?? "info",
    message: (raw.message as string) ?? "",
    timestamp:
      ((raw.timestamp ?? raw.ts) as string) ?? new Date().toISOString(),
    node: (raw.node as string) ?? undefined,
    turn_id: (raw.turn_id as string) ?? undefined,
    fields: (raw.fields as Record<string, unknown> | null) ?? null,
  };
}

// ── TaskMeta mapper ─────────────────────────────────────────────────────

/**
 * Map daemon task row → frontend TaskMeta.
 *
 * Handles both `id` and `task_id` field names (daemon returns `id` in
 * REST responses, `task_id` in SSE payloads).
 */
export function mapTaskMeta(task: Record<string, unknown>): TaskMeta {
  return {
    task_id: (task.id ?? task.task_id) as string,
    label: (task.label as string) ?? "",
    status: (task.status as TaskStatus) ?? "pending",
    complexity: task.complexity as string | undefined,
    model: (task.model ?? task.model_used) as string | undefined,
    variant: (task.variant as string) ?? undefined,
    created_at: (task.created_at as string) ?? "",
    completed_at: task.completed_at as string | undefined,
    duration_ms: task.duration_ms as number | undefined,
    full_input: task.full_input as string | undefined,
  };
}

// ── BoardEntry mapper ───────────────────────────────────────────────────

/**
 * Map raw board entry shape → frontend BoardEntry.
 *
 * Handles both `id` and `entry_id`, `body` and `content`, `author` and
 * `actor` field names for backward compat across daemon versions.
 */
export function mapBoardEntry(
  raw: Record<string, unknown>,
  idx: number,
): BoardEntry {
  return {
    id: (raw.id ?? raw.entry_id ?? `e-${idx}`) as string,
    type: (raw.type ?? raw.entry_type ?? "finding") as string,
    title: (raw.title ?? "") as string,
    body: (raw.body ?? raw.content ?? "") as string,
    author: (raw.author ?? raw.actor ?? "unknown") as string,
    refs: (raw.refs ?? []) as string[],
    confidence: (raw.confidence ?? 0) as number,
    salience: (raw.salience ?? 0) as number,
    seq: (raw.seq ?? idx) as number,
    created_at: (raw.created_at ?? "") as string,
    round: raw.round as number | undefined,
    status: (raw.status as string) ?? undefined,
  };
}

// ── TurnRecord mapper ───────────────────────────────────────────────────

/**
 * Map raw turn record → frontend TurnRecord.
 *
 * Handles both `round_no` and `round`, `tokens_in`/`tokens_out` and
 * `input_tokens`/`output_tokens` field name variants.
 */
export function mapTurnRecord(raw: Record<string, unknown>): TurnRecord {
  return {
    turn_id: (raw.turn_id ?? raw.id ?? "") as string,
    task_id: (raw.task_id ?? "") as string,
    actor: (raw.actor ?? raw.role ?? "unknown") as string,
    round_no: (raw.round_no ?? raw.round ?? 0) as number,
    phase: (raw.phase ?? "completed") as string,
    status: (raw.status ?? "completed") as string,
    started_at: (raw.started_at ?? raw.created_at ?? "") as string,
    ended_at: (raw.ended_at ?? raw.completed_at) as string | undefined,
    tokens_in: (raw.tokens_in ?? raw.input_tokens) as number | undefined,
    tokens_out: (raw.tokens_out ?? raw.output_tokens) as number | undefined,
    cost_usd: raw.cost_usd as number | undefined,
    model: raw.model as string | undefined,
  };
}
