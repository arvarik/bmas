"use client";

/**
 * boardModel — shared data model + persistence hook for the Blackboard view.
 *
 * The Blackboard tab visualizes the shared knowledge entries agents post
 * and the debate that plays out over them. Entries arrive from two sources:
 *
 *   1. The live SSE stream (board_entry events) — low-latency, but ephemeral
 *      and missing some envelope fields (status/round).
 *   2. The durable Redis snapshot via GET /api/tasks/{id}/board — authoritative
 *      and complete (status, round, salience), persisted with no TTL.
 *
 * `useBoardEntries` merges both into a single append-only map keyed by entry
 * id. Entries are NEVER deleted from the map — removed/superseded entries flip
 * status instead. This is the core fix for the "board disappears" bug: even if
 * the SSE-driven context resets to an empty array (remount / reconnect / a
 * completed task with no live events), the durable snapshot keeps the board
 * populated, and the local map retains everything already seen.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { BoardEntry } from "@/hooks/useTaskStream";
import {
  Target,
  Paperclip,
  ListTree,
  Lightbulb,
  AlertTriangle,
  MessageSquareReply,
  GitMerge,
  CheckCircle2,
  FileCode2,
  Megaphone,
  StickyNote,
  Archive,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

export type EntryStatus = "open" | "superseded" | "removed";

export interface MergedBoardEntry {
  id: string;
  type: string;
  title: string;
  body: string;
  author: string;
  refs: string[];
  confidence: number;
  salience: number;
  seq: number;
  round: number;
  status: EntryStatus;
  created_at: string;
}

// ── Entry type metadata (color + icon + label) ────────────────────────
// Colors are chosen to read clearly on the dark surface palette and to
// semantically separate the debate roles (proposal / critique / resolution).

export interface EntryTypeMeta {
  label: string;
  icon: LucideIcon;
  color: string;
  /** True for entries that drive the debate (vs. static knowledge). */
  debate?: boolean;
}

export const TYPE_META: Record<string, EntryTypeMeta> = {
  objective: { label: "Objective", icon: Target, color: "hsl(217, 91%, 62%)" },
  attachment: { label: "Attachment", icon: Paperclip, color: "hsl(220, 12%, 55%)" },
  plan: { label: "Plan", icon: ListTree, color: "hsl(265, 60%, 66%)" },
  finding: { label: "Finding", icon: Lightbulb, color: "hsl(175, 60%, 48%)" },
  condensed_finding: { label: "Condensed", icon: Archive, color: "hsl(190, 20%, 50%)" },
  critique: { label: "Critique", icon: AlertTriangle, color: "hsl(350, 72%, 62%)", debate: true },
  rebuttal: { label: "Rebuttal", icon: MessageSquareReply, color: "hsl(199, 80%, 58%)", debate: true },
  conflict: { label: "Conflict", icon: GitMerge, color: "hsl(32, 88%, 58%)", debate: true },
  directive: { label: "Directive", icon: Megaphone, color: "hsl(38, 92%, 56%)" },
  solution: { label: "Solution", icon: CheckCircle2, color: "hsl(142, 71%, 48%)", debate: true },
  artifact: { label: "Artifact", icon: FileCode2, color: "hsl(190, 75%, 52%)" },
};

export function typeMeta(type: string): EntryTypeMeta {
  return TYPE_META[type] ?? { label: type || "Entry", icon: StickyNote, color: "hsl(220, 12%, 60%)" };
}

/** Canonical type ordering for the "by type" board layout. */
export const TYPE_ORDER: string[] = [
  "objective",
  "directive",
  "plan",
  "finding",
  "condensed_finding",
  "critique",
  "rebuttal",
  "conflict",
  "solution",
  "artifact",
  "attachment",
];

// ── Salience → heat color ─────────────────────────────────────────────

export function salienceColor(s: number): string {
  const v = Math.max(0, Math.min(1, s));
  // muted slate (low) → blue → amber (high)
  if (v < 0.5) {
    return `hsl(${217}, ${40 + v * 80}%, ${52}%)`;
  }
  const t = (v - 0.5) / 0.5;
  const hue = 217 - t * 179; // 217 (blue) → 38 (amber)
  return `hsl(${hue}, 88%, 56%)`;
}

