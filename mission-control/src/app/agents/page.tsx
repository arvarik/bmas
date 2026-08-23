"use client";

/**
 * Agents — /agents
 *
 * One card per agent node: name, role, status, engine, model, and the
 * current task. Open a card to read skills and toolsets on the detail page.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, Bot, RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { Skeleton } from "@/components/ui/Skeleton";
import { authorColor } from "@/lib/design-tokens";
import { diagnosticsText, failureFromReason, type RequestFailure } from "@/lib/request-state";
import {
  agentEngine,
  agentStatus,
  loadAgentNodes,
  loadAgentSkills,
  loadAgentToolsets,
  roleTitle,
  type AgentNode,
} from "@/lib/agents";

interface CapabilityCounts {
  skills: number | null;
  toolsets: number | null;
}

export default function AgentsPage() {
  const [nodes, setNodes] = useState<AgentNode[]>([]);
  const [counts, setCounts] = useState<Record<string, CapabilityCounts>>({});
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState<RequestFailure | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setFailure(null);
    try {
      const next = await loadAgentNodes();
      setNodes(next);
      setLoading(false);
      const entries = await Promise.all(next.map(async (node) => {
        if (!node.reachable) return [node.role, { skills: null, toolsets: null }] as const;
        const [skills, toolsets] = await Promise.all([loadAgentSkills(node.role), loadAgentToolsets(node.role)]);
        return [node.role, {
          skills: skills.failure ? null : skills.items.length,
          toolsets: toolsets.failure ? null : toolsets.items.length,
        }] as const;
      }));
      setCounts(Object.fromEntries(entries));
    } catch (reason) {
      setNodes([]);
      setFailure(failureFromReason(reason, "Agent discovery failed"));
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  const online = nodes.filter((node) => node.reachable && node.health?.ready).length;

  return (
    <div className="agents-page">
      <header className="page-header">
        <div>
          <p className="page-eyebrow">Observe</p>
          <h2>Agents</h2>
          <p>{nodes.length ? `${online} of ${nodes.length} execution nodes ready.` : "Execution nodes that run role activations."}</p>
        </div>
        <ActionButton variant="secondary" onClick={() => void load()} loading={loading}>
          <RefreshCw size={14} /> Refresh
        </ActionButton>
      </header>

      {failure ? (
        <ResourceState
          kind={failure.kind}
          title={failure.kind === "permission" ? "Agent discovery access denied" : "Agent discovery failed"}
          description="Mission Control cannot load the agent nodes."
          detail={failure.detail}
          diagnostics={diagnosticsText("Agent discovery", failure)}
          onRetry={load}
          operationsHref="/infra"
        />
      ) : loading ? (
        <div className="agents-grid" aria-busy="true">
          {[0, 1, 2].map((index) => <div key={index} className="agent-card agent-card--placeholder"><Skeleton variant="list" lines={3} /></div>)}
        </div>
      ) : nodes.length === 0 ? (
        <ResourceState
          kind="unavailable"
          title="No agent nodes are configured"
          description="Add at least one agent node in bmas.yaml before you inspect agents."
          detail="The profiles endpoint returned an empty node list."
          onRetry={load}
          operationsHref="/infra"
        />
      ) : (
        <ul className="agents-grid">
          {nodes.map((node) => {
            const status = agentStatus(node);
            const engine = agentEngine(node);
            const color = authorColor(node.role);
            const count = counts[node.role];
            const model = node.health?.model ?? node.profiles[0]?.model ?? null;
            return (
              <li key={node.role}>
                <Link href={`/agents/${encodeURIComponent(node.role)}`} className="agent-card">
                  <div className="agent-card__top">
                    <span className="agent-card__avatar" style={{ background: color }} aria-hidden="true">
                      <Bot size={18} />
                    </span>
                    <div className="agent-card__identity">
                      <strong>{node.name}</strong>
                      <span>{roleTitle(node.role)} · <code>{node.host}</code></span>
                    </div>
                    <span className={`agent-status agent-status--${status.tone}`}>
                      <span className="agent-status__dot" aria-hidden="true" />
                      {status.label}
                    </span>
                  </div>
                  <dl className="agent-card__facts">
                    <div>
                      <dt>Engine</dt>
                      <dd>{engine.label}{engine.detail ? <small> · {engine.detail}</small> : null}</dd>
                    </div>
                    <div>
                      <dt>Model</dt>
                      <dd>{model ? <code>{model}</code> : "Not reported"}</dd>
                    </div>
                    <div>
                      <dt>Current task</dt>
                      <dd>
                        {node.health?.current_task
                          ? <code>{node.health.current_task}</code>
                          : node.health?.current_task_reported ? "Idle" : "Not reported"}
                      </dd>
                    </div>
                  </dl>
                  <div className="agent-card__footer">
                    <span>
                      {!node.reachable
                        ? "Capabilities unavailable"
                        : count
                          ? `${count.skills ?? "—"} skills · ${count.toolsets ?? "—"} toolsets`
                          : "Counting capabilities…"}
                    </span>
                    <span className="agent-card__open">Open <ArrowRight size={14} aria-hidden="true" /></span>
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
