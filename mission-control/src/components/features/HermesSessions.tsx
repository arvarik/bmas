"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  Bot,
  ChevronRight,
  ExternalLink,
  GitFork,
  MessagesSquare,
  RefreshCw,
  Search,
} from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { Panel } from "@/components/ui/Panel";
import { ResourceState } from "@/components/ui/ResourceState";
import { RichContent } from "@/components/ui/RichContent";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/hooks/useToast";
import {
  diagnosticsText,
  failureFromReason,
  failureFromResponse,
  type RequestFailure,
} from "@/lib/request-state";
import {
  filterHermesMessages,
  filterHermesSessions,
  sessionLineage,
  taskIdForSession,
  type HermesMessageRecord,
  type HermesSessionRecord,
} from "@/lib/hermes-session-presentation";
import styles from "./HermesSessions.module.css";
import { Select } from "@/components/ui/Select";

interface NodeData {
  role: string;
  name: string;
  host: string;
  profiles: { name: string }[];
  reachable: boolean;
}

interface HermesSession extends HermesSessionRecord {
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

interface HermesMessage extends HermesMessageRecord {
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
  const [configuredNodes, setConfiguredNodes] = useState<NodeData[]>([]);
  const [nodes, setNodes] = useState<NodeData[]>([]);
  const [selectedNode, setSelectedNode] = useState("");
  const [sessions, setSessions] = useState<HermesSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<HermesMessage[]>([]);
  const [selectedSession, setSelectedSession] = useState<HermesSession | null>(null);
  const [forkTitle, setForkTitle] = useState("");
  const [sessionQuery, setSessionQuery] = useState("");
  const [messageQuery, setMessageQuery] = useState("");
  const [discovering, setDiscovering] = useState(true);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [forking, setForking] = useState(false);
  const [discoveryFailure, setDiscoveryFailure] = useState<RequestFailure | null>(null);
  const [sessionFailure, setSessionFailure] = useState<RequestFailure | null>(null);
  const [detailFailure, setDetailFailure] = useState<RequestFailure | null>(null);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const profileRequest = useRef(0);
  const listRequest = useRef(0);
  const { toast } = useToast();

  const loadSessions = useCallback(async (node: string) => {
    if (!node) return;
    const requestId = ++listRequest.current;
    setLoading(true);
    setSessionFailure(null);
    try {
      const response = await fetch(
        `/api/sessions?node=${encodeURIComponent(node)}&limit=100&include_children=true`,
        { cache: "no-store", signal: AbortSignal.timeout(8_000) },
      );
      if (!response.ok) throw await failureFromResponse(response, "Session request failed");
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
      setSessionFailure(failureFromReason(reason, "Session request failed"));
    } finally {
      if (requestId === listRequest.current) setLoading(false);
    }
  }, []);