// ── Normalizers ───────────────────────────────────────────────────────

function asStatus(s: unknown): EntryStatus {
  return s === "superseded" || s === "removed" ? s : "open";
}

function normalizeLive(e: BoardEntry): MergedBoardEntry {
  return {
    id: e.id,
    type: e.type || "finding",
    title: e.title || "",
    body: e.body || "",
    author: e.author || "unknown",
    refs: Array.isArray(e.refs) ? e.refs : [],
    confidence: e.confidence ?? 0,
    salience: e.salience ?? 0,
    seq: e.seq ?? seqFromId(e.id),
    round: e.round ?? 0,
    status: asStatus(e.status),
    created_at: e.created_at || "",
  };
}

function normalizeSnapshot(raw: Record<string, unknown>): MergedBoardEntry {
  const id = (raw.id ?? raw.entry_id ?? "") as string;
  return {
    id,
    type: (raw.type ?? raw.entry_type ?? "finding") as string,
    title: (raw.title ?? "") as string,
    body: (raw.body ?? raw.content ?? "") as string,
    author: (raw.author ?? raw.actor ?? "unknown") as string,
    refs: (Array.isArray(raw.refs) ? raw.refs : []) as string[],
    confidence: (raw.confidence ?? 0) as number,
    salience: (raw.salience ?? 0) as number,
    seq: (raw.seq ?? seqFromId(id)) as number,
    round: (raw.round ?? 0) as number,
    status: asStatus(raw.status),
    created_at: (raw.created_at ?? "") as string,
  };
}

export function seqFromId(id: string): number {
  if (!id) return 0;
  const m = id.match(/(\d+)\s*$/);
  return m ? parseInt(m[1], 10) : 0;
}

// ── Merge / persistence hook ──────────────────────────────────────────

export interface UseBoardEntriesResult {
  entries: MergedBoardEntry[];
  /** True once the durable snapshot has been fetched at least once. */
  synced: boolean;
  /** Manually trigger a snapshot refetch. */
  refresh: () => void;
}

