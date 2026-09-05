"use client";

import { useCallback, useState } from "react";
import { ResourceState } from "@/components/ui/ResourceState";
import { ActionButton } from "@/components/ui/ActionButton";
import {
  errorText,
  statusWords,
  type EvidenceEnvelope,
  type EvidenceSection,
  type StoredScoreRecord,
} from "@/lib/evaluation-operations";
import {
  DATA_CLASS_LABELS,
  evidenceSections,
  redactedPaths,
  redactionCounts,
  sandboxRows,
  terminalTone,
} from "@/lib/evidence-presentation";

interface SectionView {
  digest: string;
  label: string;
  section: string;
  loading: boolean;
  error: string | null;
  content: EvidenceSection | null;
}

/**
 * The stored score records and the evidence bundle of one attempt.
 *
 * Every score names the boundary it ran in with the runtime digest,
 * the terminal class, the fuel used, and any trap or limit. The
 * evidence viewer lists the persisted sections by digest and, for
 * every redacted path, the data class and the detector that fired,
 * with the policy digest that produced the redaction.
 */
export function AttemptEvidencePanel({ attemptId }: { attemptId: string }) {
  const [loaded, setLoaded] = useState(false);
  const [scores, setScores] = useState<StoredScoreRecord[] | null>(null);
  const [evidence, setEvidence] = useState<EvidenceEnvelope | null>(null);
  const [evidenceMissing, setEvidenceMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [section, setSection] = useState<SectionView | null>(null);

  const load = useCallback(async () => {
    setLoaded(true);
    try {
      const [scoreResponse, evidenceResponse] = await Promise.all([
        fetch(`/api/evaluation/attempts/${encodeURIComponent(attemptId)}/score-records`, { cache: "no-store" }),
        fetch(`/api/evaluation/attempts/${encodeURIComponent(attemptId)}/evidence`, { cache: "no-store" }),
      ]);
      const scoreData = await scoreResponse.json() as { scores?: StoredScoreRecord[]; error?: string; detail?: string };
      if (!scoreResponse.ok) throw new Error(errorText(scoreData, "The score records are unavailable"));
      setScores(scoreData.scores ?? []);
      if (evidenceResponse.status === 404) {
        setEvidence(null);
        setEvidenceMissing(true);
      } else {
        const evidenceData = await evidenceResponse.json() as EvidenceEnvelope & { error?: string; detail?: string };
        if (!evidenceResponse.ok) throw new Error(errorText(evidenceData, "The attempt evidence is unavailable"));
        setEvidence(evidenceData);
        setEvidenceMissing(false);
      }
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The attempt evidence is unavailable");
    }
  }, [attemptId]);

  const openSection = async (digest: string, label: string, sectionName: string) => {
    setSection({ digest, label, section: sectionName, loading: true, error: null, content: null });
    try {
      const response = await fetch(`/api/evaluation/evidence/sections/${encodeURIComponent(digest)}`, { cache: "no-store" });
      const data = await response.json() as EvidenceSection & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The evidence section is unavailable"));
      setSection({ digest, label, section: sectionName, loading: false, error: null, content: data });
    } catch (reason) {
      setSection({ digest, label, section: sectionName, loading: false, error: reason instanceof Error ? reason.message : "The evidence section is unavailable", content: null });
    }
  };

  const record = evidence?.record;
  const counts = redactionCounts(record?.redaction_report);
  const sections = record ? evidenceSections(record) : [];
  const sectionRedactions = section ? redactedPaths(record?.redaction_report, section.section === "artifact" ? null : section.section) : [];

  return (
    <details className="attempt-evidence" onToggle={(event) => { if ((event.currentTarget as HTMLDetailsElement).open && !loaded) void load(); }}>
      <summary>Scores and evidence</summary>
      {error ? <ResourceState kind="unavailable" title="Attempt evidence unavailable" description={error} onRetry={load} compact /> : null}
      <section className="attempt-evidence__scores" aria-label="Score records">
        <h5>Score records</h5>
        {scores && scores.length === 0 ? <p className="attempt-evidence__note">No stored score record. Scores from the legacy path carry no boundary evidence.</p> : null}
        {scores?.map((stored) => {
          const tone = terminalTone(stored.record);
          return (
            <article key={stored.id} className="attempt-evidence__score" data-boundary={stored.record.sandbox?.boundary ?? "none"}>
              <header>
                <strong>{stored.record.scorer.scorer_id} <small>v{stored.record.scorer.version}</small></strong>
                <span className={`benchmark-status benchmark-status--${tone}`}>{statusWords(stored.record.status)}</span>
                {stored.record.dimensions.map((dimension) => <span key={dimension.name} className="attempt-evidence__dimension">{dimension.name}: {dimension.value === null ? "none" : dimension.value}</span>)}
              </header>
              <dl className="benchmark-metadata attempt-evidence__sandbox">
                {sandboxRows(stored.record).map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.kind === "digest" ? <code>{row.value}</code> : row.value}</dd></div>)}
              </dl>
              {stored.record.explanation ? <p className="attempt-evidence__note">{stored.record.explanation}</p> : null}
            </article>
          );
        })}
      </section>
      <section className="attempt-evidence__bundle" aria-label="Attempt evidence">
        <h5>Evidence bundle</h5>
        {evidenceMissing ? <p className="attempt-evidence__note">No evidence bundle was captured for this attempt.</p> : null}
        {record ? (
          <>
            <dl className="benchmark-metadata attempt-evidence__facts">
              <div><dt>Source</dt><dd><span className={`benchmark-status benchmark-status--${evidence?.source === "current" ? "passed" : "paused"}`}>{statusWords(evidence?.source ?? "unknown")}</span></dd></div>
              <div><dt>Completeness</dt><dd>{statusWords(record.completeness.level)}{record.completeness.unavailable_sections.length ? <small>missing {record.completeness.unavailable_sections.join(", ")}</small> : null}</dd></div>
              <div><dt>Redaction policy</dt><dd><code>{record.redaction_policy_digest}</code><small>{counts.total} redacted path{counts.total === 1 ? "" : "s"}: {counts.secret} secret, {counts.sensitive} sensitive, {counts.prohibited} prohibited</small></dd></div>
              <div><dt>Case</dt><dd>{record.case_reference.case_id}</dd></div>
              {record.failure_classification ? <div><dt>Failure</dt><dd>{statusWords(record.failure_classification)}</dd></div> : null}
            </dl>
            <ul className="attempt-evidence__sections" aria-label="Evidence sections">
              {sections.length === 0 ? <li>No persisted section</li> : null}
              {sections.map((entry) => (
                <li key={entry.digest}>
                  <span>{entry.label}</span>
                  <code>{entry.digest.slice(0, 16)}</code>
                  <small>{redactedPaths(record.redaction_report, entry.section === "artifact" ? null : entry.section).length} redacted</small>
                  <ActionButton variant="secondary" onClick={() => void openSection(entry.digest, entry.label, entry.section)}>View</ActionButton>
                </li>
              ))}
            </ul>
            {section ? (
              <div className="attempt-evidence__viewer" aria-label={`${section.label} section`}>
                <header><strong>{section.label}</strong><code>{section.digest}</code></header>
                {section.loading ? <p className="attempt-evidence__note">Loading section…</p> : null}
                {section.error ? <p className="benchmark-message benchmark-message--error" role="alert">{section.error}</p> : null}
                {section.content?.redacted ? <p className="attempt-evidence__note">This section was erased under policy: {section.content.reason ?? "unknown reason"}.</p> : null}
                {section.content && !section.content.redacted ? <pre>{JSON.stringify(section.content.value, null, 2)}</pre> : null}
                {sectionRedactions.length ? (
                  <ul className="redaction-list" aria-label="Redacted paths">
                    {sectionRedactions.map((row) => <li key={row.path}><code>{row.path}</code><span className={`benchmark-status benchmark-status--${row.dataClass === "prohibited" ? "failed" : row.dataClass === "secret" ? "provisional" : "paused"}`}>{statusWords(row.dataClass)}</span><small>{DATA_CLASS_LABELS[row.dataClass]}{row.detector ? ` · detector ${row.detector}` : " · field name"} · policy {counts.policyDigest?.slice(0, 12)}</small></li>)}
                  </ul>
                ) : section.content ? <p className="attempt-evidence__note">No redaction inside this section.</p> : null}
              </div>
            ) : null}
          </>
        ) : null}
      </section>
    </details>
  );
}
