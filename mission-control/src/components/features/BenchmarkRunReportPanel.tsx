"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Download, RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import {
  formatMetric,
  statusLabel,
  type BenchmarkRun,
  type BenchmarkRunReport,
} from "@/lib/benchmarks";
import { Select } from "@/components/ui/Select";

const FILTER_NAMES = ["subject", "split", "tag", "scorer_id"] as const;

function interval(metric: { ci_low: number | null; ci_high: number | null }, unit: "percent" | "cost") {
  if (metric.ci_low === null || metric.ci_high === null) return "More samples needed";
  return `${formatMetric(metric.ci_low, unit)} to ${formatMetric(metric.ci_high, unit)}`;
}

function probability(value: number | null) {
  return value === null ? "Unavailable" : value.toFixed(value < 0.001 ? 4 : 3);
}

export function BenchmarkRunReportPanel({ run }: { run: BenchmarkRun }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [report, setReport] = useState<BenchmarkRunReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const query = useMemo(() => {
    const next = new URLSearchParams();
    for (const name of FILTER_NAMES) {
      const value = searchParams.get(name);
      if (value) next.set(name, value);
    }
    return next.toString();
  }, [searchParams]);
  const load = useCallback(async () => {
    try {
      const response = await fetch(
        `/api/benchmarks/runs/${encodeURIComponent(run.id)}/report${query ? `?${query}` : ""}`,
        { cache: "no-store" },
      );
      const data = await response.json() as BenchmarkRunReport & { error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? "The comparison report is unavailable");
      setReport(data);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The comparison report is unavailable");
    }
  }, [query, run.id]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);

  const options = useMemo(() => ({
    subjects: [...new Set((run.attempts ?? []).map((item) => item.subject).filter(Boolean))] as string[],
    splits: [...new Set((run.attempts ?? []).map((item) => item.split).filter(Boolean))] as string[],
    tags: [...new Set((run.attempts ?? []).flatMap((item) => item.tags ?? []))],
    scorers: [...new Map((run.scores ?? []).map((item) => [item.scorer_id, item.scorer_name])).entries()],
  }), [run.attempts, run.scores]);
  const setFilter = (name: typeof FILTER_NAMES[number], value: string) => {
    const next = new URLSearchParams(searchParams.toString());
    if (value) next.set(name, value);
    else next.delete(name);
    router.replace(`${pathname}${next.size ? `?${next}` : ""}`, { scroll: false });
  };
  const exportHref = `/api/benchmarks/runs/${encodeURIComponent(run.id)}/report.csv${query ? `?${query}` : ""}`;

  return (
    <section className="benchmark-catalog benchmark-report" aria-labelledby="comparison-report-title">
      <header className="dataset-catalog__toolbar benchmark-report__header">
        <div>
          <h3 id="comparison-report-title">Comparison report</h3>
          <span>The report uses the latest retry for each item and repetition.</span>
        </div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()}>
            <RefreshCw size={15} /> Refresh
          </ActionButton>
          <Link className="button" href={exportHref} download>
            <Download size={15} /> Export filtered CSV
          </Link>
        </div>
      </header>
      <div className="benchmark-report__filters" aria-label="Report filters">
        <label>Subject<Select value={searchParams.get("subject") ?? ""} onChange={(event) => setFilter("subject", event.target.value)}><option value="">All subjects</option>{options.subjects.map((value) => <option key={value}>{value}</option>)}</Select></label>
        <label>Split<Select value={searchParams.get("split") ?? ""} onChange={(event) => setFilter("split", event.target.value)}><option value="">All splits</option>{options.splits.map((value) => <option key={value}>{value}</option>)}</Select></label>
        <label>Tag<Select value={searchParams.get("tag") ?? ""} onChange={(event) => setFilter("tag", event.target.value)}><option value="">All tags</option>{options.tags.map((value) => <option key={value}>{value}</option>)}</Select></label>
        <label>Scorer<Select value={searchParams.get("scorer_id") ?? ""} onChange={(event) => setFilter("scorer_id", event.target.value)}><option value="">All scorers</option>{options.scorers.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</Select></label>
      </div>
      {error ? <ResourceState kind="unavailable" title="Comparison report unavailable" description={error} onRetry={load} /> : null}
      {!report && !error ? <div className="page-loading">Loading comparison report…</div> : null}
      {report ? (
        <>
          {!report.complete ? <p className="benchmark-report__notice">This run is incomplete. The metrics can change until every attempt reaches a terminal state.</p> : null}
          <p className="benchmark-report__provenance">
            {report.latest_attempt_count} current attempts. {report.prior_attempt_count} prior retries remain in the run record. Analysis v{report.analysis.version} uses {report.analysis.bootstrap_resamples} deterministic BCa resamples and Holm correction. Report <code>{report.report_checksum.slice(0, 12)}</code>.
          </p>
          {report.warnings.length ? <ul className="benchmark-report__warnings" aria-label="Statistical warnings">{report.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul> : null}
          <div className="benchmark-report__arms">
            {report.arms.map((arm) => (
              <article key={arm.arm_id}>
                <header><div><h4>{arm.arm_name}</h4><p>{arm.runtime_id}</p></div><span>{arm.completed_count}/{arm.attempt_count} completed</span></header>
                <dl>
                  <div><dt>Failure rate</dt><dd>{formatMetric(arm.failure_rate, "percent")}</dd></div>
                  <div><dt>Mean cost</dt><dd>{formatMetric(arm.cost_usd.mean, "cost")}</dd><small>95% estimate: {interval(arm.cost_usd, "cost")}</small></div>
                  <div><dt>p95 duration</dt><dd>{formatMetric(arm.duration_ms.p95, "duration")}</dd></div>
                  <div><dt>Mean tokens</dt><dd>{formatMetric(arm.tokens.mean, "tokens")}</dd></div>
                </dl>
                <table className="benchmark-report__score-table">
                  <caption>Scorer results for {arm.arm_name}</caption>
                  <thead><tr><th>Scorer</th><th>Mean</th><th>95% estimate</th><th>Pass / fail</th></tr></thead>
                  <tbody>{arm.scorers.map((scorer) => <tr key={scorer.scorer_id}><td>{scorer.scorer_name} <small>v{scorer.scorer_version}</small></td><td>{formatMetric(scorer.mean, "percent")}</td><td>{interval(scorer, "percent")}</td><td>{scorer.passed} / {scorer.failed}<small>{scorer.excluded ? `${scorer.excluded} excluded` : ""}</small></td></tr>)}</tbody>
                </table>
              </article>
            ))}
          </div>
          <div className="benchmark-table-wrap">
            <table className="benchmark-table">
              <caption>Paired arm differences. Positive values favor the right arm.</caption>
              <thead><tr><th>Comparison</th><th>Scorer</th><th>Matched</th><th>Effect</th><th>95% estimate</th><th>Holm p</th><th>Diagnosis</th><th>Recommended pairs</th></tr></thead>
              <tbody>{report.comparisons.flatMap((comparison) => comparison.scorers.map((scorer) => <tr key={`${comparison.left_arm_id}-${comparison.right_arm_id}-${scorer.scorer_id}`}><td>{comparison.left_arm_name} → {comparison.right_arm_name}</td><td>{scorer.scorer_id}<small>{scorer.wins} wins · {scorer.ties} ties · {scorer.losses} losses</small></td><td>{scorer.count}</td><td>{formatMetric(scorer.mean, "percent")}<small>Superiority {formatMetric(scorer.probability_of_superiority, "percent")}</small></td><td>{interval(scorer, "percent")}</td><td>{probability(scorer.p_value_adjusted)}</td><td>{statusLabel(scorer.classification)}</td><td>{scorer.sample_guidance.recommended_pairs ?? "More variance data needed"}</td></tr>))}</tbody>
            </table>
          </div>
          <section className="benchmark-diagnostics" aria-labelledby="benchmark-diagnostics-title">
            <header><div><h4 id="benchmark-diagnostics-title">Diagnosis</h4><p>Use slices, failure categories, and review agreement to explain a change.</p></div></header>
            <div className="benchmark-diagnostics__grid">
              <article><h5>Human review</h5><strong>{report.diagnostics.human_review.reviewed_attempt_count}</strong><p>{report.diagnostics.human_review.available ? `${report.diagnostics.human_review.review_count} immutable judgments` : report.diagnostics.human_review.reason}</p></article>
              <article><h5>Per-item differences</h5><strong>{report.diagnostics.item_difference_count}</strong><p>{report.diagnostics.item_differences_truncated ? "The report shows the 500 largest absolute changes." : "The report includes every scored pair."}</p></article>
              <article><h5>Analyzed slices</h5><strong>{report.diagnostics.slices.length}</strong><p>Subject, split, and tag groups use the same interval method.</p></article>
            </div>
            {report.diagnostics.error_categories.length ? <div className="benchmark-table-wrap"><table className="benchmark-table"><caption>Execution errors by arm</caption><thead><tr><th>Arm</th><th>Category</th><th>Count</th><th>Rate</th></tr></thead><tbody>{report.diagnostics.error_categories.map((item) => <tr key={`${item.arm_id}-${item.category}`}><td>{item.arm_name}</td><td>{statusLabel(item.category)}</td><td>{item.count}</td><td>{formatMetric(item.rate, "percent")}</td></tr>)}</tbody></table></div> : null}
            {report.diagnostics.human_calibration.length ? <div className="benchmark-table-wrap"><table className="benchmark-table"><caption>Automatic scorer calibration against human review</caption><thead><tr><th>Scorer</th><th>Pairs</th><th>Agreement</th><th>Cohen kappa</th><th>Mean absolute error</th><th>Brier score</th></tr></thead><tbody>{report.diagnostics.human_calibration.map((item) => <tr key={item.scorer_id}><td>{item.scorer_id}</td><td>{item.count}</td><td>{formatMetric(item.agreement_rate, "percent")}</td><td>{item.cohen_kappa?.toFixed(3) ?? "Unavailable"}</td><td>{item.mean_absolute_error.toFixed(3)}</td><td>{item.brier_score.toFixed(3)}</td></tr>)}</tbody></table></div> : null}
            {report.diagnostics.slices.length ? <details className="benchmark-diagnostics__slices"><summary>Inspect {report.diagnostics.slices.length} slices</summary><div className="benchmark-table-wrap"><table className="benchmark-table"><thead><tr><th>Slice</th><th>Attempts</th><th>Arm</th><th>Failure rate</th><th>Scores</th></tr></thead><tbody>{report.diagnostics.slices.flatMap((slice) => slice.arms.map((arm) => <tr key={`${slice.dimension}-${slice.value}-${arm.arm_id}`}><td>{statusLabel(slice.dimension)}: {slice.value}</td><td>{arm.attempt_count}</td><td>{arm.arm_name}</td><td>{formatMetric(arm.failure_rate, "percent")}</td><td>{arm.scorers.map((scorer) => `${scorer.scorer_id}: ${formatMetric(scorer.mean, "percent")}`).join(", ") || "Unavailable"}</td></tr>))}</tbody></table></div></details> : null}
          </section>
        </>
      ) : null}
    </section>
  );
}
