"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { FlaskConical, Plus, Search, X } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { BenchmarkTestForm } from "@/components/features/BenchmarkTestForm";
import { ResourceState } from "@/components/ui/ResourceState";
import type { BenchmarkTest } from "@/lib/benchmarks";

export function TestsPageClient() {
  const [tests, setTests] = useState<BenchmarkTest[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [authoring, setAuthoring] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const response = await fetch(`/api/benchmarks/tests?limit=200&search=${encodeURIComponent(query)}`, { cache: "no-store" });
      const data = await response.json() as { tests?: BenchmarkTest[]; total?: number; detail?: string };
      if (!response.ok) throw new Error(data.detail ?? "The test catalog request failed");
      setTests(data.tests ?? []); setTotal(data.total ?? 0); setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "The test catalog is unavailable"); }
  }, [query]);
  useEffect(() => { const timeout = window.setTimeout(() => void load(), 200); return () => window.clearTimeout(timeout); }, [load]);
  const hasQuery = useMemo(() => Boolean(query.trim()), [query]);
  return <div className="benchmarks-page"><header className="page-header"><div><p className="page-eyebrow">Evaluate</p><h2>Tests</h2><p>Define repeatable benchmark plans with immutable datasets, runtimes, and scorers.</p></div><ActionButton variant={authoring ? "secondary" : "primary"} onClick={() => setAuthoring((current) => !current)}>{authoring ? <X size={15} /> : <Plus size={15} />} {authoring ? "Close" : "New test"}</ActionButton></header>{authoring ? <BenchmarkTestForm /> : null}<section className="benchmark-catalog" aria-labelledby="tests-title"><header className="dataset-catalog__toolbar"><div><h3 id="tests-title">Published tests</h3><span>{tests.length} of {total} tests</span></div><label className="dataset-search"><span className="sr-only">Search tests</span><Search size={15} /><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name or description" /></label></header>{error ? <ResourceState kind="unavailable" title="Test catalog unavailable" description={error} onRetry={load} /> : tests.length === 0 ? <ResourceState kind="empty" title={hasQuery ? "No tests match this search" : "No benchmark tests"} description={hasQuery ? "Change the search text." : "Publish a test to create a reusable benchmark plan."} /> : <ul className="benchmark-card-grid">{tests.map((test) => <li key={test.id}><Link href={`/tests/${encodeURIComponent(test.id)}`} className="benchmark-card"><header><FlaskConical size={18} /><div><strong>{test.name}</strong><small>Revision {test.latest_revision ?? 1}</small></div></header><p>{test.description || "No description was provided."}</p><dl><div><dt>Dataset</dt><dd>{test.dataset_name}</dd></div><div><dt>Items</dt><dd>{test.item_count ?? 0}</dd></div><div><dt>Arms</dt><dd>{test.arm_count ?? 0}</dd></div><div><dt>Runs</dt><dd>{test.run_count ?? 0}</dd></div></dl></Link></li>)}</ul>}</section></div>;
}
