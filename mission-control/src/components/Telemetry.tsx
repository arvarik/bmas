"use client";

import { useEffect, useState, useCallback } from "react";
import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/Skeleton";
import { Monitor } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface SystemInfo {
  cpu: number; mem: number; memUsed: number; memTotal: number;
  temps: { label: string; value: number }[];
}

interface BeszelResponse {
  hub_status?: string;
  cpu?: number; memPct?: number; memUsed?: number; memTotal?: number;
  temperatures?: Array<{ label: string; value: number }>;
  note?: string;
  error?: string;
}

const POLL_INTERVAL = 5_000;

function clamp(n: number): number { return Math.max(0, Math.min(100, n)); }

function tempColor(c: number): string {
  if (c < 50) return "var(--status-success)";
  if (c < 70) return "var(--status-paused)";
  if (c < 85) return "hsl(32, 80%, 55%)";
  return "var(--status-error)";
}

// ── Radial Gauge ──────────────────────────────────────────────────────

function RadialGauge({ label, value, unit, color, subtitle }: {
  label: string; value: number; unit: string; color: string; subtitle?: string;
}) {
  const r = 36, circ = 2 * Math.PI * r;
  const offset = circ - (clamp(value) / 100) * circ;

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "var(--space-1)" }}>
      <div style={{ position: "relative", width: 88, height: 88 }}>
        <svg width="88" height="88" style={{ transform: "rotate(-90deg)" }}>
          <circle cx="44" cy="44" r={r} fill="none" stroke="var(--surface-hover)" strokeWidth="7" />
          <circle cx="44" cy="44" r={r} fill="none" stroke={color} strokeWidth="7"
            strokeLinecap="round" strokeDasharray={circ} strokeDashoffset={offset}
            style={{ transition: "stroke-dashoffset 700ms ease-out, stroke 300ms ease" }} />
        </svg>
        <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <span style={{ fontSize: "var(--text-lg)", fontWeight: "var(--weight-semibold)", fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>
            {value.toFixed(1)}{unit}
          </span>
        </div>
      </div>
      <span style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--text-secondary)" }}>{label}</span>
      {subtitle && <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>{subtitle}</span>}
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────

export default function Telemetry() {
  const [info, setInfo] = useState<SystemInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastFetch, setLastFetch] = useState<string | null>(null);

  const fetchTelemetry = useCallback(async () => {
    try {
      // Use the server-side proxy to avoid CORS and IP issues
      const res = await fetch("/api/telemetry", { cache: "no-store", signal: AbortSignal.timeout(4_000) });
      if (!res.ok) { setError(`Beszel returned ${res.status}`); return; }
      const data = (await res.json()) as BeszelResponse;
      if (data.error) { setError(data.error); return; }
      setInfo({ cpu: data.cpu ?? 0, mem: data.memPct ?? 0, memUsed: data.memUsed ?? 0, memTotal: data.memTotal ?? 0, temps: data.temperatures ?? [] });
      setError(null);
      setLastFetch(new Date().toLocaleTimeString());
    } catch (err) { setError(err instanceof Error ? err.message : "Beszel unreachable"); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void fetchTelemetry(), 0);
    const id = setInterval(fetchTelemetry, POLL_INTERVAL);
    return () => { clearTimeout(timer); clearInterval(id); };
  }, [fetchTelemetry]);

  const panelStatus = loading ? "loading" as const : error && !info ? "error" as const : undefined;

  return (
    <Panel
      title="Infrastructure"
      subtitle="Hardware telemetry"
      status={panelStatus}
      errorMessage={error ? "Beszel Hub unreachable" : undefined}
      emptyIcon={Monitor}
      emptyMessage="No telemetry data"
      emptyHint="Verify Beszel Hub connection."
      onRetry={error ? fetchTelemetry : undefined}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
        {/* Stale data warning */}
        {error && info && (
          <div style={{ padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-sm)", background: "hsl(0,84%,60%,0.1)", borderLeft: "3px solid var(--status-error)", fontSize: "var(--text-xs)", color: "var(--status-error)" }}>
            ⚠ Beszel Hub unreachable — last successful fetch: {lastFetch}
          </div>
        )}

        {info && (
          <>
            {/* CPU + Memory Gauges */}
            <div style={{ display: "flex", justifyContent: "space-around", gap: "var(--space-4)" }}>
              <RadialGauge label="CPU" value={info.cpu} unit="%" color={info.cpu < 75 ? "var(--status-running)" : "var(--status-error)"} />
              <RadialGauge label="RAM" value={info.mem} unit="%" color={info.mem < 75 ? "var(--status-running)" : "var(--status-error)"} subtitle={`${info.memUsed.toFixed(1)} / ${info.memTotal.toFixed(1)} GB`} />
            </div>

            {/* Thermals */}
            {info.temps.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
                <span style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--text-tertiary)" }}>Thermals</span>
                {info.temps.map((t) => (
                  <div key={t.label} style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
                    <span style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", width: 80, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={t.label}>{t.label}</span>
                    <div style={{ flex: 1, height: 6, borderRadius: "var(--radius-full)", background: "var(--surface-hover)", overflow: "hidden" }}>
                      <div style={{ height: "100%", borderRadius: "var(--radius-full)", width: `${clamp(t.value)}%`, background: tempColor(t.value), transition: "width 500ms ease-out, background 300ms ease" }} />
                    </div>
                    <span style={{ fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", width: 48, textAlign: "right", color: tempColor(t.value) }}>{t.value}°C</span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        {!info && !error && <Skeleton variant="metric" />}
      </div>
    </Panel>
  );
}
