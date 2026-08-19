export interface HermesSessionRecord {
  id: string;
  title?: string | null;
  source?: string;
  model?: string | null;
  preview?: string | null;
  parent_session_id?: string | null;
  task_id?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface HermesMessageRecord {
  role?: string;
  content?: unknown;
  tool_name?: string;
}

function searchableText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function filterHermesSessions<T extends HermesSessionRecord>(
  sessions: readonly T[],
  query: string,
): T[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return [...sessions];
  return sessions.filter((session) => [
    session.id,
    session.title,
    session.source,
    session.model,
    session.preview,
    session.parent_session_id,
    session.task_id,
  ].some((value) => typeof value === "string" && value.toLowerCase().includes(normalized)));
}

export function filterHermesMessages<T extends HermesMessageRecord>(
  messages: readonly T[],
  query: string,
): T[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return [...messages];
  return messages.filter((message) => [
    message.role,
    message.tool_name,
    searchableText(message.content),
  ].some((value) => typeof value === "string" && value.toLowerCase().includes(normalized)));
}

export function taskIdForSession(
  session: HermesSessionRecord | null,
  nodeRole: string,
): string | null {
  if (!session) return null;
  const metadataTask = typeof session.metadata?.task_id === "string"
    ? session.metadata.task_id
    : null;
  const explicit = session.task_id ?? metadataTask;
  if (explicit && /^[a-zA-Z0-9_-]{1,64}$/.test(explicit)) return explicit;

  const sourceMatch = session.source?.match(/^bmas(?::|\/)([a-zA-Z0-9_-]{1,64})$/i);
  if (sourceMatch?.[1]) return sourceMatch[1];

  const idMatch = session.id.match(/^([a-zA-Z0-9_-]{1,64}):([a-zA-Z0-9_-]{1,64})$/);
  return idMatch?.[2] === nodeRole ? idMatch[1] : null;
}

export function sessionLineage<T extends HermesSessionRecord>(
  sessions: readonly T[],
  selectedId: string | null,
): { ancestors: T[]; children: T[] } {
  if (!selectedId) return { ancestors: [], children: [] };
  const byId = new Map(sessions.map((session) => [session.id, session]));
  const selected = byId.get(selectedId);
  const ancestors: T[] = [];
  const seen = new Set<string>([selectedId]);
  let parentId = selected?.parent_session_id ?? null;
  while (parentId && !seen.has(parentId)) {
    const parent = byId.get(parentId);
    if (!parent) break;
    ancestors.unshift(parent);
    seen.add(parent.id);
    parentId = parent.parent_session_id ?? null;
  }
  const children = sessions.filter((session) => session.parent_session_id === selectedId);
  return { ancestors, children };
}
