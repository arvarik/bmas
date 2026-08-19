import type { TraceEvent, TurnRecord } from "@/hooks/useTaskStream";

export interface SearchableLogLine {
  agent: string;
  level: string;
  message: string;
  node?: string;
  turnId?: string;
  fields?: Record<string, unknown> | null;
}

function searchableJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? "").toLowerCase();
  } catch {
    return "";
  }
}

export function matchesLogQuery(line: SearchableLogLine, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [
    line.agent,
    line.level,
    line.message,
    line.node ?? "",
    line.turnId ?? "",
    searchableJson(line.fields),
  ].some((value) => value.toLowerCase().includes(normalized));
}

export function matchesTraceQuery(trace: TraceEvent, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [
    trace.actor,
    trace.type,
    trace.content,
    trace.turn_id,
    trace.run_id ?? "",
    searchableJson(trace.data),
  ].some((value) => value.toLowerCase().includes(normalized));
}

export function matchesTurnQuery(turn: TurnRecord, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [
    turn.actor,
    turn.role ?? "",
    turn.model ?? "",
    turn.node ?? "",
    turn.phase,
    turn.status,
    turn.rationale ?? "",
    turn.turn_id,
    `round ${turn.round_no}`,
  ].some((value) => value.toLowerCase().includes(normalized));
}

export function parseListParam(value: string | null): Set<string> {
  return new Set(
    (value ?? "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  );
}

export function updateUrlParams(
  currentSearch: string,
  updates: Record<string, string | readonly string[] | null>,
): string {
  const params = new URLSearchParams(currentSearch);
  for (const [key, value] of Object.entries(updates)) {
    const serialized = typeof value === "string"
      ? value
      : value
        ? Array.from(value).sort().join(",")
        : null;
    if (serialized) params.set(key, serialized);
    else params.delete(key);
  }
  const next = params.toString();
  return next ? `?${next}` : "";
}

function csvCell(value: unknown): string {
  const rawText = typeof value === "string" ? value : JSON.stringify(value ?? "");
  const text = /^[=+\-@]/.test(rawText) ? `'${rawText}` : rawText;
  return `"${text.replaceAll('"', '""')}"`;
}

export function logsToCsv(lines: SearchableLogLine[]): string {
  const header = ["timestamp", "level", "agent", "node", "turn_id", "message", "fields"];
  const rows = lines.map((line) => [
    "timestamp" in line ? (line as SearchableLogLine & { timestamp?: string }).timestamp ?? "" : "",
    line.level,
    line.agent,
    line.node ?? "",
    line.turnId ?? "",
    line.message,
    line.fields ?? "",
  ]);
  return [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\n");
}

export function turnsToCsv(turns: TurnRecord[]): string {
  const header = ["turn_id", "round", "actor", "role", "status", "phase", "model", "node", "started_at", "ended_at", "tokens_in", "tokens_out", "cost_usd", "rationale"];
  const rows = turns.map((turn) => [
    turn.turn_id,
    turn.round_no,
    turn.actor,
    turn.role ?? "",
    turn.status,
    turn.phase,
    turn.model ?? "",
    turn.node ?? "",
    turn.started_at,
    turn.ended_at ?? "",
    turn.tokens_in ?? "",
    turn.tokens_out ?? "",
    turn.cost_usd ?? "",
    turn.rationale ?? "",
  ]);
  return [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\n");
}
