"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts";
import { Panel } from "@/components/ui/Panel";
import { MetricCard } from "@/components/ui/MetricCard";
import { AGENT_COLORS } from "@/lib/design-tokens";
import { DollarSign } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface CostMetrics { cost: Record<string, string>; tokens: Record<string, string>; }
interface ChartDatum { model: string; cost: number; tokens: number; }

const POLL_INTERVAL = 10_000;

const BAR_COLORS = [
  AGENT_COLORS.planner, AGENT_COLORS.executor, AGENT_COLORS.auditor,
  "hsl(217,91%,60%)", "hsl(38,92%,50%)", "hsl(142,71%,45%)",
  "hsl(0,84%,60%)", "hsl(220,15%,50%)",
];

// ── Custom Tooltip ────────────────────────────────────────────────────

function CostTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: ChartDatum }> }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div style={{ borderRadius: "var(--radius-md)", background: "var(--surface-overlay)", border: "1px solid var(--border-default)", padding: "var(--space-2) var(--space-3)", boxShadow: "var(--shadow-md)", fontSize: "var(--text-sm)" }}>
      <p style={{ fontWeight: "var(--weight-semibold)", color: "var(--text-primary)", marginBottom: 4 }}>{d.model}</p>
      <p style={{ color: "var(--status-running)" }}>Cost: <span style={{ fontFamily: "var(--font-mono)" }}>${d.cost.toFixed(4)}</span></p>
      <p style={{ color: "var(--text-secondary)" }}>Tokens: <span style={{ fontFamily: "var(--font-mono)" }}>{d.tokens.toLocaleString()}</span></p>
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────

export default function CostTracker() {
  const [metrics, setMetrics] = useState<CostMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchCost = useCallback(async () => {
    try {
      const res = await fetch("/api/cost", { cache: "no-store" });
      if (!res.ok) { setError(`HTTP ${res.status}`); return; }
      const data = (await res.json()) as CostMetrics;
      setMetrics(data);
      setError(null);
    } catch (err) { setError(err instanceof Error ? err.message : "Fetch failed"); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    // Schedule the first fetch asynchronously to avoid synchronous setState in effect body
    const timer = setTimeout(() => void fetchCost(), 0);
    const id = setInterval(fetchCost, POLL_INTERVAL);
    return () => { clearTimeout(timer); clearInterval(id); };
  }, [fetchCost]);

  const chartData = useMemo<ChartDatum[]>(() => {
    if (!metrics) return [];
    const models = new Set([...Object.keys(metrics.cost), ...Object.keys(metrics.tokens)]);
    return Array.from(models)
      .map((m) => ({ model: shortenModel(m), cost: parseFloat(metrics.cost[m] ?? "0"), tokens: parseInt(metrics.tokens[m] ?? "0", 10) }))
      .sort((a, b) => b.cost - a.cost);
  }, [metrics]);

  const totalCost = useMemo(() => chartData.reduce((a, d) => a + d.cost, 0), [chartData]);
  const totalTokens = useMemo(() => chartData.reduce((a, d) => a + d.tokens, 0), [chartData]);
  const estimatedSavings = totalCost * 0.4;

  const panelStatus = loading ? "loading" as const : error && !metrics ? "error" as const : undefined;

  return (
    <Panel
      title="Cost & Tokens"
      status={panelStatus}
      errorMessage={error ?? undefined}
      emptyIcon={DollarSign}
      emptyMessage="No cost data yet"
      onRetry={error ? fetchCost : undefined}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
        {/* Error banner for stale data */}
        {error && metrics && (
          <div style={{ padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-sm)", background: "hsl(0,84%,60%,0.1)", borderLeft: "3px solid var(--status-error)", fontSize: "var(--text-xs)", color: "var(--status-error)" }}>
            ⚠ {error} — showing last known values
          </div>
        )}

        {/* MetricCards — MetricCard tracks previousValue internally via currentValueRef */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "var(--space-3)" }}>
          <MetricCard label="Total Spend" value={totalCost || 0} format="currency" />
          <MetricCard label="Tokens" value={totalTokens || 0} format="number" />
          <MetricCard label="Est. Savings" value={estimatedSavings || 0} format="currency" />
        </div>

        {/* Bar Chart */}
        {chartData.length > 0 ? (
          <div style={{ height: 140, width: "100%" }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 4, right: 4, left: -12, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(222,36%,16%)" vertical={false} />
                <XAxis dataKey="model" tick={{ fill: "hsl(220,10%,45%)", fontSize: 10 }} axisLine={{ stroke: "hsl(222,20%,22%)" }} tickLine={false} />
                <YAxis tick={{ fill: "hsl(220,10%,45%)", fontSize: 10 }} axisLine={{ stroke: "hsl(222,20%,22%)" }} tickLine={false} tickFormatter={(v: number) => `$${v.toFixed(2)}`} />
                <Tooltip content={<CostTooltip />} cursor={{ fill: "hsl(215,15%,65%,0.08)" }} />
                <Bar dataKey="cost" radius={[4, 4, 0, 0]} maxBarSize={36}>
                  {chartData.map((_, idx) => (<Cell key={idx} fill={BAR_COLORS[idx % BAR_COLORS.length]} />))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          !loading && (
            <div style={{ height: 100, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-tertiary)", fontSize: "var(--text-sm)" }}>
              No cost data yet
            </div>
          )
        )}

        {/* Per-model breakdown */}
        {chartData.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
            <span style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--text-tertiary)" }}>Per-Model Breakdown</span>
            {chartData.map((d, idx) => (
              <div key={d.model} style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", padding: "var(--space-1) var(--space-3)", borderRadius: "var(--radius-sm)", background: "var(--surface-hover)" }}>
                <span style={{ width: 8, height: 8, borderRadius: "var(--radius-full)", background: BAR_COLORS[idx % BAR_COLORS.length], flexShrink: 0 }} />
                <span style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.model}</span>
                <span style={{ fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", color: "var(--status-running)" }}>${d.cost.toFixed(4)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Panel>
  );
}

function shortenModel(name: string): string {
  return name.replace(/-\d{4,}[-\d]*/g, "").replace(/^models\//, "");
}
