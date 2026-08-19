"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Download, RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import {
  formatMetric,
  type BenchmarkRun,
  type BenchmarkRunReport,
} from "@/lib/benchmarks";

const FILTER_NAMES = ["subject", "split", "tag", "scorer_id"] as const;

function interval(metric: { ci_low: number | null; ci_high: number | null }, unit: "percent" | "cost") {
  if (metric.ci_low === null || metric.ci_high === null) return "More samples needed";
  return `${formatMetric(metric.ci_low, unit)} to ${formatMetric(metric.ci_high, unit)}`;
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
        <label>Subject<select value={searchParams.get("subject") ?? ""} onChange={(event) => setFilter("subject", event.target.value)}><option value="">All subjects</option>{options.subjects.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Split<select value={searchParams.get("split") ?? ""} onChange={(event) => setFilter("split", event.target.value)}><option value="">All splits</option>{options.splits.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Tag<select value={searchParams.get("tag") ?? ""} onChange={(event) => setFilter("tag", event.target.value)}><option value="">All tags</option>{options.tags.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Scorer<select value={searchParams.get("scorer_id") ?? ""} onChange={(event) => setFilter("scorer_id", event.target.value)}><option value="">All scorers</option>{options.scorers.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      </div>
      {error ? <ResourceState kind="unavailable" title="Comparison report unavailable" description={error} onRetry={load} /> : null}
      {!report && !error ? <div className="page-loading">Loading comparison report…</div> : null}
      {report ? (
        <>
          {!report.complete ? <p className="benchmark-report__notice">This run is incomplete. The metrics can change until every attempt reaches a terminal state.</p> : null}
          <p className="benchmark-report__provenance">
            {report.latest_attempt_count} current attempts. {report.prior_attempt_count} prior retries remain in the run record. Report <code>{report.report_checksum.slice(0, 12)}</code>.
          </p>
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
              <thead><tr><th>Comparison</th><th>Scorer</th><th>Matched</th><th>Right minus left</th><th>95% estimate</th><th>Wins / ties / losses</th></tr></thead>
              <tbody>{report.comparisons.flatMap((comparison) => comparison.scorers.map((scorer) => <tr key={`${comparison.left_arm_id}-${comparison.right_arm_id}-${scorer.scorer_id}`}><td>{comparison.left_arm_name} → {comparison.right_arm_name}</td><td>{scorer.scorer_id}</td><td>{scorer.count}</td><td>{formatMetric(scorer.mean, "percent")}</td><td>{interval(scorer, "percent")}</td><td>{scorer.wins} / {scorer.ties} / {scorer.losses}</td></tr>))}</tbody>
            </table>
          </div>
        </>
      ) : null}
    </section>
  );
}
