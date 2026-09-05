"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { GitCompare, RefreshCw, Snowflake, X } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { FreezeAnalysisForm } from "@/components/features/FreezeAnalysisForm";
import { ResourceState } from "@/components/ui/ResourceState";
import type { BenchmarkRun } from "@/lib/benchmarks";
import {
  sideBySide,
  snapshotChain,
  supersessionReasonLabel,
  type AnalysisOverview,
  type AnalysisSnapshotSummary,
} from "@/lib/analysis-history-presentation";

interface ComparisonState {
  leftId: string;
  rightId: string;
  left: AnalysisOverview | null;
  right: AnalysisOverview | null;
  error: string | null;
  loading: boolean;
}

async function fetchOverview(runId: string, snapshotId: string): Promise<AnalysisOverview> {
  const response = await fetch(
    `/api/evaluation/runs/${encodeURIComponent(runId)}/analyses/${encodeURIComponent(snapshotId)}/overview`,
    { cache: "no-store" },
  );
  const data = await response.json() as AnalysisOverview & { error?: string; detail?: string };
  if (!response.ok) throw new Error(data.error ?? data.detail ?? "The analysis overview is unavailable");
  return data;
}

/**
 * The analysis history of one run: every stored snapshot, which one
 * is current, which supersession replaced which, and a side-by-side
 * comparison of a superseded snapshot with its successor.
 */
export function AnalysisHistoryPanel({ runId, run }: { runId: string; run?: BenchmarkRun }) {
  const [snapshots, setSnapshots] = useState<AnalysisSnapshotSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comparison, setComparison] = useState<ComparisonState | null>(null);
  const [freezing, setFreezing] = useState(false);
  const load = useCallback(async () => {
    try {
      const response = await fetch(`/api/evaluation/runs/${encodeURIComponent(runId)}/analyses`, { cache: "no-store" });
      const data = await response.json() as { snapshots?: AnalysisSnapshotSummary[]; error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? "The analysis history is unavailable");
      setSnapshots(data.snapshots ?? []);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The analysis history is unavailable");
    }
  }, [runId]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  const chain = useMemo(() => snapshotChain(snapshots ?? []), [snapshots]);
  const compare = async (leftId: string, rightId: string) => {
    setComparison({ leftId, rightId, left: null, right: null, error: null, loading: true });
    try {
      const [left, right] = await Promise.all([fetchOverview(runId, leftId), fetchOverview(runId, rightId)]);
      setComparison({ leftId, rightId, left, right, error: null, loading: false });
    } catch (reason) {
      setComparison({ leftId, rightId, left: null, right: null, loading: false, error: reason instanceof Error ? reason.message : "The analysis overview is unavailable" });
    }
  };
  const rows = useMemo(() => comparison && comparison.left && comparison.right ? sideBySide(comparison.left, comparison.right) : [], [comparison]);

  return (
    <section className="benchmark-catalog analysis-history" aria-labelledby="analysis-history-title">
      <header className="dataset-catalog__toolbar">
        <div>
          <h3 id="analysis-history-title">Analysis history</h3>
          <span>{snapshots ? `${snapshots.length} stored snapshots` : "Loading"}{chain.current ? ` · current ${chain.current.id.slice(-12)}` : ""}</span>
        </div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
          {run ? <ActionButton variant={freezing ? "secondary" : "primary"} onClick={() => setFreezing((value) => !value)}>{freezing ? <X size={15} /> : <Snowflake size={15} />} {freezing ? "Close" : "Freeze analysis"}</ActionButton> : null}
        </div>
      </header>
      {run && freezing ? <FreezeAnalysisForm run={run} onFrozen={() => { setFreezing(false); void load(); }} /> : null}
      {error ? <ResourceState kind="unavailable" title="Analysis history unavailable" description={error} onRetry={load} /> : null}
      {snapshots && snapshots.length === 0 ? <ResourceState kind="empty" title="No frozen analysis" description={run ? "Freeze an analysis to record the first snapshot." : "Freeze an analysis through the evaluation API to record the first snapshot."} /> : null}
      {chain.entries.length ? (
        <div className="benchmark-table-wrap">
          <table className="benchmark-table analysis-history__table">
            <caption>Snapshots in creation order. A superseded snapshot never changes; its successor holds the recomputation.</caption>
            <thead><tr><th>Snapshot</th><th>Created</th><th>State</th><th>Supersession</th><th>Compare</th></tr></thead>
            <tbody>
              {chain.entries.map((entry) => (
                <tr key={entry.snapshot.id} data-current={entry.snapshot.current ? "true" : "false"}>
                  <td><code>{entry.snapshot.id.slice(-12)}</code><small>checksum {entry.snapshot.record_checksum.slice(0, 12)}</small></td>
                  <td>{new Date(entry.snapshot.created_at).toLocaleString()}</td>
                  <td><span className={`benchmark-status benchmark-status--${entry.snapshot.current ? "passed" : "paused"}`}>{entry.snapshot.current ? "Current" : "Superseded"}</span></td>
                  <td>
                    {entry.replacedBy ? <>Replaced by <code>{entry.replacedBy.id.slice(-12)}</code><small>{supersessionReasonLabel(entry.snapshot.supersession_reason)}</small></> : null}
                    {entry.replaces ? <><small>Replaces <code>{entry.replaces.id.slice(-12)}</code></small></> : null}
                    {!entry.replacedBy && !entry.replaces ? <small>No supersession</small> : null}
                  </td>
                  <td>
                    {entry.replacedBy ? (
                      <ActionButton variant="secondary" onClick={() => void compare(entry.snapshot.id, (entry.replacedBy as AnalysisSnapshotSummary).id)}>
                        <GitCompare size={15} /> Compare with successor
                      </ActionButton>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {comparison ? (
        <section className="analysis-history__compare" aria-labelledby="analysis-compare-title">
          <h4 id="analysis-compare-title">Superseded <code>{comparison.leftId.slice(-12)}</code> beside current <code>{comparison.rightId.slice(-12)}</code></h4>
          {comparison.loading ? <div className="page-loading">Loading both overviews…</div> : null}
          {comparison.error ? <ResourceState kind="unavailable" title="Overview unavailable" description={comparison.error} compact /> : null}
          {rows.length ? (
            <div className="benchmark-table-wrap">
              <table className="benchmark-table analysis-history__side-by-side">
                <thead><tr><th>Field</th><th>Superseded</th><th>Current</th></tr></thead>
                <tbody>{rows.map((row) => <tr key={row.key} data-changed={row.changed ? "true" : "false"}><td>{row.label}</td><td>{row.left}</td><td>{row.right}{row.changed ? <small className="analysis-history__changed">changed</small> : null}</td></tr>)}</tbody>
              </table>
            </div>
          ) : null}
        </section>
      ) : null}
    </section>
  );
}
