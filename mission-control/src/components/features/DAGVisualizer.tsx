"use client";

import { useEffect, useMemo, useCallback, memo } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeMouseHandler,
  type NodeProps,
  BackgroundVariant,
  Handle,
  Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  type Task,
  type SubTask,
  type TaskStatus,
} from "@/hooks/useTaskStream";
import { STATUS_COLORS, AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { StatusType } from "@/lib/design-tokens";
import { GitBranch } from "lucide-react";

// ── Constants ─────────────────────────────────────────────────────────

const NODE_WIDTH = 240;
const NODE_HEIGHT = 88;
const TASK_NODE_WIDTH = 280;
const TASK_NODE_HEIGHT = 56;

/** Map TaskStatus → StatusType for our badge system */
const STATUS_MAP: Record<TaskStatus, StatusType> = {
  pending: "pending",
  running: "running",
  completed: "success",
  failed: "error",
};

/** Background colors keyed by status, used for node styling */
const NODE_BG: Record<TaskStatus, string> = {
  pending: "hsl(222, 36%, 16%)",
  running: "hsl(217, 50%, 14%)",
  completed: "hsl(142, 40%, 12%)",
  failed: "hsl(0, 40%, 14%)",
};

const NODE_BORDER: Record<TaskStatus, string> = {
  pending: STATUS_COLORS.pending,
  running: STATUS_COLORS.running,
  completed: STATUS_COLORS.success,
  failed: STATUS_COLORS.error,
};

// ── Custom Node Component ─────────────────────────────────────────────

interface BmasNodeData {
  label: string;
  status: TaskStatus;
  agent?: AgentRole;
  isTaskGroup?: boolean;
  [key: string]: unknown;
}

const BmasNode = memo(function BmasNode({ data }: NodeProps<Node<BmasNodeData>>) {
  const { label, status, agent, isTaskGroup } = data;
  const agentColor = agent ? AGENT_COLORS[agent] : undefined;
  const badgeStatus = STATUS_MAP[status];

  return (
    <div
      style={{
        background: NODE_BG[status],
        border: `2px solid ${NODE_BORDER[status]}`,
        borderRadius: isTaskGroup ? "var(--radius-lg)" : "var(--radius-md)",
        padding: "var(--space-3) var(--space-4)",
        minWidth: isTaskGroup ? TASK_NODE_WIDTH : NODE_WIDTH,
        transition: "background 300ms ease, border-color 300ms ease, box-shadow 300ms ease",
        boxShadow: status === "running"
          ? `0 0 16px ${NODE_BORDER[status]}44`
          : `0 0 8px ${NODE_BORDER[status]}11`,
        fontFamily: "var(--font-sans)",
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />

      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
        {/* Agent identity dot */}
        {agentColor && (
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "var(--radius-full)",
              background: agentColor,
              flexShrink: 0,
            }}
          />
        )}

        {/* Task label */}
        <span
          style={{
            fontSize: isTaskGroup ? "var(--text-base)" : "var(--text-sm)",
            fontWeight: isTaskGroup ? "var(--weight-semibold)" : "var(--weight-medium)",
            color: "var(--text-primary)",
            flex: 1,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {label}
        </span>

        {/* Status badge */}
        <StatusBadge status={badgeStatus} />
      </div>

      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  );
});

const nodeTypes = { bmas: BmasNode };

// ── Layout helpers ────────────────────────────────────────────────────

function layoutDag(tasks: Task[]): { nodes: Node<BmasNodeData>[]; edges: Edge[] } {
  const nodes: Node<BmasNodeData>[] = [];
  const edges: Edge[] = [];

  let taskY = 0;

  for (const task of tasks) {
    // ── Task group node ───────────────────────────────────────────
    nodes.push({
      id: task.id,
      type: "bmas",
      position: { x: 0, y: taskY },
      data: {
        label: task.label,
        status: task.status,
        isTaskGroup: true,
      },
    });

    // ── Sub-task nodes ────────────────────────────────────────────
    const depthMap = new Map<string, number>();

    const computeDepth = (sub: SubTask): number => {
      if (depthMap.has(sub.id)) return depthMap.get(sub.id)!;
      if (sub.depends_on.length === 0) {
        depthMap.set(sub.id, 0);
        return 0;
      }
      const parentDepths = sub.depends_on.map((depId) => {
        const parent = task.sub_tasks.find((s) => s.id === depId);
        return parent ? computeDepth(parent) : 0;
      });
      const depth = Math.max(...parentDepths) + 1;
      depthMap.set(sub.id, depth);
      return depth;
    };

    for (const sub of task.sub_tasks) {
      computeDepth(sub);
    }

    const columns = new Map<number, SubTask[]>();
    for (const sub of task.sub_tasks) {
      const d = depthMap.get(sub.id) ?? 0;
      if (!columns.has(d)) columns.set(d, []);
      columns.get(d)!.push(sub);
    }

    const sortedDepths = [...columns.keys()].sort((a, b) => a - b);
    const subBaseY = taskY + TASK_NODE_HEIGHT + 48;

    for (const depth of sortedDepths) {
      const col = columns.get(depth)!;
      const colX = depth * (NODE_WIDTH + 60);

      col.forEach((sub, rowIdx) => {
        nodes.push({
          id: sub.id,
          type: "bmas",
          position: { x: colX, y: subBaseY + rowIdx * (NODE_HEIGHT + 32) },
          data: {
            label: sub.label,
            status: sub.status,
            agent: sub.agent as AgentRole,
          },
        });

        // Edge from task → first-depth sub-tasks
        if (depth === 0) {
          edges.push({
            id: `e-${task.id}-${sub.id}`,
            source: task.id,
            target: sub.id,
            animated: sub.status === "running",
            style: {
              stroke: sub.status === "completed"
                ? STATUS_COLORS.success
                : NODE_BORDER[sub.status],
              strokeWidth: 2,
            },
          });
        }

        // Dependency edges
        for (const depId of sub.depends_on) {
          const depSub = task.sub_tasks.find((s) => s.id === depId);
          const edgeColor = depSub?.status === "completed"
            ? STATUS_COLORS.success
            : NODE_BORDER[depSub?.status ?? "pending"];

          edges.push({
            id: `e-${depId}-${sub.id}`,
            source: depId,
            target: sub.id,
            animated: sub.status === "running",
            style: { stroke: edgeColor, strokeWidth: 2 },
          });
        }
      });
    }

    const maxRows = Math.max(1, ...[...columns.values()].map((col) => col.length));
    taskY = subBaseY + maxRows * (NODE_HEIGHT + 32) + 60;
  }

  return { nodes, edges };
}

// ── Component ─────────────────────────────────────────────────────────

interface DAGVisualizerProps {
  tasks: Task[];
  loading?: boolean;
  error?: string | null;
}

export default function DAGVisualizer({ tasks, loading = false, error = null }: DAGVisualizerProps) {
  const { layoutNodes, layoutEdges } = useMemo(() => {
    const { nodes, edges } = layoutDag(tasks);
    return { layoutNodes: nodes, layoutEdges: edges };
  }, [tasks]);

  const [nodes, setNodes, onNodesChange] = useNodesState(layoutNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(layoutEdges);

  useEffect(() => {
    setNodes(layoutNodes);
    setEdges(layoutEdges);
  }, [layoutNodes, layoutEdges, setNodes, setEdges]);

  const onNodeClick: NodeMouseHandler = useCallback((_event, node) => {
    console.log("[DAG] clicked node:", node.id);
  }, []);

  // ── Panel status mapping ──────────────────────────────────────────
  const panelStatus = loading
    ? "loading" as const
    : error
      ? "error" as const
      : tasks.length === 0
        ? "empty" as const
        : undefined;

  return (
    <Panel
      title="Task DAG"
      subtitle="Execution graph"
      status={panelStatus}
      errorMessage={error ?? undefined}
      emptyIcon={GitBranch}
      emptyMessage="No active tasks"
      emptyHint="Submit a task to see the execution graph."
      onRetry={undefined}
    >
      <div style={{ width: "100%", height: "100%", minHeight: 200 }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          nodeTypes={nodeTypes}
          onlyRenderVisibleElements={true}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          minZoom={0.1}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={20}
            size={1}
            color="hsl(222, 36%, 16%)"
          />
          <Controls
            style={{
              background: "var(--surface-raised)",
              borderColor: "var(--border-default)",
              borderRadius: "var(--radius-md)",
            }}
          />
          <MiniMap
            nodeColor={(node) => {
              const data = node.data as BmasNodeData | undefined;
              return data ? NODE_BORDER[data.status] : STATUS_COLORS.pending;
            }}
            maskColor="hsl(222, 47%, 6%, 0.8)"
            style={{
              background: "var(--surface-raised)",
              borderColor: "var(--border-default)",
            }}
          />
        </ReactFlow>
      </div>
    </Panel>
  );
}
