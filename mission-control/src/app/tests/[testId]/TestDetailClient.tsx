"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Play, Plus, RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { BenchmarkTestForm } from "@/components/features/BenchmarkTestForm";
import { ResourceState } from "@/components/ui/ResourceState";
import type { BenchmarkTest } from "@/lib/benchmarks";

export function TestDetailClient({ testId }: { testId: string }) {
  const router = useRouter();
  const [test, setTest] = useState<BenchmarkTest | null>(null);
  const [newRevision, setNewRevision] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [priorities, setPriorities] = useState<Record<string, number>>({});
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(async () => { const response = await fetch(`/api/benchmarks/tests/${encodeURIComponent(testId)}`); const data = await response.json() as BenchmarkTest & { detail?: string }; if (!response.ok) throw new Error(data.detail ?? "The test is unavailable"); setTest(data); }, [testId]);
  useEffect(() => { void Promise.resolve().then(load).catch((reason: unknown) => setMessage(reason instanceof Error ? reason.message : "The test is unavailable")); }, [load]);
  const startRun = async (revisionId: string) => { setPending(revisionId); setMessage(null); try { const response = await fetch(`/api/benchmarks/tests/${encodeURIComponent(testId)}/revisions/${encodeURIComponent(revisionId)}/runs`, { method: "POST", headers: { "Content-Type": "application/json", "X-Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ operator_note: "Started from Mission Control", priority: priorities[revisionId] ?? 0 }) }); const data = await response.json() as { id?: string; detail?: string }; if (!response.ok || !data.id) throw new Error(data.detail ?? "The run could not start"); router.push(`/runs/${encodeURIComponent(data.id)}`); } catch (reason) { setMessage(reason instanceof Error ? reason.message : "The run could not start"); setPending(null); } };
  if (!test && !message) return <div className="page-loading">Loading test…</div>;
  if (!test) return <ResourceState kind="unavailable" title="Test unavailable" description={message ?? "The test is unavailable."} onRetry={() => void load()} />;
  return <div className="benchmarks-page"><header className="page-header"><div><p className="page-eyebrow">Benchmark test</p><h2>{test.name}</h2><p>{test.description || "No description was provided."}</p></div><div className="page-header__actions"><ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton><ActionButton onClick={() => setNewRevision((current) => !current)}><Plus size={15} /> New revision</ActionButton></div></header>{message ? <p className="benchmark-message benchmark-message--error" role="alert">{message}</p> : null}{newRevision ? <BenchmarkTestForm testId={testId} /> : null}<div className="benchmark-revisions">{test.revisions?.map((revision) => <section className="benchmark-revision" key={revision.id}><header><div><h3>Revision {revision.revision}</h3><p>{revision.dataset_name} v{revision.dataset_version} · {revision.item_count} items</p></div><div className="benchmark-run-start"><label>Queue priority<select value={priorities[revision.id] ?? 0} onChange={(event) => setPriorities((current) => ({ ...current, [revision.id]: Number(event.target.value) }))}><option value={-50}>Low</option><option value={0}>Normal</option><option value={50}>High</option><option value={100}>Urgent</option></select></label><ActionButton loading={pending === revision.id} onClick={() => void startRun(revision.id)}><Play size={15} /> Start run</ActionButton></div></header><dl className="benchmark-metadata"><div><dt>Repetitions</dt><dd>{String(revision.configuration.repetitions ?? 1)}</dd></div><div><dt>Concurrency</dt><dd>{String(revision.configuration.max_concurrency ?? 1)}</dd></div><div><dt>Timeout</dt><dd>{String(revision.configuration.timeout_seconds ?? 3600)}s</dd></div><div><dt>Checksum</dt><dd><code>{revision.configuration_checksum.slice(0, 12)}</code></dd></div></dl><div className="benchmark-arm-summary">{revision.arms.map((arm) => <article key={arm.id}><strong>{arm.name}</strong><span>{arm.runtime_id}</span><code>{arm.configuration_checksum.slice(0, 12)}</code></article>)}</div>{revision.runs.length ? <footer>{revision.runs.map((run) => <Link key={run.id} href={`/runs/${encodeURIComponent(run.id)}`}>{run.id.slice(-8)} · {run.status}</Link>)}</footer> : null}</section>)}</div></div>;
}
