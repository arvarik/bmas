"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, CheckCircle2, FlaskConical, RefreshCw, Server, ShieldCheck } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { useToast } from "@/hooks/useToast";
import {
  statusLabel,
  type BenchmarkCapacity,
  type BenchmarkRuntime,
  type BenchmarkRuntimeCatalog,
  type RuntimeQualification,
} from "@/lib/benchmarks";

async function responseJson<T>(response: Response, fallback: string): Promise<T> {
  const data = await response.json() as T & { error?: string; detail?: string };
  if (!response.ok) throw new Error(data.error ?? data.detail ?? fallback);
  return data;
}

function CapacityPanel({ capacity }: { capacity: BenchmarkCapacity }) {
  const liveWorkers = capacity.workers.filter(
    (worker) => worker.status === "active" && !worker.stale,
  ).length;
  return (
    <section className="benchmark-catalog" aria-labelledby="benchmark-capacity-title">
      <header className="dataset-catalog__toolbar">
        <div>
          <h3 id="benchmark-capacity-title">Scheduler capacity</h3>
          <span>Fenced leases prevent two scheduler replicas from owning one attempt.</span>
        </div>
        <span className="benchmark-status benchmark-status--running">
          {capacity.global.active} of {capacity.global.limit} active
        </span>
      </header>
      <div className="benchmark-capacity-summary">
        <article><Activity size={18} /><div><strong>{capacity.queue.total}</strong><span>Queued attempts</span></div></article>
        <article><Server size={18} /><div><strong>{liveWorkers}</strong><span>Live scheduler replicas</span></div></article>
        <article><ShieldCheck size={18} /><div><strong>{capacity.global.available}</strong><span>Global slots available</span></div></article>
      </div>
      {capacity.resources.length ? (
        <div className="benchmark-table-wrap">
          <table className="benchmark-table">
            <caption>Configured runtime, provider, and model capacity limits</caption>
            <thead><tr><th>Resource</th><th>Active</th><th>Limit</th><th>Available</th></tr></thead>
            <tbody>{capacity.resources.map((resource) => (
              <tr key={resource.key}>
                <td><code>{resource.key}</code></td><td>{resource.active}</td><td>{resource.limit}</td><td>{resource.available}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <p className="benchmark-report__notice">No resource-specific limits apply. The global limit still applies.</p>}
      <div className="benchmark-table-wrap">
        <table className="benchmark-table">
          <caption>Scheduler replica ownership</caption>
          <thead><tr><th>Replica</th><th>State</th><th>Attempts</th><th>Last heartbeat</th></tr></thead>
          <tbody>{capacity.workers.map((worker) => {
            const live = worker.status === "active" && !worker.stale;
            return (
              <tr key={worker.worker_id}>
                <td><strong>{worker.hostname}</strong><small>{worker.worker_id}</small></td>
                <td><span className={`benchmark-status benchmark-status--${live ? "running" : "failed"}`}>{live ? "Live" : worker.stale ? "Stale" : "Stopped"}</span></td>
                <td>{worker.owned_attempts}</td><td>{new Date(worker.last_seen_at).toLocaleString()}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
    </section>
  );
}

export function RuntimesPageClient() {
  const { toast } = useToast();
  const [catalog, setCatalog] = useState<BenchmarkRuntimeCatalog | null>(null);
  const [capacity, setCapacity] = useState<BenchmarkCapacity | null>(null);
  const [runIds, setRunIds] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [capacityError, setCapacityError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setCapacityError(null);
    const [catalogResult, capacityResult] = await Promise.allSettled([
      fetch("/api/benchmarks/runtimes", { cache: "no-store" }).then((response) =>
        responseJson<BenchmarkRuntimeCatalog>(response, "The runtime catalog is unavailable")
      ),
      fetch("/api/benchmarks/capacity", { cache: "no-store" }).then((response) =>
        responseJson<BenchmarkCapacity>(response, "The scheduler capacity is unavailable")
      ),
    ]);
    if (catalogResult.status === "fulfilled") setCatalog(catalogResult.value);
    else setError(catalogResult.reason instanceof Error ? catalogResult.reason.message : "The runtime catalog is unavailable");
    if (capacityResult.status === "fulfilled") setCapacity(capacityResult.value);
    else setCapacityError(capacityResult.reason instanceof Error ? capacityResult.reason.message : "The scheduler capacity is unavailable");
  }, []);

  useEffect(() => { void Promise.resolve().then(load); }, [load]);

  const qualifications = useMemo(() => {
    const grouped = new Map<string, RuntimeQualification[]>();
    for (const qualification of catalog?.qualifications ?? []) {
      grouped.set(qualification.runtime_id, [
        ...(grouped.get(qualification.runtime_id) ?? []),
        qualification,
      ]);
    }
    return grouped;
  }, [catalog?.qualifications]);

  const qualify = async (runtime: BenchmarkRuntime) => {
    setPending(runtime.id);
    setError(null);
    try {
      const response = await fetch(
        `/api/benchmarks/runtimes/${encodeURIComponent(runtime.id)}/qualify`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ run_id: runIds[runtime.id]?.trim() || null }),
        },
      );
      const data = await responseJson<RuntimeQualification>(response, "The runtime qualification failed");
      toast({
        type: data.status === "failed" ? "error" : "success",
        message: `Qualification ${data.status}. ${data.report.run_id ? "The runtime used saved run evidence." : "The runtime used static contract checks."}`,
      });
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The runtime qualification failed");
    } finally {
      setPending(null);
    }
  };

  if (!catalog) {
    return (
      <div className="benchmarks-page">
        <header className="page-header"><div><p className="page-eyebrow">Evaluate</p><h2>Runtime qualifications</h2><p>Verify each registered runtime against the shared benchmark contract.</p></div></header>
        {error
          ? <ResourceState kind="unavailable" title="Runtime catalog unavailable" description={error} onRetry={load} operationsHref="/infra" />
          : <div className="page-loading">Loading runtime qualifications…</div>}
      </div>
    );
  }

  return (
    <div className="benchmarks-page">
      <header className="page-header">
        <div><p className="page-eyebrow">Evaluate</p><h2>Runtime qualifications</h2><p>Verify each runtime contract and inspect shared benchmark capacity.</p></div>
        <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      {capacity ? <CapacityPanel capacity={capacity} /> : capacityError ? (
        <ResourceState kind="unavailable" title="Scheduler capacity unavailable" description={capacityError} onRetry={load} operationsHref="/infra" />
      ) : <div className="page-loading">Loading scheduler capacity…</div>}
      <section className="benchmark-runtime-grid" aria-label="Available benchmark runtimes">
        {catalog.variants.map((runtime) => {
          const latest = qualifications.get(runtime.id)?.[0];
          return (
            <article key={runtime.id}>
              <header>
                <div><ShieldCheck size={20} /><div><h3>{runtime.label}</h3><p><code>{runtime.id}</code> · contract {runtime.contract_version}</p></div></div>
                <span className={`benchmark-status benchmark-status--${latest?.status ?? "queued"}`}>{latest ? statusLabel(latest.status) : "Not qualified"}</span>
              </header>
              <dl>
                <div><dt>Benchmark support</dt><dd>{runtime.benchmark.supported ? "Declared" : "Unavailable"}</dd></div>
                <div><dt>Seed behavior</dt><dd>{statusLabel(runtime.benchmark.seed_strategy)}</dd></div>
                <div><dt>Repetitions</dt><dd>{runtime.benchmark.supports_repetitions ? "Supported" : "Unavailable"}</dd></div>
                <div><dt>Recovery</dt><dd>{runtime.supports_recovery ? "Supported" : "Not declared"}</dd></div>
              </dl>
              <details><summary>Contract details</summary><p>Required snapshot fields</p><ul>{runtime.benchmark.required_snapshot_fields.map((field) => <li key={field}><code>{field}</code></li>)}</ul><pre>{JSON.stringify(runtime.benchmark.configuration_schema, null, 2)}</pre></details>
              <div className="benchmark-runtime__qualify">
                <label>Completed evidence run <span>Optional</span><input value={runIds[runtime.id] ?? ""} onChange={(event) => setRunIds((current) => ({ ...current, [runtime.id]: event.target.value }))} placeholder="run identifier" /></label>
                <ActionButton loading={pending === runtime.id} onClick={() => void qualify(runtime)}><FlaskConical size={15} /> {runIds[runtime.id]?.trim() ? "Qualify with evidence" : "Run static checks"}</ActionButton>
              </div>
              {latest ? (
                <div className="benchmark-runtime__result">
                  <p><CheckCircle2 size={15} /> Latest saved result: {statusLabel(latest.status)}</p>
                  <small>{latest.report.run_id ? `Evidence run ${latest.report.run_id}` : "Static checks only. Run evidence remains required for a passed result."}</small>
                  <details><summary>{latest.report.checks.length} checks</summary><ul>{latest.report.checks.map((check) => <li key={check.name}><span className={`benchmark-status benchmark-status--${check.status}`}>{statusLabel(check.status)}</span><div><strong>{statusLabel(check.name)}</strong><small>{check.detail}</small></div></li>)}</ul></details>
                </div>
              ) : null}
            </article>
          );
        })}
      </section>
      {catalog.planned_runtime_ids.length ? (
        <section className="benchmark-catalog">
          <header className="dataset-catalog__toolbar"><div><h3>Planned runtime adapters</h3><span>These runtimes cannot run benchmarks yet.</span></div></header>
          <ul className="benchmark-planned-runtimes">{catalog.planned_runtime_ids.map((runtimeId) => <li key={runtimeId}><strong>{statusLabel(runtimeId)}</strong><span className="benchmark-status">Not implemented</span><p>The runtime needs a versioned adapter and completed-run evidence.</p></li>)}</ul>
        </section>
      ) : null}
    </div>
  );
}