export function useBoardEntries(
  taskId: string,
  liveEntries: BoardEntry[],
  removedIds: string[],
  isLive: boolean,
): UseBoardEntriesResult {
  // The durable snapshot is the authoritative source of truth; the live
  // SSE entries are merged on top to keep the board low-latency. Because the
  // merge is a pure derivation (useMemo) over the fetched snapshot, the board
  // never empties when the SSE-driven `liveEntries` array resets — the last
  // snapshot keeps it populated. This is the core "never disappears" guarantee.
  const [snapshot, setSnapshot] = useState<MergedBoardEntry[]>([]);
  const [synced, setSynced] = useState(false);

  const fetchSnapshot = useCallback(async () => {
    if (!taskId) return;
    try {
      const res = await fetch(`/api/tasks/${taskId}/board`, { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const arr: Record<string, unknown>[] = Array.isArray(data)
        ? data
        : (data.entries ?? []);
      const mapped = arr.map(normalizeSnapshot).filter((e) => e.id);
      setSnapshot(mapped);
    } catch {
      // Snapshot fetch is best-effort; the last known snapshot stays intact.
    } finally {
      setSynced(true);
    }
  }, [taskId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async snapshot fetch / poll
    void fetchSnapshot();
    if (!isLive) return;
    const iv = setInterval(() => void fetchSnapshot(), 4000);
    return () => clearInterval(iv);
  }, [fetchSnapshot, isLive]);

  const entries = useMemo(() => {
    const map = new Map<string, MergedBoardEntry>();
    // Live entries first (lower priority) — gives progressive rendering
    // before the first snapshot fetch lands.
    for (const e of liveEntries) {
      if (e?.id) map.set(e.id, normalizeLive(e));
    }
    // Durable snapshot overwrites with authoritative status/round/salience.
    for (const e of snapshot) {
      map.set(e.id, e);
    }
    // Overlay any pending live removals not yet in the snapshot.
    for (const id of removedIds) {
      const ex = map.get(id);
      if (ex && ex.status !== "removed") map.set(id, { ...ex, status: "removed" });
    }
    return Array.from(map.values()).sort(
      (a, b) => a.seq - b.seq || a.id.localeCompare(b.id),
    );
  }, [liveEntries, snapshot, removedIds]);

  return { entries, synced, refresh: fetchSnapshot };
}

// ── Grouping helpers ──────────────────────────────────────────────────

export type GroupMode = "round" | "type" | "author";

export interface EntryGroup {
  key: string;
  label: string;
  sublabel?: string;
  entries: MergedBoardEntry[];
}

export function groupEntries(entries: MergedBoardEntry[], mode: GroupMode): EntryGroup[] {
  if (mode === "type") {
    const byType = new Map<string, MergedBoardEntry[]>();
    for (const e of entries) {
      if (!byType.has(e.type)) byType.set(e.type, []);
      byType.get(e.type)!.push(e);
    }
    return TYPE_ORDER.filter((t) => byType.has(t))
      .concat([...byType.keys()].filter((t) => !TYPE_ORDER.includes(t)))
      .map((t) => ({
        key: t,
        label: typeMeta(t).label,
        sublabel: `${byType.get(t)!.length}`,
        entries: byType.get(t)!,
      }));
  }
  if (mode === "author") {
    const byAuthor = new Map<string, MergedBoardEntry[]>();
    for (const e of entries) {
      if (!byAuthor.has(e.author)) byAuthor.set(e.author, []);
      byAuthor.get(e.author)!.push(e);
    }
    return [...byAuthor.keys()].sort().map((a) => ({
      key: a,
      label: prettyAuthor(a),
      sublabel: `${byAuthor.get(a)!.length}`,
      entries: byAuthor.get(a)!,
    }));
  }
  // round
  const byRound = new Map<number, MergedBoardEntry[]>();
  for (const e of entries) {
    if (!byRound.has(e.round)) byRound.set(e.round, []);
    byRound.get(e.round)!.push(e);
  }
  return [...byRound.keys()]
    .sort((a, b) => a - b)
    .map((r) => ({
      key: `round-${r}`,
      label: r === 0 ? "Genesis" : `Round ${r}`,
      sublabel: `${byRound.get(r)!.length}`,
      entries: byRound.get(r)!,
    }));
}

// ── Debate thread builder ─────────────────────────────────────────────

export interface ThreadNode {
  entry: MergedBoardEntry;
  children: ThreadNode[];
}

/**
 * Build debate threads from entry refs. An entry that references another
 * (critique → finding, rebuttal → critique, solution → finding, …) becomes
 * a child of that entry. Roots are entries not referenced by anything.
 */
export function buildThreads(entries: MergedBoardEntry[]): ThreadNode[] {
  const byId = new Map(entries.map((e) => [e.id, e]));
  const children = new Map<string, MergedBoardEntry[]>();
  const childIds = new Set<string>();

  for (const e of entries) {
    for (const ref of e.refs) {
      if (!byId.has(ref)) continue;
      if (!children.has(ref)) children.set(ref, []);
      children.get(ref)!.push(e);
      childIds.add(e.id);
    }
  }

  const seen = new Set<string>();
  function build(entry: MergedBoardEntry): ThreadNode {
    seen.add(entry.id);
    const kids = (children.get(entry.id) ?? [])
      .filter((c) => !seen.has(c.id))
      .sort((a, b) => a.seq - b.seq)
      .map(build);
    return { entry, children: kids };
  }

  return entries
    .filter((e) => !childIds.has(e.id))
    .sort((a, b) => a.seq - b.seq)
    .map(build);
}

/** Count total entries in a thread subtree. */
export function threadSize(node: ThreadNode): number {
  return 1 + node.children.reduce((n, c) => n + threadSize(c), 0);
}

// ── Misc ──────────────────────────────────────────────────────────────

export function prettyAuthor(author: string): string {
  if (author.startsWith("expert.")) {
    return author
      .slice(7)
      .split("_")
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(" ");
  }
  return author
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/**
 * Unescape literal two-character escape sequences (\\n → newline, \\t → tab,
 * \\" → quote) that the daemon sometimes stores in board entry bodies.
 */
function unescapeLiteralSequences(s: string): string {
  return s
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t")
    .replace(/\\"/g, '"');
}

/**
 * Normalize a board entry body that may be JSON-encoded, markdown-fenced JSON,
 * or already plain text / markdown. Returns human-readable text suitable for
 * both card previews and full-body rendering.
 */
export function normalizeBody(raw: string): string {
  if (!raw) return "";
  const trimmed = raw.trim();

  // 1a. Markdown-fenced JSON: ```json\n{...}\n```
  const fenceMatch = trimmed.match(/^```(?:json)?\s*\n([\s\S]+?)\n```\s*$/);
  if (fenceMatch) {
    const extracted = extractReadableFromJson(fenceMatch[1]);
    if (extracted) return extracted;
  }

  // 1b. Fence with literal escapes: ```json\\n[...\\n]\\n```
  const litFenceMatch = trimmed.match(/^```(?:json)?\\n([\s\S]+?)\\n```\s*$/);
  if (litFenceMatch) {
    const unescaped = unescapeLiteralSequences(litFenceMatch[1]);
    const extracted = extractReadableFromJson(unescaped);
    if (extracted) return extracted;
  }

  // 2. Bare JSON object or array
  if ((trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
    const extracted = extractReadableFromJson(trimmed);
    if (extracted) return extracted;
  }

  // 3. Already plain text / markdown — return as-is
  return trimmed;
}

/** Try to parse a JSON string and extract human-readable content from it. */
function extractReadableFromJson(jsonStr: string): string | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonStr);
  } catch {
    // Retry with unescaped literal sequences (daemon sometimes double-escapes)
    try {
      parsed = JSON.parse(unescapeLiteralSequences(jsonStr));
    } catch {
      return null;
    }
  }

  if (Array.isArray(parsed)) {
    // Array of board entries — extract body from the most useful one (solution > last)
    const solution = parsed.find(
      (e) => typeof e === "object" && e !== null && (e as Record<string, unknown>).type === "solution"
    ) ?? parsed[parsed.length - 1];
    return extractBodyFromObject(solution);
  }

  return extractBodyFromObject(parsed);
}

/** Extract the body/content text from a parsed JSON object. */
function extractBodyFromObject(obj: unknown): string | null {
  if (typeof obj === "string") return obj;
  if (typeof obj !== "object" || obj === null) return null;
  const rec = obj as Record<string, unknown>;

  // Common body field names used by the daemon
  for (const key of ["body", "content", "text", "message", "description", "summary"]) {
    if (typeof rec[key] === "string" && (rec[key] as string).trim()) {
      let body = (rec[key] as string).trim();
      // Unescape any remaining literal sequences in the body text
      if (body.includes("\\n") || body.includes("\\t")) {
        body = unescapeLiteralSequences(body);
      }
      // If there's also a title, prepend it
      if (typeof rec.title === "string" && rec.title.trim()) {
        return `**${rec.title.trim()}**\n\n${body}`;
      }
      return body;
    }
  }

  // Fallback: pretty-print the object as key-value pairs
  const entries = Object.entries(rec).filter(
    ([, v]) => v !== null && v !== undefined && v !== "",
  );
  if (entries.length === 0) return null;
  return entries
    .map(([k, v]) => {
      const label = k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
      const val = typeof v === "string" ? v : JSON.stringify(v);
      return `**${label}:** ${val}`;
    })
    .join("\n");
}

/** Strip the structured-markup noise some agents emit so previews read well. */
export function bodyPreview(body: string, max = 240): string {
  const normalized = normalizeBody(body);
  const cleaned = normalized
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\n{2,}/g, "\n")
    .trim();
  return cleaned.length > max ? cleaned.slice(0, max).trimEnd() + "…" : cleaned;
}
