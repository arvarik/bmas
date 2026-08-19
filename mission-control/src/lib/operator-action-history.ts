import type { TaskOperatorAction } from "@/hooks/useTaskOperatorAction";

export const OPERATOR_HISTORY_VERSION = 1;
export const OPERATOR_HISTORY_LIMIT = 20;

export interface StoredOperatorAction {
  id: string;
  action: TaskOperatorAction;
  label: string;
  timestamp: string;
}

interface StoredOperatorHistory {
  version: number;
  events: StoredOperatorAction[];
}

export function operatorHistoryKey(taskId: string): string {
  return `bmas:operator-actions:v${OPERATOR_HISTORY_VERSION}:${taskId}`;
}

export function parseOperatorHistory(value: string | null): StoredOperatorAction[] {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value) as Partial<StoredOperatorHistory>;
    if (parsed.version !== OPERATOR_HISTORY_VERSION || !Array.isArray(parsed.events)) return [];
    return parsed.events
      .filter((event): event is StoredOperatorAction => Boolean(
        event &&
        typeof event.id === "string" &&
        typeof event.action === "string" &&
        typeof event.label === "string" &&
        typeof event.timestamp === "string",
      ))
      .slice(-OPERATOR_HISTORY_LIMIT);
  } catch {
    return [];
  }
}

export function serializeOperatorHistory(events: StoredOperatorAction[]): string {
  return JSON.stringify({
    version: OPERATOR_HISTORY_VERSION,
    events: events.slice(-OPERATOR_HISTORY_LIMIT),
  } satisfies StoredOperatorHistory);
}
