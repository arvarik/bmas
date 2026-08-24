"use client";

import { useCallback, useEffect, useState } from "react";
import { BackLink } from "@/components/ui/BackLink";
import { CheckCircle2, Copy, Download, Search } from "lucide-react";
import { DatasetImportPanel } from "@/components/features/DatasetImportPanel";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/hooks/useToast";
import type { DatasetDetail, DatasetItem, DatasetVersion } from "@/lib/datasets";
import { diagnosticsText, failureFromReason, failureFromResponse, type RequestFailure } from "@/lib/request-state";
import { Select } from "@/components/ui/Select";

export function DatasetDetailClient({ datasetId }: { datasetId: string }) {
  const [dataset, setDataset] = useState<DatasetDetail | null>(null);
  const [version, setVersion] = useState<DatasetVersion | null>(null);
  const [items, setItems] = useState<DatasetItem[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [failure, setFailure] = useState<RequestFailure | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [maxUploadBytes, setMaxUploadBytes] = useState(100 * 1024 * 1024);
  const { toast } = useToast();

  const loadDataset = useCallback(async () => {
    setLoading(true);
    setFailure(null);
    try {
      const response = await fetch(`/api/datasets/${encodeURIComponent(datasetId)}`, {
        cache: "no-store",
        signal: AbortSignal.timeout(10_000),
      });
      if (!response.ok) throw await failureFromResponse(response, "Dataset request failed");
      const body = await response.json() as { dataset: DatasetDetail; max_upload_bytes?: number };
      setDataset(body.dataset);
      if (body.max_upload_bytes) setMaxUploadBytes(body.max_upload_bytes);
      setVersion((current) => body.dataset.versions.find((candidate) => candidate.id === current?.id) ?? body.dataset.versions[0] ?? null);
    } catch (reason) {
      setFailure(failureFromReason(reason, "Dataset request failed"));
    } finally {
      setLoading(false);
    }
  }, [datasetId]);

  const loadItems = useCallback(async (selected: DatasetVersion, search: string) => {
    setItemsLoading(true);
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (search.trim()) params.set("search", search.trim());
      const response = await fetch(
        `/api/datasets/${encodeURIComponent(datasetId)}/versions/${encodeURIComponent(selected.id)}/items?${params}`,
        { cache: "no-store", signal: AbortSignal.timeout(10_000) },
      );
      if (!response.ok) throw await failureFromResponse(response, "Dataset items request failed");
      const body = await response.json() as { items: DatasetItem[]; total: number };
      setItems(body.items);
      setTotal(body.total);
    } catch (reason) {
      setFailure(failureFromReason(reason, "Dataset items request failed"));
    } finally {
      setItemsLoading(false);
    }
  }, [datasetId]);

  useEffect(() => {
    void Promise.resolve().then(loadDataset);
  }, [loadDataset]);

  useEffect(() => {
    if (!version) return;
    const timeout = window.setTimeout(() => void loadItems(version, query), 200);
    return () => window.clearTimeout(timeout);
  }, [loadItems, query, version]);

  if (loading && !dataset) return <Skeleton variant="list" lines={9} />;
  if (failure && !dataset) {
    return <ResourceState kind={failure.kind} title="Dataset unavailable" description="Mission Control cannot load this dataset." detail={failure.detail} diagnostics={diagnosticsText("Dataset detail", failure, { dataset_id: datasetId })} onRetry={loadDataset} operationsHref="/infra" />;
  }
  if (!dataset || !version) return <ResourceState kind="empty" title="Dataset has no versions" description="Publish a version before you inspect dataset items." />;

  return (
    <div className="dataset-detail-page">
      <header className="page-header dataset-detail-header">
        <div>
          <BackLink href="/datasets" label="Datasets" />
          <p className="page-eyebrow">Evaluate</p>
          <h2>{dataset.name}</h2>
          <p>{dataset.description || "No description was provided."}</p>
        </div>
        <ActionButton onClick={() => setShowImport((current) => !current)}>{showImport ? "Close version import" : "Publish new version"}</ActionButton>
      </header>

      {showImport ? <DatasetImportPanel maxUploadBytes={maxUploadBytes} datasetId={dataset.id} initialName={dataset.name} initialDescription={dataset.description} initialSourceUri={dataset.source_uri ?? ""} initialLicense={dataset.license ?? ""} initialAuthor={dataset.author ?? ""} onImported={() => { setShowImport(false); void loadDataset(); }} /> : null}

      <section className="dataset-provenance" aria-label="Dataset identity">
        <dl>
          <div><dt>Dataset ID</dt><dd><code>{dataset.id}</code></dd></div>
          <div><dt>Author</dt><dd>{dataset.author || "Not specified"}</dd></div>
          <div><dt>License</dt><dd>{dataset.license || "Not specified"}</dd></div>
          <div><dt>Source</dt><dd>{dataset.source_uri ? <a href={dataset.source_uri} target="_blank" rel="noreferrer">Open source</a> : "Not specified"}</dd></div>
        </dl>
      </section>

      <section className="dataset-version-bar" aria-label="Dataset version selection">
        <label>Version<Select value={version.id} onChange={(event) => setVersion(dataset.versions.find((candidate) => candidate.id === event.target.value) ?? version)}>{dataset.versions.map((candidate) => <option key={candidate.id} value={candidate.id}>v{candidate.version}, {candidate.item_count.toLocaleString()} items</option>)}</Select></label>
        <span className="dataset-status"><CheckCircle2 size={13} /> {version.status}</span>
        <span>{version.source_filename}</span>
        <a className="button button--secondary" href={`/api/datasets/${encodeURIComponent(dataset.id)}/versions/${encodeURIComponent(version.id)}/source`}><Download size={13} /> Source file</a>
        <button type="button" className="dataset-checksum" onClick={() => void navigator.clipboard.writeText(version.checksum).then(() => toast({ type: "success", message: "Dataset checksum copied." }))}><Copy size={13} /> <code>{version.checksum.slice(0, 16)}</code></button>
      </section>

      <section className="dataset-distribution" aria-label="Latest dataset distribution">
        <div><h3>Subjects</h3><ul>{Object.entries(dataset.subjects).map(([name, count]) => <li key={name}><span>{name}</span><strong>{count}</strong></li>)}</ul></div>
        <div><h3>Splits</h3><ul>{Object.entries(dataset.splits).map(([name, count]) => <li key={name}><span>{name}</span><strong>{count}</strong></li>)}</ul></div>
      </section>

      <section className="dataset-items" aria-labelledby="dataset-items-title">
        <header>
          <div><h3 id="dataset-items-title">Dataset items</h3><span>Showing {items.length} of {total}</span></div>
          <label className="dataset-search"><span className="sr-only">Search dataset items</span><Search size={15} /><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="ID, input, expected output, or subject" /></label>
        </header>
        {failure ? <ResourceState kind={failure.kind} title="Dataset items unavailable" description="Mission Control cannot load this item page." detail={failure.detail} onRetry={() => loadItems(version, query)} compact /> : null}
        {itemsLoading && !items.length ? <Skeleton variant="list" lines={6} /> : null}
        {!itemsLoading && !failure && !items.length ? <ResourceState kind="empty" title="No matching dataset items" description="Change the search text or inspect another version." compact /> : null}
        {items.length ? <div className="dataset-items-table-wrap"><table className="dataset-items-table"><caption>Canonical items from dataset version {version.version}</caption><thead><tr><th>Item ID</th><th>Input</th><th>Expected output</th><th>Subject</th><th>Split</th><th>Tags</th></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td><code>{item.item_key}</code></td><td>{item.input}</td><td>{item.expected_output}</td><td>{item.subject || "Not set"}</td><td>{item.split || "Not set"}</td><td>{item.tags.join(", ") || "None"}</td></tr>)}</tbody></table></div> : null}
      </section>
    </div>
  );
}
