"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { errorText, statusWords, type RunStudy } from "@/lib/evaluation-operations";
import { checkLabel, estimateRows, verdictSummary } from "@/lib/study-presentation";

/**
 * The study of one run and its admission verdict.
 *
 * A run whose test revision came from a published study admits only
 * when every study condition holds at admission. The panel shows the
 * study, every check with its detail, the blocking conditions, and
 * how many attempts failed admission because of them.
 */
export function RunStudyPanel({ runId, blockedAttempts }: { runId: string; blockedAttempts: number }) {
  const [study, setStudy] = useState<RunStudy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const response = await fetch(`/api/evaluation/runs/${encodeURIComponent(runId)}/study`, { cache: "no-store" });
      const data = await response.json() as RunStudy & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The study conditions are unavailable"));
      setStudy(data);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The study conditions are unavailable");
    }
  }, [runId]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  const verdict = useMemo(() => verdictSummary(study?.verdict), [study]);
  const estimates = useMemo(() => (study?.study ? estimateRows(study.study) : []), [study]);

  return (
    <section className="benchmark-catalog run-study" aria-labelledby="run-study-title">
      <header className="dataset-catalog__toolbar">
        <div>
          <h3 id="run-study-title">Study conditions</h3>
          <span>{study?.study ? `${study.study.name} · ${statusWords(study.study.study_type)}` : study ? "This run carries no study plan" : "Loading"}</span>
        </div>
        <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
      </header>
      {error ? <ResourceState kind="unavailable" title="Study conditions unavailable" description={error} onRetry={load} compact /> : null}
      {study && !study.study ? (
        <ResourceState kind="empty" title="No study plan" description="The test revision of this run was not published from a study, so admission checks only the budget. Author a study to predeclare the arms, the estimand, and the gates." compact />
      ) : null}
      {study?.study ? (
        <div className="run-study__body">
          <div className={`verdict-banner verdict-banner--${verdict.tone}`} role="status">
            <strong>{verdict.title}</strong>
            {verdict.blocking.length ? <ul aria-label="Blocking conditions">{verdict.blocking.map((name) => <li key={name}>{checkLabel(name)}</li>)}</ul> : <span>Every study condition holds at the admission stage.</span>}
            {blockedAttempts > 0 ? <small>{blockedAttempts} attempt{blockedAttempts === 1 ? "" : "s"} failed admission with a configuration failure that names these conditions.</small> : null}
          </div>
          <dl className="benchmark-metadata run-study__facts">
            <div><dt>Study</dt><dd><Link href={`/studies/${encodeURIComponent(study.study_id ?? "")}`}>{study.study.name}</Link><small>{statusWords(study.study.study_type)} · digest {study.study.study_digest.slice(0, 12)}</small></dd></div>
            <div><dt>Run plan</dt><dd><code>{study.plan_id}</code></dd></div>
            <div><dt>Arms</dt><dd>{study.study.arms.map((arm) => arm.slug).join(", ")}</dd></div>
            {estimates.filter((row) => ["Attempts", "Estimated cost", "Estimated duration"].includes(row.label)).map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}
          </dl>
          <div className="benchmark-table-wrap">
            <table className="benchmark-table run-study__checks">
              <caption>Admission-stage checks of the study conditions</caption>
              <thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
              <tbody>
                {[...verdict.failed, ...verdict.passed].map((check) => (
                  <tr key={check.check} data-passed={check.passed ? "true" : "false"}>
                    <td>{checkLabel(check.check)}</td>
                    <td><span className={`benchmark-status benchmark-status--${check.passed ? "passed" : "failed"}`}>{check.passed ? "Holds" : "Blocks"}</span></td>
                    <td>{check.detail || "No detail"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </section>
  );
}
