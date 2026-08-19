"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Database, FileCheck2, Plus, RefreshCw, Search } from "lucide-react";
import { DatasetImportPanel } from "@/components/features/DatasetImportPanel";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { Skeleton } from "@/components/ui/Skeleton";
import type { DatasetSummary } from "@/lib/datasets";
import { diagnosticsText, failureFromReason, failureFromResponse, type RequestFailure } from "@/lib/request-state";

interface DatasetListResponse {
  datasets: DatasetSummary[];
  total: number;
  max_upload_bytes: number;
  accepted_types: string[];
}
export function DatasetsPageClient() {
  const [data, setData] = useState<DatasetListResponse | null>(null);
  const [failure, setFailure] = useState<RequestFailure | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [showImport, setShowImport] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setFailure(null);
    try {
      const response = await fetch("/api/datasets?limit=200", {
        cache: "no-store",
        signal: AbortSignal.timeout(10_000),
      });
      if (!response.ok) throw await failureFromResponse(response, "Dataset registry request failed");
      setData(await response.json() as DatasetListResponse);
    } catch (reason) {
      setFailure(failureFromReason(reason, "Dataset registry request failed"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return data?.datasets ?? [];
    return (data?.datasets ?? []).filter((dataset) => [
      dataset.name,
      dataset.description,
      dataset.author,
      dataset.license,
      dataset.latest_checksum,
    ].some((value) => value?.toLowerCase().includes(normalized)));
  }, [data?.datasets, query]);

  return (
    <div className="datasets-page">
      <header className="page-header">
        <div>
          <p className="page-eyebrow">Evaluate</p>
          <h2>Datasets</h2>
          <p>Import, validate, and publish immutable benchmark data.</p>
        </div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()} loading={loading}>
            <RefreshCw size={15} /> Refresh
          </ActionButton>
          <ActionButton onClick={() => setShowImport((current) => !current)}>
            <Plus size={15} /> {showImport ? "Close import" : "Import dataset"}
          </ActionButton>
        </div>
      </header>

      {showImport && data ? (
        <DatasetImportPanel
          maxUploadBytes={data.max_upload_bytes}
          onImported={() => {
            setShowImport(false);
            void load();
          }}
        />
      ) : null}

      {loading && !data ? <Skeleton variant="list" lines={8} /> : null}

      {!loading && failure ? (
        <ResourceState
          kind={failure.kind}
          title={failure.kind === "permission" ? "Dataset access denied" : "Dataset registry unavailable"}
          description="Mission Control cannot load benchmark datasets from the daemon."
          detail={failure.detail}
          diagnostics={diagnosticsText("Dataset registry", failure)}
          onRetry={load}
          operationsHref="/infra"
        />
      ) : null}

      {data ? (
        <section className="dataset-catalog" aria-labelledby="dataset-catalog-title">
          <header className="dataset-catalog__toolbar">
            <div>
              <h3 id="dataset-catalog-title">Published datasets</h3>
              <span>{visible.length} of {data.total} datasets</span>
            </div>
            <label className="dataset-search">
              <span className="sr-only">Search datasets</span>
              <Search size={15} aria-hidden="true" />
              <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name, author, license, or checksum" />
            </label>
          </header>

          {data.datasets.length === 0 ? (
            <ResourceState
              kind="empty"
              title="No benchmark datasets"
              description="Import a CSV or JSONL file to publish the first immutable dataset version."
            />
          ) : visible.length === 0 ? (
            <ResourceState
              kind="empty"
              title="No datasets match this search"
              description="Change the search text to see more datasets."
            />
          ) : (
            <ul className="dataset-card-grid" aria-label="Dataset catalog">
              {visible.map((dataset) => (
                <li key={dataset.id}>
                  <Link href={`/datasets/${encodeURIComponent(dataset.id)}`} className="dataset-card">
                    <header>
                      <span className="dataset-card__icon"><Database size={18} aria-hidden="true" /></span>
                      <div><strong>{dataset.name}</strong><small>{dataset.id}</small></div>
                      <span className="dataset-status"><FileCheck2 size={13} /> Published</span>
                    </header>
                    <p>{dataset.description || "No description was provided."}</p>
                    <dl>
                      <div><dt>Items</dt><dd>{(dataset.item_count ?? 0).toLocaleString()}</dd></div>
                      <div><dt>Versions</dt><dd>{dataset.version_count}</dd></div>
                      <div><dt>Latest</dt><dd>v{dataset.latest_version ?? 1}</dd></div>
                      <div><dt>License</dt><dd>{dataset.license || "Not specified"}</dd></div>
                    </dl>
                    <footer>
                      <span>{dataset.author || "Unknown author"}</span>
                      <code>{dataset.latest_checksum?.slice(0, 12) || "No checksum"}</code>
                    </footer>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}
    </div>
  );
}
