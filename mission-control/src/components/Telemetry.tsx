"use client";

import { useEffect, useState, useCallback } from "react";
import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/Skeleton";
import { Monitor, Clock, HardDrive, Thermometer, Activity, Wifi } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface SystemData {
  id: string;
  name: string;
  host: string;
  status: string;
  updatedAt: string;
  cpu: number;
  memPct: number;
  diskPct: number;
  diskTotalGB: number;
  temp: number | null;
  uptimeSec: number;
  agentVersion: string | null;
  bandwidthBytes: number;
  cpuThreads: number;
  loadAvg: number[];
}

interface TelemetryResponse {
  hub_status?: string;
  systems?: SystemData[];
  error?: string;
  note?: string;
}

const POLL_INTERVAL = 5_000;

// ── Utilities ─────────────────────────────────────────────────────────

function clamp(n: number): number { return Math.max(0, Math.min(100, n)); }

function uptimeStr(sec: number): string {
  if (sec <= 0) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function statusColor(status: string): string {
  if (status === "up") return "var(--status-success)";
  if (status === "down") return "var(--status-error)";
  return "var(--status-paused)";
}

function gaugeColor(value: number): string {
  if (value < 50) return "var(--status-success)";
  if (value < 75) return "var(--status-paused)";
  if (value < 90) return "hsl(32, 80%, 55%)";
  return "var(--status-error)";
}

function tempColor(c: number): string {
  if (c < 50) return "var(--status-success)";
  if (c < 70) return "var(--status-paused)";
  if (c < 85) return "hsl(32, 80%, 55%)";
  return "var(--status-error)";
}

// ── Radial Gauge ──────────────────────────────────────────────────────

function RadialGauge({ label, value, unit, color, size = 72 }: {
  label: string; value: number; unit: string; color: string; size?: number;
}) {
  const r = (size - 12) / 2;
  const circ = 2 * Math.PI * r;
  const offset = circ - (clamp(value) / 100) * circ;

  return (
    <div className="infra-gauge">
      <div className="infra-gauge__ring" style={{ width: size, height: size }}>
        <svg width={size} height={size} style={{ transform: "rotate(-90deg)" }}>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-hover)" strokeWidth="6" />
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth="6"
            strokeLinecap="round" strokeDasharray={circ} strokeDashoffset={offset}
            style={{ transition: "stroke-dashoffset 700ms ease-out, stroke 300ms ease" }} />
        </svg>
        <span className="infra-gauge__value">
          {value.toFixed(1)}{unit}
        </span>
      </div>
      <span className="infra-gauge__label">{label}</span>
    </div>
  );
}

// ── Mini Bar ──────────────────────────────────────────────────────────

function MiniBar({ label, value, unit, color, icon: Icon }: {
  label: string; value: number; unit: string; color: string;
  icon: React.ComponentType<{ size?: number }>;
}) {
  return (
    <div className="infra-minibar">
      <div className="infra-minibar__header">
        <Icon size={13} />
        <span className="infra-minibar__label">{label}</span>
        <span className="infra-minibar__value" style={{ color }}>{value.toFixed(1)}{unit}</span>
      </div>
      <div className="infra-minibar__track">
        <div className="infra-minibar__fill" style={{
          width: `${clamp(value)}%`,
          background: color,
        }} />
      </div>
    </div>
  );
}

// ── Node Card ─────────────────────────────────────────────────────────

