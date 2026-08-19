"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  ChevronRight,
  GitFork,
  MessagesSquare,
  RefreshCw,
} from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/hooks/useToast";

interface NodeData {
  role: string;
  name: string;
  host: string;
  profiles: { name: string }[];
  reachable: boolean;
}

interface HermesSession {
  id: string;
  title?: string | null;
  source?: string;
  model?: string | null;
  preview?: string | null;
  message_count?: number;
  input_tokens?: number;
  output_tokens?: number;
  estimated_cost_usd?: number;
  actual_cost_usd?: number;
  parent_session_id?: string | null;
  last_active?: number | string;
  started_at?: number | string;
  ended_at?: number | string | null;
}

interface HermesMessage {
  id?: string | number;
  role?: string;
  content?: unknown;
  timestamp?: number | string;
  token_count?: number;
  tool_name?: string;
}

function asSessionList(value: unknown): HermesSession[] {
  if (typeof value !== "object" || value === null) return [];
  const data = (value as Record<string, unknown>).data;
  return Array.isArray(data) ? data as HermesSession[] : [];
}

async function errorMessage(response: Response): Promise<string> {
  const body = await response.json().catch(() => ({})) as { error?: string };
  return body.error ?? `HTTP ${response.status}`;
}

function dateText(value: number | string | null | undefined): string {
  if (value == null || value === "") return "Unknown time";
  const date = typeof value === "number"
    ? new Date(value < 10_000_000_000 ? value * 1000 : value)
    : new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString();
}

