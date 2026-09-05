"use client";

import { useMemo, useState } from "react";
import { Archive, Download, Upload } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { Select } from "@/components/ui/Select";
import { useToast } from "@/hooks/useToast";
import { errorText, statusWords, type ReplayExport, type ReplayImport } from "@/lib/evaluation-operations";
import { archiveBytes, base64FromBytes, bytesText, importSummary, memberRows } from "@/lib/evidence-presentation";

/**
 * Export the replay bundle of one run and import a bundle back.
 *
 * The export shows the manifest members by class with their digests
 * before the archive downloads. The import quarantines unreadable
 * members, strips undeclared fields, and, with an approval, replays
 * the analysis and compares the results digest.
 */
export function ReplayBundlePanel({ runId, snapshotIds = [] }: { runId: string; snapshotIds?: string[] }) {
  const { toast } = useToast();
  const [policy, setPolicy] = useState("redacted");
  const [snapshotId, setSnapshotId] = useState("");
  const [exported, setExported] = useState<ReplayExport | null>(null);
  const [exporting, setExporting] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [approve, setApprove] = useState(true);
  const [actor, setActor] = useState("");
  const [policyVersion, setPolicyVersion] = useState("1");
  const [imported, setImported] = useState<ReplayImport | null>(null);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const members = useMemo(() => memberRows(exported?.manifest), [exported]);
  const summary = useMemo(() => (imported ? importSummary(imported) : null), [imported]);

  const runExport = async () => {
    setExporting(true);
    setError(null);
    try {
      const query = new URLSearchParams({ policy });
      if (snapshotId) query.set("snapshot_id", snapshotId);
      const response = await fetch(`/api/evaluation/runs/${encodeURIComponent(runId)}/replay-bundles?${query}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const data = await response.json() as ReplayExport & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The bundle export failed"));
      setExported(data);
      toast({ type: "success", message: `Bundle exported with ${data.member_count} members.` });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The bundle export failed");
    } finally {
      setExporting(false);
    }
  };

  const download = () => {
    if (!exported) return;
    const blob = new Blob([archiveBytes(exported.archive_base64) as BlobPart], { type: "application/zip" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `replay-bundle-${runId}-${exported.bundle_digest.slice(0, 12)}.zip`;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const runImport = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file) return;
    setImporting(true);
    setError(null);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const body: { archive_base64: string; approval?: { actor: string; policy_version: string } } = { archive_base64: base64FromBytes(bytes) };
      if (approve) body.approval = { actor: actor.trim() || "mission-control", policy_version: policyVersion.trim() || "1" };
      const response = await fetch("/api/evaluation/replay-bundles/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await response.json() as ReplayImport & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The bundle import failed"));
      setImported(data);
      toast({ type: "success", message: `Bundle imported: ${statusWords(data.ingestion_state)}${data.replay ? `, replay ${data.replay.analysis_replayable ? "verified" : "mismatch"}` : ""}.` });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The bundle import failed");
    } finally {
      setImporting(false);
    }
  };

  return (
    <section className="benchmark-catalog replay-bundle" aria-labelledby="replay-bundle-title">
      <header className="dataset-catalog__toolbar">
        <div><h3 id="replay-bundle-title">Replay bundle</h3><span>Export the redacted or complete bundle, inspect its manifest, and import a bundle with a replay approval.</span></div>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      <div className="replay-bundle__grid">
        <div className="replay-bundle__export" aria-label="Bundle export">
          <h4><Archive size={15} /> Export</h4>
          <div className="benchmark-form__grid">
            <label>Policy<Select value={policy} onChange={(event) => setPolicy(event.target.value)}><option value="redacted">Redacted (shareable)</option><option value="complete">Complete (internal)</option></Select></label>
            <label>Snapshot<Select value={snapshotId} onChange={(event) => setSnapshotId(event.target.value)}><option value="">Current snapshot</option>{snapshotIds.map((id) => <option key={id} value={id}>{id}</option>)}</Select></label>
          </div>
          <div className="page-header__actions">
            <ActionButton loading={exporting} onClick={() => void runExport()}>Export bundle</ActionButton>
            {exported ? <ActionButton variant="secondary" onClick={download}><Download size={15} /> Download archive</ActionButton> : null}
          </div>
          {exported ? (
            <div className="replay-bundle__manifest" aria-label="Bundle manifest">
              <dl className="benchmark-metadata">
                <div><dt>Members</dt><dd>{exported.member_count}</dd></div>
                <div><dt>Bundle digest</dt><dd><code>{exported.bundle_digest}</code></dd></div>
                <div><dt>Manifest digest</dt><dd><code>{exported.manifest_digest}</code></dd></div>
                <div><dt>Redaction policy</dt><dd><code>{exported.manifest.redaction_policy_digest}</code></dd></div>
                <div><dt>Claims</dt><dd>{Object.entries(exported.manifest.claims ?? {}).map(([name, value]) => `${statusWords(name)}: ${statusWords(String(value))}`).join(" · ") || "None"}</dd></div>
              </dl>
              <div className="benchmark-table-wrap">
                <table className="benchmark-table">
                  <caption>Manifest members by class</caption>
                  <thead><tr><th>Class</th><th>Members</th><th>Size</th></tr></thead>
                  <tbody>{members.map((row) => <tr key={row.memberClass}><td>{row.label}</td><td>{row.count}</td><td>{bytesText(row.bytes)}</td></tr>)}</tbody>
                </table>
              </div>
              <details><summary>Every member ({exported.manifest.members.length})</summary><ul className="replay-bundle__members">{exported.manifest.members.map((member) => <li key={member.path}><code>{member.path}</code><small>{member.class} · {bytesText(member.size_bytes)} · {member.digest.slice(0, 16)}</small></li>)}</ul></details>
            </div>
          ) : null}
        </div>
        <form className="replay-bundle__import" aria-label="Bundle import" onSubmit={runImport}>
          <h4><Upload size={15} /> Import</h4>
          <label className="replay-bundle__file">Bundle archive<input type="file" accept=".zip,application/zip" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
          <label className="replay-bundle__approve"><input type="checkbox" checked={approve} onChange={(event) => setApprove(event.target.checked)} /> Approve the analysis replay</label>
          {approve ? (
            <div className="benchmark-form__grid">
              <label>Approving actor<input value={actor} onChange={(event) => setActor(event.target.value)} placeholder="operator name" /></label>
              <label>Policy version<input value={policyVersion} onChange={(event) => setPolicyVersion(event.target.value)} /></label>
            </div>
          ) : null}
          <div className="page-header__actions"><ActionButton type="submit" loading={importing} disabled={!file}>Import bundle</ActionButton></div>
          {summary ? (
            <div className={`verdict-banner verdict-banner--${summary.tone}`} role="status" aria-label="Import result">
              <strong>{summary.title}</strong>
              <dl className="benchmark-metadata">{summary.rows.map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.kind === "digest" ? <code>{row.value}</code> : row.value}</dd></div>)}</dl>
            </div>
          ) : null}
        </form>
      </div>
    </section>
  );
}
