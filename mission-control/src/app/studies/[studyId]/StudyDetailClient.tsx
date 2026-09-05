"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { BackLink } from "@/components/ui/BackLink";
import { ResourceState } from "@/components/ui/ResourceState";
import type { BenchmarkRun } from "@/lib/benchmarks";
import { errorText, statusWords, type StoredStudy } from "@/lib/evaluation-operations";
import { estimateRows } from "@/lib/study-presentation";

interface StudyInvariants {
  dataset_version_id?: string;
  case_ids?: string[];
  seed_schedule?: { base_seed?: number };
  scorers?: string[];
  arm_order?: string;
  repetitions?: number;
}

/**
 * One published study: its arms, its invariants, its estimand and
 * gates, its estimate, and every run that carries its plan.
 */
export function StudyDetailClient({ studyId }: { studyId: string }) {
  const [study, setStudy] = useState<StoredStudy | null>(null);
  const [runs, setRuns] = useState<BenchmarkRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const response = await fetch(`/api/evaluation/studies/${encodeURIComponent(studyId)}`, { cache: "no-store" });
      const data = await response.json() as StoredStudy & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The study is unavailable"));
      setStudy(data);
      setError(null);
      const runResponse = await fetch("/api/benchmarks/runs?limit=200", { cache: "no-store" });
      if (runResponse.ok) {
        const runData = await runResponse.json() as { runs?: BenchmarkRun[] };
        setRuns((runData.runs ?? []).filter((run) => run.test_revision_id === data.test_revision_id));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The study is unavailable");
    }
  }, [studyId]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  const rows = useMemo(() => (study ? estimateRows(study.record) : []), [study]);
  if (!study && !error) return <div className="page-loading">Loading study…</div>;
  if (!study) return <ResourceState kind="unavailable" title="Study unavailable" description={error ?? "The study is unavailable."} onRetry={load} />;
  const record = study.record;
  const invariants = record.invariants as StudyInvariants;
  const comparisons = ((record.gates.comparison_family as { comparisons?: Array<Record<string, unknown>> }).comparisons ?? []);
  const hypothesis = String((record.estimand as { hypothesis?: string }).hypothesis ?? "non_inferiority");
  return (
    <div className="benchmarks-page">
      <BackLink href="/studies" label="Studies" />
      <header className="page-header">
        <div><p className="page-eyebrow">Study</p><h2>{record.name}</h2><p>{statusWords(record.study_type)} over {record.treatment_paths.join(", ")} · digest {record.study_digest.slice(0, 12)}</p></div>
        <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      <section className="benchmark-catalog" aria-labelledby="study-plan-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="study-plan-title">Sample plan and estimate</h3><span>Published {new Date(study.created_at).toLocaleString()}</span></div></header>
        <dl className="benchmark-metadata study-detail__facts">
          {rows.map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}
          <div><dt>Run plan</dt><dd><code>{study.run_plan_id}</code></dd></div>
          <div><dt>Test revision</dt><dd><code>{study.test_revision_id}</code></dd></div>
          <div><dt>Hypothesis</dt><dd>{statusWords(hypothesis)}</dd></div>
        </dl>
      </section>
      <section className="benchmark-catalog" aria-labelledby="study-arms-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="study-arms-title">Arms</h3><span>One arm per treatment value, expanded from the base configuration.</span></div></header>
        <div className="benchmark-table-wrap">
          <table className="benchmark-table">
            <thead><tr><th>Arm</th><th>Treatment</th><th>Configuration digest</th></tr></thead>
            <tbody>{record.arms.map((arm) => <tr key={arm.slug}><td><strong>{arm.slug}</strong></td><td><code>{JSON.stringify(arm.treatment)}</code></td><td><code>{arm.configuration_digest}</code></td></tr>)}</tbody>
          </table>
        </div>
      </section>
      <section className="benchmark-catalog" aria-labelledby="study-invariants-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="study-invariants-title">Invariants and gates</h3><span>Frozen at publication. Admission rejects any run that violates them.</span></div></header>
        <dl className="benchmark-metadata study-detail__facts">
          <div><dt>Dataset version</dt><dd><code>{invariants.dataset_version_id ?? "Unavailable"}</code></dd></div>
          <div><dt>Cases</dt><dd>{invariants.case_ids?.length ?? 0}<small>{(invariants.case_ids ?? []).slice(0, 6).join(", ")}{(invariants.case_ids?.length ?? 0) > 6 ? ", and more" : ""}</small></dd></div>
          <div><dt>Seed schedule</dt><dd>base seed {invariants.seed_schedule?.base_seed ?? "?"}</dd></div>
          <div><dt>Scorers</dt><dd>{(invariants.scorers ?? []).join(", ")}</dd></div>
          <div><dt>Arm order</dt><dd>{statusWords(invariants.arm_order ?? "")}</dd></div>
          <div><dt>Repetitions</dt><dd>{invariants.repetitions ?? "?"}</dd></div>
          <div><dt>Gates</dt><dd>{record.gates.predeclared ? "Predeclared" : "Not predeclared"}<small>{comparisons.length} comparison{comparisons.length === 1 ? "" : "s"}: {comparisons.map((comparison) => `${String(comparison.baseline_arm)} vs ${String(comparison.candidate_arm)}${comparison.non_inferiority_margin !== undefined ? ` (margin ${String(comparison.non_inferiority_margin)})` : ""}`).join("; ")}</small></dd></div>
        </dl>
      </section>
      <section className="benchmark-catalog" aria-labelledby="study-runs-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="study-runs-title">Runs on this revision</h3><span>{runs.length} run{runs.length === 1 ? "" : "s"}. Each run page shows its admission verdict.</span></div></header>
        {runs.length === 0 ? <ResourceState kind="empty" title="No run yet" description="Start a run from the test revision this study published." compact /> : (
          <div className="benchmark-table-wrap">
            <table className="benchmark-table">
              <thead><tr><th>Run</th><th>Status</th><th>Attempts</th><th>Created</th></tr></thead>
              <tbody>{runs.map((run) => <tr key={run.id}><td><Link href={`/runs/${encodeURIComponent(run.id)}`}>{run.id}</Link></td><td><span className={`benchmark-status benchmark-status--${run.status}`}>{statusWords(run.status)}</span></td><td>{run.completed_attempts} of {run.total_attempts}</td><td>{new Date(run.created_at).toLocaleString()}</td></tr>)}</tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