function NodeCard({ sys }: { sys: SystemData }) {
  return (
    <div className="infra-node-card">
      {/* Header */}
      <div className="infra-node-card__header">
        <div className="infra-node-card__dot" style={{ background: statusColor(sys.status) }} />
        <div className="infra-node-card__identity">
          <span className="infra-node-card__name">{sys.name}</span>
          <span className="infra-node-card__host">{sys.host}</span>
        </div>
        <span className="infra-node-card__status" style={{ color: statusColor(sys.status) }}>
          {sys.status}
        </span>
      </div>

      {/* Gauges row */}
      <div className="infra-node-card__gauges">
        <RadialGauge label="CPU" value={sys.cpu} unit="%" color={gaugeColor(sys.cpu)} />
        <RadialGauge label="Memory" value={sys.memPct} unit="%" color={gaugeColor(sys.memPct)} />
        <RadialGauge label="Disk" value={sys.diskPct} unit="%" color={gaugeColor(sys.diskPct)} />
      </div>

      {/* Stats grid */}
      <div className="infra-node-card__stats">
        {sys.temp !== null && (
          <MiniBar label="Temp" value={sys.temp} unit="°C" color={tempColor(sys.temp)} icon={Thermometer} />
        )}
        {sys.loadAvg.length > 0 && (
          <div className="infra-minibar">
            <div className="infra-minibar__header">
              <Activity size={13} />
              <span className="infra-minibar__label">Load Avg</span>
              <span className="infra-minibar__value" style={{ color: "var(--text-primary)" }}>
                {sys.loadAvg.map((v) => v.toFixed(2)).join(" / ")}
              </span>
            </div>
          </div>
        )}
        {sys.diskTotalGB > 0 && (
          <div className="infra-minibar">
            <div className="infra-minibar__header">
              <HardDrive size={13} />
              <span className="infra-minibar__label">Disk</span>
              <span className="infra-minibar__value" style={{ color: "var(--text-primary)" }}>
                {sys.diskTotalGB.toFixed(1)} GB
              </span>
            </div>
          </div>
        )}
      </div>

      {/* Footer meta */}
      <div className="infra-node-card__footer">
        <span className="infra-node-card__meta">
          <Clock size={11} />
          {uptimeStr(sys.uptimeSec)}
        </span>
        {sys.bandwidthBytes > 0 && (
          <span className="infra-node-card__meta">
            <Wifi size={11} />
            {formatBytes(sys.bandwidthBytes)}
          </span>
        )}
        {sys.agentVersion && (
          <span className="infra-node-card__meta">
            v{sys.agentVersion}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────

export default function Telemetry() {
  const [systems, setSystems] = useState<SystemData[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchTelemetry = useCallback(async () => {
    try {
      const res = await fetch("/api/telemetry", { cache: "no-store", signal: AbortSignal.timeout(6_000) });
      if (!res.ok) { setError(`Beszel returned ${res.status}`); return; }
      const data = (await res.json()) as TelemetryResponse;

      if (data.error) { setError(data.error); return; }
      if (data.note) { setNote(data.note); }

      setSystems(data.systems ?? []);
      setError(null);
    } catch (err) { setError(err instanceof Error ? err.message : "Beszel unreachable"); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void fetchTelemetry(), 0);
    const id = setInterval(fetchTelemetry, POLL_INTERVAL);
    return () => { clearTimeout(timer); clearInterval(id); };
  }, [fetchTelemetry]);

  const panelStatus = loading ? "loading" as const : error && systems.length === 0 ? "error" as const : undefined;
  const upCount = systems.filter((s) => s.status === "up").length;

  return (
    <Panel
      title="Infrastructure"
      subtitle={systems.length > 0 ? `${upCount}/${systems.length} systems online` : "Hardware telemetry"}
      status={panelStatus}
      errorMessage={error ? "Beszel Hub unreachable" : undefined}
      emptyIcon={Monitor}
      emptyMessage="No telemetry data"
      emptyHint={note ?? "Verify Beszel Hub connection and credentials in .env."}
      onRetry={error ? fetchTelemetry : undefined}
    >
      <div className="infra-content">
        {/* Stale data warning */}
        {error && systems.length > 0 && (
          <div className="infra-stale-warning">
            ⚠ Beszel Hub unreachable — showing last known data
          </div>
        )}

        {/* Node cards grid */}
        {systems.length > 0 && (
          <div className="infra-nodes-grid">
            {systems.map((sys) => (
              <NodeCard key={sys.id} sys={sys} />
            ))}
          </div>
        )}

        {!loading && !error && systems.length === 0 && !note && (
          <Skeleton variant="metric" />
        )}
      </div>
    </Panel>
  );
}