function contentText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function HermesSessions() {
  const [nodes, setNodes] = useState<NodeData[]>([]);
  const [selectedNode, setSelectedNode] = useState("");
  const [sessions, setSessions] = useState<HermesSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<HermesMessage[]>([]);
  const [selectedSession, setSelectedSession] = useState<HermesSession | null>(null);
  const [forkTitle, setForkTitle] = useState("");
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [forking, setForking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRequest = useRef(0);
  const { toast } = useToast();

  const loadSessions = useCallback(async (node: string) => {
    if (!node) return;
    const requestId = ++listRequest.current;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/sessions?node=${encodeURIComponent(node)}&limit=100&include_children=true`,
        { cache: "no-store" },
      );
      if (!response.ok) throw new Error(await errorMessage(response));
      const body = await response.json();
      if (requestId !== listRequest.current) return;
      const next = asSessionList(body);
      setSessions(next);
      setSelectedId((current) => current && next.some((item) => item.id === current)
        ? current
        : next[0]?.id ?? null);
    } catch (reason) {
      if (requestId !== listRequest.current) return;
      setSessions([]);
      setSelectedId(null);
      setError(reason instanceof Error ? reason.message : "Session request failed");
    } finally {
      if (requestId === listRequest.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/profiles", { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(await errorMessage(response));
        return response.json() as Promise<{ nodes?: NodeData[] }>;
      })
      .then((body) => {
        const nextNodes = (body.nodes ?? []).filter((node) => node.reachable);
        setNodes(nextNodes);
        setSelectedNode((current) => current || nextNodes[0]?.role || "");
        if (!nextNodes.length) setLoading(false);
      })
      .catch((reason) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "Node discovery failed");
        setLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (selectedNode) void Promise.resolve().then(() => loadSessions(selectedNode));
  }, [loadSessions, selectedNode]);

  useEffect(() => {
    if (!selectedNode || !selectedId) return;
    const controller = new AbortController();
    void Promise.resolve().then(async () => {
      setDetailLoading(true);
      setSelectedSession(null);
      setMessages([]);
      try {
        const response = await fetch(
          `/api/sessions/${encodeURIComponent(selectedId)}?node=${encodeURIComponent(selectedNode)}`,
          { cache: "no-store", signal: controller.signal },
        );
        if (!response.ok) throw new Error(await errorMessage(response));
        const body = await response.json() as { session?: HermesSession; messages?: HermesMessage[] };
        setSelectedSession(body.session ?? null);
        setMessages(body.messages ?? []);
        setForkTitle(`${body.session?.title || "Session"} fork`);
      } catch (reason) {
        if (!controller.signal.aborted) {
          toast({ type: "error", message: reason instanceof Error ? reason.message : "Session detail failed" });
        }
      } finally {
        if (!controller.signal.aborted) setDetailLoading(false);
      }
    });
    return () => controller.abort();
  }, [selectedId, selectedNode, toast]);

  const activeProfile = useMemo(
    () => nodes.find((node) => node.role === selectedNode)?.profiles[0]?.name ?? selectedNode,
    [nodes, selectedNode],
  );

  const fork = async () => {
    if (!selectedId || !selectedNode || !forkTitle.trim()) return;
    setForking(true);
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(selectedId)}/fork`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node: selectedNode, title: forkTitle.trim() }),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const body = await response.json() as { session?: HermesSession; object?: string };
      const forked = body.session
        ?? (typeof body === "object" && body !== null
          ? (body as { session?: HermesSession }).session
          : undefined);
      toast({ type: "success", message: `Forked ${selectedId}.` });
      await loadSessions(selectedNode);
      if (forked?.id) setSelectedId(forked.id);
    } catch (reason) {
      toast({ type: "error", message: reason instanceof Error ? reason.message : "Fork failed" });
    } finally {
      setForking(false);
    }
  };

  return (
    <Panel
      title="Hermes sessions"
      subtitle="Browse persisted transcripts and create a child session from any point in the lineage."
      actions={(
        <ActionButton
          variant="secondary"
          onClick={() => void loadSessions(selectedNode)}
          loading={loading}
          disabled={!selectedNode}
        >
          <RefreshCw size={14} /> Refresh
        </ActionButton>
      )}
    >
      <div className="hermes-sessions">
        <div className="hermes-sessions__toolbar">
          <label>
            <span>Agent profile</span>
            <select
              value={selectedNode}
              onChange={(event) => {
                listRequest.current += 1;
                setSelectedId(null);
                setSelectedSession(null);
                setMessages([]);
                setSelectedNode(event.target.value);
              }}
            >
              {nodes.map((node) => (
                <option key={node.role} value={node.role}>
                  {node.name} · {node.profiles[0]?.name ?? node.role}
                </option>
              ))}
            </select>
          </label>
          {selectedNode ? <span className="capability-badge capability-badge--enabled">{activeProfile}</span> : null}
        </div>

        {error ? <div className="node-card__error">{error}</div> : null}
        {!error && !loading && sessions.length === 0 ? (
          <div className="hermes-sessions__empty">
            <MessagesSquare size={28} />
            <span>No sessions exist for this profile.</span>
          </div>
        ) : null}

        <div className="hermes-sessions__layout">
          <div className="hermes-sessions__list" aria-label="Hermes session list">
            {loading ? <Skeleton variant="list" lines={6} /> : sessions.map((session) => (
              <button
                key={session.id}
                type="button"
                className={`hermes-session-row ${selectedId === session.id ? "hermes-session-row--active" : ""}`}
                onClick={() => setSelectedId(session.id)}
              >
                <Bot size={14} />
                <span className="hermes-session-row__content">
                  <strong>{session.title || session.id}</strong>
                  <span>{session.preview || `${session.message_count ?? 0} messages`}</span>
                  <small>{dateText(session.last_active ?? session.started_at)}</small>
                </span>
                {session.parent_session_id ? <GitFork size={12} aria-label="Forked session" /> : <ChevronRight size={13} />}
              </button>
            ))}
          </div>

          <div className="hermes-session-detail">
            {detailLoading ? <Skeleton variant="list" lines={6} /> : selectedSession ? (
              <>
                <div className="hermes-session-detail__header">
                  <div>
                    <h3>{selectedSession.title || selectedSession.id}</h3>
                    <span>{selectedSession.model || activeProfile} · {messages.length} messages</span>
                  </div>
                  <div className="hermes-session-detail__metrics">
                    <span>{((selectedSession.input_tokens ?? 0) + (selectedSession.output_tokens ?? 0)).toLocaleString()} tok</span>
                    <span>${(selectedSession.actual_cost_usd ?? selectedSession.estimated_cost_usd ?? 0).toFixed(4)}</span>
                  </div>
                </div>

                <div className="hermes-session-detail__messages">
                  {messages.length === 0 ? (
                    <div className="hermes-sessions__empty">This session has no messages.</div>
                  ) : messages.map((message, index) => (
                    <article key={message.id ?? index} className={`hermes-message hermes-message--${message.role || "unknown"}`}>
                      <div className="hermes-message__meta">
                        <strong>{message.role || "unknown"}</strong>
                        <span>{dateText(message.timestamp)}</span>
                      </div>
                      <pre>{contentText(message.content)}</pre>
                    </article>
                  ))}
                </div>

                <div className="hermes-session-detail__fork">
                  <GitFork size={15} />
                  <input
                    value={forkTitle}
                    onChange={(event) => setForkTitle(event.target.value)}
                    maxLength={200}
                    placeholder="Fork title"
                    aria-label="Fork title"
                  />
                  <ActionButton onClick={() => void fork()} loading={forking} disabled={!forkTitle.trim()}>
                    Fork session
                  </ActionButton>
                </div>
              </>
            ) : null}
          </div>
        </div>
      </div>
    </Panel>
  );
}

export default HermesSessions;
