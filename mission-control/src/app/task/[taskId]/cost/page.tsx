"use client";

/**
 * Cost Tab — /task/[taskId]/cost
 *
 * Per-task cost breakdown:
 * - Running tasks: live cost from useTaskData().cost
 * - Completed tasks: REST fallback to GET /api/tasks/{id}/cost
 * - Recharts bar chart + per-model table + MetricCards
 *
 */

import { useEffect, useState, useCallback, useMemo } from "react";
import { useParams } from "next/navigation";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell,
} from "recharts";
import { useTaskData } from "../TaskStreamContext";
import type { CostData } from "@/hooks/useTaskStream";
import { Panel } from "@/components/ui/Panel";
import { MetricCard } from "@/components/ui/MetricCard";
import { AGENT_COLORS } from "@/lib/design-tokens";
import { DollarSign } from "lucide-react";

// ── Chart colors ──────────────────────────────────────────────────────

const BAR_COLORS = [
  AGENT_COLORS.planner, AGENT_COLORS.executor, AGENT_COLORS.auditor,
  "hsl(217,91%,60%)", "hsl(38,92%,50%)", "hsl(142,71%,45%)",
  "hsl(0,84%,60%)", "hsl(220,15%,50%)",
];

interface ChartDatum { model: string; cost: number; tokens: number; }

// ── REST cost data normalization ──────────────────────────────────────

/** Daemon returns `total_cost_usd` + `by_model` as an array of objects.
 *  Frontend CostData expects `total_cost` + `by_model` as a Record. */
interface DaemonCostResponse {
  total_cost_usd: number;
  total_tokens: number;
  by_model: Array<{ model: string; cost_usd: number; input_tokens: number; output_tokens: number }>;
  by_phase?: Array<{ phase: string; cost_usd: number; tokens: number }>;
}

function mapRestCost(raw: DaemonCostResponse): CostData {
  const byModel: Record<string, { cost: number; tokens: number }> = {};
  for (const entry of raw.by_model ?? []) {
    byModel[entry.model] = {
      cost: entry.cost_usd ?? 0,
      tokens: (entry.input_tokens ?? 0) + (entry.output_tokens ?? 0),
    };
  }
  return {
    total_cost: raw.total_cost_usd ?? 0,
    total_tokens: raw.total_tokens ?? 0,
    by_model: byModel,
  };
}

// ── Custom Tooltip ────────────────────────────────────────────────────

function CostTooltip({ active, payload }: {
  active?: boolean;
  payload?: Array<{ payload: ChartDatum }>;
}) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="cost-breakdown__tooltip">
      <p className="cost-breakdown__tooltip-model">{d.model}</p>
      <p className="cost-breakdown__tooltip-cost">
        Cost: <span>${d.cost.toFixed(4)}</span>
      </p>
      <p className="cost-breakdown__tooltip-tokens">
        Tokens: <span>{d.tokens.toLocaleString()}</span>
      </p>
    </div>
  );
}

function shortenModel(name: string): string {
  return name.replace(/-\d{4,}[-\d]*/g, "").replace(/^models\//, "");
}

// ── Component ─────────────────────────────────────────────────────────

export default function CostPage() {
  const { taskId } = useParams();
  const { cost: liveCost, isLive } = useTaskData();

  // ── REST fallback for completed tasks ─────────────────────────────
  const [restCost, setRestCost] = useState<CostData | null>(null);
  const [restLoading, setRestLoading] = useState(false);

  const fetchCost = useCallback(async () => {
    if (isLive || !taskId) return;
    setRestLoading(true);
    try {
      const res = await fetch(`/api/tasks/${taskId}/cost`);
      if (res.ok) {
        const raw = (await res.json()) as DaemonCostResponse;
        setRestCost(mapRestCost(raw));
      }
    } catch {
      // Best-effort
    } finally {
      setRestLoading(false);
    }
  }, [isLive, taskId]);

  useEffect(() => {
    if (!isLive && !liveCost) {
      void fetchCost();
    }
  }, [isLive, liveCost, fetchCost]);

  const cost = liveCost ?? restCost;

  // ── Empty state ───────────────────────────────────────────────────
  if (!cost) {
    return (
      <div className="view-container">
        <Panel
          title="Cost Breakdown"
          status={isLive || restLoading ? "loading" : "empty"}
          emptyIcon={DollarSign}
          emptyMessage="No cost data"
          emptyHint={isLive ? "Cost data will appear as the task runs." : "This task has no recorded cost data."}
        />
      </div>
    );
  }

  // ── Build chart data ──────────────────────────────────────────────
  const chartData = useMemo<ChartDatum[]>(() => {
    const models = Object.entries(cost.by_model);
    return models
      .map(([model, d]) => ({
        model: shortenModel(model),
        cost: d.cost,
        tokens: d.tokens,
      }))
      .sort((a, b) => b.cost - a.cost);
  }, [cost.by_model]);

  return (
    <div className="view-container">
      <Panel title="Cost Breakdown">
        <div className="cost-breakdown">
          {/* Summary metrics */}
          <div className="cost-breakdown__metrics">
            <MetricCard label="Total Cost" value={cost.total_cost} format="currency" />
            <MetricCard label="Total Tokens" value={cost.total_tokens} format="number" />
          </div>

          {/* Bar chart */}
          {chartData.length > 0 && (
            <div className="cost-breakdown__chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData} margin={{ top: 4, right: 4, left: -12, bottom: 0 }}>
                  <CartesianGrid
                    strokeDasharray="3 3"
                    stroke="hsl(222,36%,16%)"
                    vertical={false}
                  />
                  <XAxis
                    dataKey="model"
                    tick={{ fill: "hsl(220,10%,45%)", fontSize: 10 }}
                    axisLine={{ stroke: "hsl(222,20%,22%)" }}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "hsl(220,10%,45%)", fontSize: 10 }}
                    axisLine={{ stroke: "hsl(222,20%,22%)" }}
                    tickLine={false}
                    tickFormatter={(v: number) => `$${v.toFixed(2)}`}
                  />
                  <Tooltip
                    content={<CostTooltip />}
                    cursor={{ fill: "hsl(215,15%,65%,0.08)" }}
                  />
                  <Bar dataKey="cost" radius={[4, 4, 0, 0]} maxBarSize={36}>
                    {chartData.map((_, idx) => (
                      <Cell key={idx} fill={BAR_COLORS[idx % BAR_COLORS.length]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Per-model breakdown table */}
          {chartData.length > 0 && (
            <div className="cost-breakdown__section">
              <span className="cost-breakdown__section-label">Per-Model Breakdown</span>
              {chartData.map((d, idx) => (
                <div key={d.model} className="cost-breakdown__row">
                  <span
                    className="cost-breakdown__dot"
                    style={{ background: BAR_COLORS[idx % BAR_COLORS.length] }}
                  />
                  <span className="cost-breakdown__model">{d.model}</span>
                  <span className="cost-breakdown__cost">${d.cost.toFixed(4)}</span>
                  <span className="cost-breakdown__tokens">
                    {d.tokens.toLocaleString()} tok
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </Panel>
    </div>
  );
}