  const loadNodes = useCallback(async () => {
    const requestId = ++profileRequest.current;
    setDiscovering(true);
    setDiscoveryFailure(null);
    try {
      const response = await fetch("/api/profiles", {
        cache: "no-store",
        signal: AbortSignal.timeout(8_000),
      });
      if (!response.ok) throw await failureFromResponse(response, "Agent profile discovery failed");
      const body = await response.json() as { nodes?: NodeData[] };
      if (requestId !== profileRequest.current) return;
      const configured = body.nodes ?? [];
      const reachable = configured.filter((node) => node.reachable);
      setConfiguredNodes(configured);
      setNodes(reachable);
      setSelectedNode((current) => reachable.some((node) => node.role === current)
        ? current
        : reachable[0]?.role ?? "");
      if (reachable.length) setLoading(true);
      if (!reachable.length) {
        listRequest.current += 1;
        setSessions([]);
        setSelectedId(null);
        setSelectedSession(null);
        setMessages([]);
        setLoading(false);
      }
    } catch (reason) {
      if (requestId !== profileRequest.current) return;
      setConfiguredNodes([]);
      setNodes([]);
      setSelectedNode("");
      setDiscoveryFailure(failureFromReason(reason, "Agent profile discovery failed"));
    } finally {
      if (requestId === profileRequest.current) setDiscovering(false);
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(loadNodes);
    return () => { profileRequest.current += 1; };
  }, [loadNodes]);

  useEffect(() => {
    if (selectedNode) void Promise.resolve().then(() => loadSessions(selectedNode));
  }, [loadSessions, selectedNode]);

  useEffect(() => {
    if (!selectedNode || !selectedId) return;
    const controller = new AbortController();
    void Promise.resolve().then(async () => {
      setDetailLoading(true);
      setDetailFailure(null);
      setSelectedSession(null);
      setMessages([]);
      try {
        const response = await fetch(
          `/api/sessions/${encodeURIComponent(selectedId)}?node=${encodeURIComponent(selectedNode)}`,
          { cache: "no-store", signal: controller.signal },
        );
        if (!response.ok) throw await failureFromResponse(response, "Session detail request failed");
        const body = await response.json() as { session?: HermesSession; messages?: HermesMessage[] };
        setSelectedSession(body.session ?? null);
        setMessages(body.messages ?? []);
        setForkTitle(`${body.session?.title || "Session"} fork`);
      } catch (reason) {
        if (!controller.signal.aborted) {
          const failure = failureFromReason(reason, "Session detail request failed");
          setDetailFailure(failure);
        }
      } finally {
        if (!controller.signal.aborted) setDetailLoading(false);
      }
    });
    return () => controller.abort();
  }, [detailAttempt, selectedId, selectedNode]);

  const activeProfile = useMemo(
    () => nodes.find((node) => node.role === selectedNode)?.profiles[0]?.name ?? selectedNode,
    [nodes, selectedNode],
  );
  const visibleSessions = useMemo(
    () => filterHermesSessions(sessions, sessionQuery),
    [sessionQuery, sessions],
  );
  const visibleMessages = useMemo(
    () => filterHermesMessages(messages, messageQuery),
    [messageQuery, messages],
  );
  const lineage = useMemo(
    () => sessionLineage(sessions, selectedId),
    [selectedId, sessions],
  );
  const linkedTaskId = useMemo(
    () => taskIdForSession(selectedSession, selectedNode),
    [selectedNode, selectedSession],
  );
  const selectSession = useCallback((sessionId: string) => {
    if (sessionId === selectedId) return;
    setSelectedSession(null);
    setMessages([]);
    setMessageQuery("");
    setDetailFailure(null);
    setDetailLoading(true);
    setSelectedId(sessionId);
  }, [selectedId]);

  const fork = async () => {
    if (!selectedId || !selectedNode || !forkTitle.trim()) return;
    setForking(true);
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(selectedId)}/fork`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node: selectedNode, title: forkTitle.trim() }),
        signal: AbortSignal.timeout(15_000),
      });
      if (!response.ok) throw await failureFromResponse(response, "Session fork failed");
      const body = await response.json() as { session?: HermesSession; object?: string };
      const forked = body.session
        ?? (typeof body === "object" && body !== null
          ? (body as { session?: HermesSession }).session
          : undefined);
      toast({ type: "success", message: `Forked ${selectedId}.` });
      await loadSessions(selectedNode);
      if (forked?.id) setSelectedId(forked.id);
    } catch (reason) {
      toast({ type: "error", message: failureFromReason(reason, "Session fork failed").message });
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
          onClick={() => void (selectedNode ? loadSessions(selectedNode) : loadNodes())}
          loading={discovering || loading}
        >
          <RefreshCw size={14} /> Refresh
        </ActionButton>
      )}
    >
      <div className="hermes-sessions">
        {discovering && nodes.length === 0 ? <Skeleton variant="list" lines={6} /> : null}

        {!discovering && discoveryFailure ? (
          <ResourceState
            kind={discoveryFailure.kind}
            title={discoveryFailure.kind === "permission" ? "Agent profile access denied" : "Agent profiles unavailable"}
            description="Mission Control cannot discover the profiles that provide Hermes sessions."
            detail={discoveryFailure.detail}
            diagnostics={diagnosticsText("Hermes session profile discovery", discoveryFailure)}
            onRetry={loadNodes}
            operationsHref="/infra"
          />
        ) : null}

        {!discovering && !discoveryFailure && nodes.length === 0 ? (
          <ResourceState
            kind="unavailable"
            title={configuredNodes.length ? "No agent profiles are reachable" : "No agent profiles are configured"}
            description={configuredNodes.length
              ? "Hermes sessions remain unavailable until at least one configured agent responds."
              : "Add an agent node before you browse or fork Hermes sessions."}
            detail={configuredNodes.length
              ? configuredNodes.map((node) => `${node.name}: ${node.host}`).join("\n")
              : "The profiles endpoint returned an empty node list."}
            diagnostics={JSON.stringify({
              component: "Hermes sessions",
              state: configuredNodes.length ? "all_profiles_unreachable" : "no_profiles_configured",
              nodes: configuredNodes.map(({ role, name, host, reachable }) => ({ role, name, host, reachable })),
              captured_at: new Date().toISOString(),
            }, null, 2)}
            onRetry={loadNodes}
            operationsHref="/infra"
          />
        ) : null}

        {nodes.length > 0 ? (
          <>
            <div className="hermes-sessions__toolbar">
              <label>
                <span>Agent profile</span>
                <Select
                  value={selectedNode}
                  onChange={(event) => {
                    listRequest.current += 1;
                    setSelectedId(null);
                    setSelectedSession(null);
                    setSessions([]);
                    setMessages([]);
                    setDetailFailure(null);
                    setSessionFailure(null);
                    setSelectedNode(event.target.value);
                  }}
                >
                  {nodes.map((node) => (
                    <option key={node.role} value={node.role}>
                      {node.name} · {node.profiles[0]?.name ?? node.role}
                    </option>
                  ))}
                </Select>
              </label>
              <label className={styles.searchField}>
                <span>Search sessions</span>
                <span className={styles.searchControl}>
                  <Search size={14} aria-hidden="true" />
                  <input
                    type="search"
                    value={sessionQuery}
                    onChange={(event) => setSessionQuery(event.target.value)}
                    placeholder="Title, ID, model, or preview"
                  />
                </span>
              </label>
              <span className="capability-badge capability-badge--enabled">{activeProfile}</span>
            </div>

            <p className="sr-only" aria-live="polite">
              {visibleSessions.length} of {sessions.length} sessions shown.
            </p>

            {loading && sessions.length === 0 ? <Skeleton variant="list" lines={6} /> : null}

            {!loading && sessionFailure ? (
              <ResourceState
                kind={sessionFailure.kind}
                title={sessionFailure.kind === "permission" ? "Session access denied" : "Sessions unavailable"}
                description={`Mission Control cannot load sessions from ${activeProfile}.`}
                detail={sessionFailure.detail}
                diagnostics={diagnosticsText("Hermes session list", sessionFailure, {
                  node: selectedNode,
                  profile: activeProfile,
                })}
                onRetry={() => loadSessions(selectedNode)}
                operationsHref="/infra"
              />
            ) : null}

            {!loading && !sessionFailure && sessions.length === 0 ? (
              <ResourceState
                kind="empty"
                title="No sessions for this profile"
                description={`${activeProfile} is reachable, but it has no persisted sessions.`}
                onRetry={() => loadSessions(selectedNode)}
              />
            ) : null}

            {sessions.length > 0 ? (
              <div className="hermes-sessions__layout">
                <ul className={`hermes-sessions__list ${styles.sessionList}`} aria-label="Hermes session list">
                  {visibleSessions.length === 0 ? (
                    <li className={styles.noMatches}>No sessions match this search.</li>
                  ) : visibleSessions.map((session) => (
                    <li key={session.id}>
                      <button
                        type="button"
                        className={`hermes-session-row ${selectedId === session.id ? "hermes-session-row--active" : ""}`}
                        onClick={() => selectSession(session.id)}
                        aria-current={selectedId === session.id ? "true" : undefined}
                      >
                        <Bot size={14} aria-hidden="true" />
                        <span className="hermes-session-row__content">
                          <strong>{session.title || session.id}</strong>
                          <span>{session.preview || `${session.message_count ?? 0} messages`}</span>
                          <small>{dateText(session.last_active ?? session.started_at)}</small>
                          {session.parent_session_id ? <small>Forked session</small> : null}
                        </span>
                        {session.parent_session_id
                          ? <GitFork size={12} aria-hidden="true" />
                          : <ChevronRight size={13} aria-hidden="true" />}
                      </button>
                    </li>
                  ))}
                </ul>

                <div className="hermes-session-detail">
                  {detailLoading ? <Skeleton variant="list" lines={6} /> : detailFailure ? (
                    <ResourceState
                      kind={detailFailure.kind}
                      title={detailFailure.kind === "permission" ? "Transcript access denied" : "Transcript unavailable"}
                      description="Mission Control cannot load this session transcript."
                      detail={detailFailure.detail}
                      diagnostics={diagnosticsText("Hermes session transcript", detailFailure, {
                        node: selectedNode,
                        session_id: selectedId,
                      })}
                      onRetry={() => setDetailAttempt((current) => current + 1)}
                      operationsHref="/infra"
                      compact
                    />
                  ) : selectedSession ? (
                    <>
                      <div className="hermes-session-detail__header">
                        <div>
                          <h3 id="hermes-session-title">{selectedSession.title || selectedSession.id}</h3>
                          <span>{selectedSession.model || activeProfile} · {messages.length} messages</span>
                        </div>
                        <div className="hermes-session-detail__metrics" aria-label="Session usage">
                          <span>{((selectedSession.input_tokens ?? 0) + (selectedSession.output_tokens ?? 0)).toLocaleString()} tokens</span>
                          <span>${(selectedSession.actual_cost_usd ?? selectedSession.estimated_cost_usd ?? 0).toFixed(4)} cost</span>
                        </div>
                      </div>

                      <div className={styles.detailContext}>
                        <nav className={styles.lineage} aria-label="Session lineage">
                          <span className={styles.contextLabel}>Lineage</span>
                          <ol>
                            {lineage.ancestors.map((session) => (
                              <li key={session.id}>
                                <button type="button" onClick={() => selectSession(session.id)}>
                                  {session.title || session.id}
                                </button>
                              </li>
                            ))}
                            <li aria-current="page">{selectedSession.title || selectedSession.id}</li>
                          </ol>
                          {selectedSession.parent_session_id && lineage.ancestors.length === 0 ? (
                            <span className={styles.missingParent}>
                              Parent {selectedSession.parent_session_id} is outside the loaded result set.
                            </span>
                          ) : null}
                          {lineage.children.length > 0 ? (
                            <div className={styles.children}>
                              <span>Direct forks</span>
                              {lineage.children.map((session) => (
                                <button key={session.id} type="button" onClick={() => selectSession(session.id)}>
                                  <GitFork size={12} aria-hidden="true" /> {session.title || session.id}
                                </button>
                              ))}
                            </div>
                          ) : null}
                        </nav>
                        {linkedTaskId ? (
                          <Link className={styles.taskLink} href={`/task/${encodeURIComponent(linkedTaskId)}`}>
                            Open task {linkedTaskId} <ExternalLink size={13} aria-hidden="true" />
                          </Link>
                        ) : (
                          <span className={styles.noTask}>No linked task was reported.</span>
                        )}
                      </div>

                      <div className={styles.messageToolbar}>
                        <label>
                          <span className="sr-only">Search this transcript</span>
                          <Search size={14} aria-hidden="true" />
                          <input
                            type="search"
                            value={messageQuery}
                            onChange={(event) => setMessageQuery(event.target.value)}
                            placeholder="Search this transcript"
                          />
                        </label>
                        <span aria-live="polite">
                          {visibleMessages.length} of {messages.length} messages
                        </span>
                      </div>

                      <div className="hermes-session-detail__messages" aria-labelledby="hermes-session-title">
                        {messages.length === 0 ? (
                          <div className="hermes-sessions__empty">This session has no messages.</div>
                        ) : visibleMessages.length === 0 ? (
                          <div className="hermes-sessions__empty">No messages match this search.</div>
                        ) : visibleMessages.map((message, index) => (
                          <article
                            key={message.id ?? index}
                            className={`hermes-message hermes-message--${message.role || "unknown"}`}
                            aria-label={`${message.role || "Unknown"} message`}
                          >
                            <div className="hermes-message__meta">
                              <strong>{message.role || "unknown"}</strong>
                              {message.tool_name ? (
                                <span className={styles.toolBadge}>
                                  <MessagesSquare size={11} aria-hidden="true" /> {message.tool_name}
                                </span>
                              ) : null}
                              <span>{dateText(message.timestamp)}</span>
                            </div>
                            <RichContent
                              content={contentText(message.content)}
                              forceMarkdown={typeof message.content === "string"}
                              className={styles.messageContent}
                            />
                          </article>
                        ))}
                      </div>

                      <div className="hermes-session-detail__fork">
                        <GitFork size={15} aria-hidden="true" />
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
            ) : null}
          </>
        ) : null}
      </div>
    </Panel>
  );
}

export default HermesSessions;
