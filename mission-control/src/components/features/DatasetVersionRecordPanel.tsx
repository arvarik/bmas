"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ResourceState } from "@/components/ui/ResourceState";
import { errorText, type StoredDatasetVersionRecord } from "@/lib/evaluation-operations";
import { datasetVersionRows, lineageSummary, splitRows } from "@/lib/evidence-presentation";

/**
 * The stored version record of one dataset version: its content and
 * policy digests, its split manifest, its asset digests, and its
 * source lineage. A version published before the record existed has
 * none, and the panel says so instead of failing.
 */
export function DatasetVersionRecordPanel({ datasetId, versionId }: { datasetId: string; versionId: string }) {
  const [stored, setStored] = useState<StoredDatasetVersionRecord | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setStored(null);
    setMissing(false);
    try {
      const response = await fetch(`/api/evaluation/datasets/${encodeURIComponent(datasetId)}/versions/${encodeURIComponent(versionId)}/record`, { cache: "no-store" });
      if (response.status === 404) {
        setMissing(true);
        setError(null);
        return;
      }
      const data = await response.json() as StoredDatasetVersionRecord & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The version record is unavailable"));
      setStored(data);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The version record is unavailable");
    }
  }, [datasetId, versionId]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  const rows = useMemo(() => (stored ? datasetVersionRows(stored.record) : []), [stored]);
  const splits = useMemo(() => (stored ? splitRows(stored.record) : []), [stored]);
  const lineage = useMemo(() => (stored ? lineageSummary(stored.record) : null), [stored]);

  return (
    <section className="benchmark-catalog dataset-version-record" aria-labelledby="dataset-version-record-title">
      <header className="dataset-catalog__toolbar">
        <div><h3 id="dataset-version-record-title">Version record</h3><span>{stored ? `Recorded ${new Date(stored.created_at).toLocaleString()} · checksum ${stored.record_checksum.slice(0, 12)}` : missing ? "No stored record for this version" : error ? "Unavailable" : "Loading"}</span></div>
      </header>
      {error ? <ResourceState kind="unavailable" title="Version record unavailable" description={error} onRetry={load} compact /> : null}
      {missing ? <ResourceState kind="empty" title="No version record" description="This version was published before the lineage record existed, or it was created outside the governed publication path. Publish a new version to record its digests and lineage." compact /> : null}
      {stored && lineage ? (
        <div className="dataset-version-record__body">
          <dl className="benchmark-metadata dataset-version-record__digests" aria-label="Version digests">
            {rows.map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.kind === "digest" ? <code>{row.value || "Unavailable"}</code> : row.value}</dd></div>)}
          </dl>
          <div className="dataset-version-record__grid">
            <div aria-label="Source lineage">
              <h4>Source lineage</h4>
              <ul>{lineage.sources.length ? lineage.sources.map((source) => <li key={source}><code>{source}</code></li>) : <li>No source recorded</li>}</ul>
              <h4>Trust inputs</h4>
              <ul>{lineage.trust.length ? lineage.trust.map((entry) => <li key={entry}>{entry}</li>) : <li>None</li>}</ul>
              <h4>Effective restrictions</h4>
              <ul>{lineage.restrictions.length ? lineage.restrictions.map((entry) => <li key={entry}>{entry}</li>) : <li>None</li>}</ul>
            </div>
            <div aria-label="Split manifest">
              <h4>Split manifest</h4>
              <div className="benchmark-table-wrap">
                <table className="benchmark-table">
                  <thead><tr><th>Split</th><th>Cases</th><th>Sample</th></tr></thead>
                  <tbody>{splits.length ? splits.map((split) => <tr key={split.split}><td>{split.split}</td><td>{split.count}</td><td>{split.sample.join(", ")}{split.count > split.sample.length ? ", and more" : ""}</td></tr>) : <tr><td colSpan={3}>No split recorded</td></tr>}</tbody>
                </table>
              </div>
              <h4>Asset digests ({lineage.assetCount})</h4>
              <ul>{stored.record.asset_digests.length ? stored.record.asset_digests.map((digest) => <li key={digest}><code>{digest}</code></li>) : <li>No asset</li>}</ul>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
