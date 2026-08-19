import type { TraceEvent } from "@/hooks/useTaskStream";

export interface DelegationNode {
  id: string;
  parentId: string | null;
  depth: number;
  status: string;
  model: string | null;
  summary: string;
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
  costUsd: number;
  durationSeconds: number | null;
  toolCount: number;
  timestamp: string;
  children: DelegationNode[];
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function numberValue(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function buildDelegationTree(traces: readonly TraceEvent[]): DelegationNode[] {
  const nodes = new Map<string, DelegationNode>();
  for (const trace of traces) {
    if (trace.type !== "subagent_start" && trace.type !== "subagent_complete") continue;
    const data = trace.data ?? {};
    const id = stringValue(data.subagent_id) ?? stringValue(data.child_session_id);
    if (!id) continue;
    const previous = nodes.get(id);
    const isComplete = trace.type === "subagent_complete";
    nodes.set(id, {
      id,
      parentId: stringValue(data.parent_id) ?? previous?.parentId ?? null,
      depth: numberValue(data.depth, previous?.depth ?? 0),
      status: stringValue(data.status) ?? (isComplete ? "completed" : previous?.status ?? "running"),
      model: stringValue(data.model) ?? previous?.model ?? null,
      summary: stringValue(data.summary) ?? previous?.summary ?? "",
      inputTokens: numberValue(data.input_tokens, previous?.inputTokens ?? 0),
      outputTokens: numberValue(data.output_tokens, previous?.outputTokens ?? 0),
      reasoningTokens: numberValue(data.reasoning_tokens, previous?.reasoningTokens ?? 0),
      costUsd: numberValue(data.cost_usd, previous?.costUsd ?? 0),
      durationSeconds: data.duration_seconds == null
        ? previous?.durationSeconds ?? null
        : numberValue(data.duration_seconds),
      toolCount: numberValue(data.tool_count, previous?.toolCount ?? 0),
      timestamp: trace.timestamp || previous?.timestamp || "",
      children: [],
    });
  }

  const roots: DelegationNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.parentId ? nodes.get(node.parentId) : null;
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  const sortNodes = (items: DelegationNode[]) => {
    items.sort((left, right) => left.timestamp.localeCompare(right.timestamp));
    for (const item of items) sortNodes(item.children);
  };
  sortNodes(roots);
  return roots;
}

function durationText(value: number | null): string {
  if (value == null) return "live";
  if (value < 1) return `${Math.round(value * 1000)}ms`;
  if (value < 60) return `${value.toFixed(1)}s`;
  const minutes = Math.floor(value / 60);
  return `${minutes}m ${Math.round(value % 60)}s`;
}

function DelegationBranch({ node }: { node: DelegationNode }) {
  const tokens = node.inputTokens + node.outputTokens + node.reasoningTokens;
  const statusClass = ["completed", "success"].includes(node.status)
    ? "delegation-node--success"
    : ["failed", "timeout", "cancelled"].includes(node.status)
      ? "delegation-node--error"
      : "delegation-node--running";
  return (
    <li className="delegation-branch">
      <article className={`delegation-node ${statusClass}`}>
        <div className="delegation-node__header">
          <span className="delegation-node__status" />
          <strong>{node.id}</strong>
          <span>{node.status}</span>
        </div>
        {node.summary ? <p>{node.summary}</p> : null}
        <div className="delegation-node__metrics">
          {node.model ? <span>{node.model}</span> : null}
          <span>{tokens.toLocaleString()} tok</span>
          <span>${node.costUsd.toFixed(4)}</span>
          <span>{durationText(node.durationSeconds)}</span>
          <span>{node.toolCount} tools</span>
        </div>
      </article>
      {node.children.length ? (
        <ul>{node.children.map((child) => <DelegationBranch key={child.id} node={child} />)}</ul>
      ) : null}
    </li>
  );
}

export function DelegationTree({ traces }: { traces: readonly TraceEvent[] }) {
  const roots = buildDelegationTree(traces);
  if (!roots.length) return null;
  return (
    <section className="delegation-tree" aria-label="Hermes delegation tree">
      <div className="delegation-tree__title">
        <strong>Hermes delegations</strong>
        <span>{roots.length} root {roots.length === 1 ? "agent" : "agents"}</span>
      </div>
      <ul>{roots.map((root) => <DelegationBranch key={root.id} node={root} />)}</ul>
    </section>
  );
}
