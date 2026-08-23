"use client";

/**
 * AgentDetailClient — /agents/[role]
 *
 * Tabs: Overview (health and profile), Skills, Toolsets.
 * The active tab lives in the `tab` query parameter.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { ArrowLeft, Bot, RefreshCw, Search, ShieldCheck } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { Skeleton } from "@/components/ui/Skeleton";
import { authorColor } from "@/lib/design-tokens";
import { diagnosticsText, failureFromReason, type RequestFailure } from "@/lib/request-state";
import {
  agentEngine,
  agentStatus,
  capacityLabel,
  loadAgentNodes,
  loadAgentSkills,
  loadAgentToolsets,
  roleTitle,
  type AgentCollection,
  type AgentNode,
  type AgentSkill,
  type AgentToolset,
} from "@/lib/agents";

type Tab = "overview" | "skills" | "toolsets";
const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "skills", label: "Skills" },
  { id: "toolsets", label: "Toolsets" },
];

function readTab(value: string | null): Tab {
  return value === "skills" || value === "toolsets" ? value : "overview";
}

export function AgentDetailClient({ role }: { role: string }) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const tab = readTab(searchParams.get("tab"));
  const [node, setNode] = useState<AgentNode | null>(null);
  const [missing, setMissing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState<RequestFailure | null>(null);
  const [skills, setSkills] = useState<AgentCollection<AgentSkill> | null>(null);
  const [toolsets, setToolsets] = useState<AgentCollection<AgentToolset> | null>(null);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setFailure(null);
    setMissing(false);
    try {
      const nodes = await loadAgentNodes();
      const found = nodes.find((candidate) => candidate.role === role) ?? null;
      setNode(found);
      setLoading(false);
      if (!found) {
        setMissing(true);
        return;
      }
      if (!found.reachable) {
        const unavailable: RequestFailure = {
          kind: "unavailable",
          message: `${found.name} is unreachable`,
          detail: `No response came from ${found.host}.`,
        };
        setSkills({ items: [], failure: unavailable });
        setToolsets({ items: [], failure: unavailable });
        return;
      }
      const [nextSkills, nextToolsets] = await Promise.all([loadAgentSkills(role), loadAgentToolsets(role)]);
      setSkills(nextSkills);
      setToolsets(nextToolsets);
    } catch (reason) {
      setFailure(failureFromReason(reason, "Agent discovery failed"));
      setLoading(false);
    }
  }, [role]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  const selectTab = (next: Tab) => {
    const params = new URLSearchParams(searchParams.toString());
    if (next === "overview") params.delete("tab");
    else params.set("tab", next);
    window.history.replaceState(null, "", `${pathname}${params.size ? `?${params.toString()}` : ""}`);
    setQuery("");
  };

  const normalized = query.trim().toLowerCase();
  const visibleSkills = useMemo(() => (skills?.items ?? []).filter((skill) => !normalized
    || [skill.name, skill.description, skill.category].some((value) => typeof value === "string" && value.toLowerCase().includes(normalized))), [normalized, skills]);
  const visibleToolsets = useMemo(() => (toolsets?.items ?? []).filter((toolset) => !normalized
    || [toolset.name, toolset.label, toolset.description, ...(toolset.tools ?? [])].some((value) => typeof value === "string" && value.toLowerCase().includes(normalized))), [normalized, toolsets]);

  const status = node ? agentStatus(node) : null;
  const engine = node ? agentEngine(node) : null;
  const color = authorColor(role);

  return (
    <div className="agents-page agent-detail">
      <Link href="/agents" className="task-header__back">
        <ArrowLeft size={16} />
        <span>Agents</span>
      </Link>

      {failure ? (
        <ResourceState
          kind={failure.kind}
          title="Agent discovery failed"
          description="Mission Control cannot load this agent node."
          detail={failure.detail}
          diagnostics={diagnosticsText("Agent discovery", failure, { role })}
          onRetry={load}
          operationsHref="/infra"
        />
      ) : loading && !node ? (
        <div className="agent-detail__header"><Skeleton variant="list" lines={2} /></div>
      ) : missing || !node ? (
        <ResourceState
          kind="empty"
          title="Unknown agent"
          description={`No configured node has the role “${role}”.`}
          onRetry={load}
        />
      ) : (
        <>
          <header className="agent-detail__header">
            <span className="agent-card__avatar agent-card__avatar--lg" style={{ background: color }} aria-hidden="true">
              <Bot size={24} />
            </span>
            <div className="agent-detail__identity">
              <h2>{node.name}</h2>
              <p>
                {roleTitle(node.role)} · <code>{node.host}</code>
                {engine ? <> · {engine.label}{engine.detail ? ` (${engine.detail})` : ""}</> : null}
              </p>
            </div>
            <div className="agent-detail__actions">
              {status ? (
                <span className={`agent-status agent-status--${status.tone}`}>
                  <span className="agent-status__dot" aria-hidden="true" />
                  {status.label}
                </span>
              ) : null}
              <ActionButton variant="secondary" onClick={() => void load()} loading={loading}>
                <RefreshCw size={14} /> Refresh
              </ActionButton>
            </div>
          </header>

          <nav className="task-tabs agent-detail__tabs" role="tablist" aria-label="Agent detail views">
            {TABS.map((item) => {
              const count = item.id === "skills" ? skills?.items.length : item.id === "toolsets" ? toolsets?.items.length : undefined;
              return (
                <button
                  key={item.id}
                  type="button"
                  role="tab"
                  aria-selected={tab === item.id}
                  className={`task-tabs__tab ${tab === item.id ? "task-tabs__tab--active" : ""}`}
                  onClick={() => selectTab(item.id)}
                >
                  {item.label}
                  {typeof count === "number" ? <span className="agent-detail__tab-count">{count}</span> : null}
                </button>
              );
            })}
          </nav>

          {tab === "overview" ? (
            <section className="agent-detail__panel" role="tabpanel">
              <dl className="agent-facts">
                <div><dt>Health</dt><dd data-state={node.health?.status ?? "unavailable"}>{status?.label}</dd></div>
                <div><dt>Capacity</dt><dd>{capacityLabel(node)}</dd></div>
                <div><dt>Model</dt><dd><code>{node.health?.model ?? node.profiles[0]?.model ?? "Not reported"}</code></dd></div>
                <div><dt>Engine</dt><dd>{engine?.label}{engine?.detail ? <small> · {engine.detail}</small> : null}</dd></div>
                <div><dt>Execution API</dt><dd data-state={node.health?.runs_api_ready ? "ready" : "unavailable"}>{node.health?.runs_api_ready ? "Ready" : "Unavailable"}</dd></div>
                <div>
                  <dt>Current task</dt>
                  <dd>
                    {node.health?.current_task
                      ? <Link href={`/task/${encodeURIComponent(node.health.current_task)}`}>{node.health.current_task}</Link>
                      : node.health?.current_task_reported ? "Idle" : "Not reported"}
                  </dd>
                </div>
                <div><dt>Hermes status</dt><dd>{node.health?.hermes_status ?? "Not reported"}</dd></div>
                <div><dt>Active profile</dt><dd><code>{node.profiles[0]?.name ?? node.role}</code></dd></div>
              </dl>
              <p className="agent-detail__note">
                <ShieldCheck size={14} aria-hidden="true" />
                Mission Control reads this node&apos;s profile, skills, and toolsets. Change them on the agent node.
              </p>
            </section>
          ) : null}

          {tab === "skills" || tab === "toolsets" ? (
            <section className="agent-detail__panel" role="tabpanel">
              <label className="tasks-search agent-detail__search">
                <Search size={15} aria-hidden="true" />
                <span className="sr-only">Search {tab}</span>
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={tab === "skills" ? "Search skills" : "Search toolsets or tools"}
                />
              </label>

              {tab === "skills" ? (
                skills === null ? <Skeleton variant="list" lines={5} /> : skills.failure ? (
                  <ResourceState
                    kind={skills.failure.kind}
                    title={skills.failure.kind === "permission" ? "Skill access denied" : "Skills unavailable"}
                    description="This node did not return its skill catalog."
                    detail={skills.failure.detail}
                    diagnostics={diagnosticsText(`${node.name} skills`, skills.failure, { role: node.role, host: node.host })}
                    onRetry={load}
                    operationsHref="/infra"
                    compact
                  />
                ) : visibleSkills.length === 0 ? (
                  <p className="agent-detail__empty">{normalized ? "No skills match this search." : "This profile reports no skills."}</p>
                ) : (
                  <ul className="agent-list">
                    {visibleSkills.map((skill) => (
                      <li key={skill.name} className="agent-list__row">
                        <div className="agent-list__text">
                          <strong>{skill.name}</strong>
                          {skill.description ? <span>{skill.description}</span> : null}
                        </div>
                        <span className={`capability-badge ${skill.enabled === false ? "" : "capability-badge--enabled"}`}>
                          {skill.enabled === false ? "disabled" : skill.category ?? "available"}
                        </span>
                      </li>
                    ))}
                  </ul>
                )
              ) : (
                toolsets === null ? <Skeleton variant="list" lines={5} /> : toolsets.failure ? (
                  <ResourceState
                    kind={toolsets.failure.kind}
                    title={toolsets.failure.kind === "permission" ? "Toolset access denied" : "Toolsets unavailable"}
                    description="This node did not return its toolset catalog."
                    detail={toolsets.failure.detail}
                    diagnostics={diagnosticsText(`${node.name} toolsets`, toolsets.failure, { role: node.role, host: node.host })}
                    onRetry={load}
                    operationsHref="/infra"
                    compact
                  />
                ) : visibleToolsets.length === 0 ? (
                  <p className="agent-detail__empty">{normalized ? "No toolsets match this search." : "This profile reports no toolsets."}</p>
                ) : (
                  <ul className="agent-list agent-list--cards">
                    {visibleToolsets.map((toolset) => (
                      <li key={toolset.name} className="agent-toolset">
                        <div className="agent-toolset__header">
                          <div className="agent-list__text">
                            <strong>{toolset.label || toolset.name}</strong>
                            {toolset.description ? <span>{toolset.description}</span> : null}
                          </div>
                          <div className="agent-toolset__badges">
                            <span className={`capability-badge ${toolset.configured ? "capability-badge--configured" : ""}`}>
                              {toolset.configured ? "configured" : "needs config"}
                            </span>
                            <span className={`capability-badge ${toolset.enabled ? "capability-badge--enabled" : ""}`}>
                              {toolset.enabled ? "enabled" : "off"}
                            </span>
                          </div>
                        </div>
                        {toolset.tools?.length ? (
                          <div className="agent-toolset__tools">
                            {toolset.tools.map((tool) => <code key={tool}>{tool}</code>)}
                          </div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )
              )}
            </section>
          ) : null}
        </>
      )}
    </div>
  );
}
