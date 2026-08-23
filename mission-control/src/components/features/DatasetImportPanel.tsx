"use client";

import { useId, useMemo, useRef, useState } from "react";
import { CheckCircle2, Database, FileUp, ShieldCheck, XCircle } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ActionableError } from "@/components/ui/ActionableError";
import { useToast } from "@/hooks/useToast";
import {
  formatDatasetBytes,
  inferDatasetColumns,
  suggestDatasetMapping,
  type DatasetField,
  type DatasetMapping,
  type DatasetValidation,
} from "@/lib/datasets";
import { Select } from "@/components/ui/Select";

const EMPTY_MAPPING: DatasetMapping = {
  id: "",
  input: "",
  expected_output: "",
  subject: "",
  split: "",
  tags: "",
};

const MAPPING_LABELS: Array<{ field: DatasetField; label: string; required: boolean }> = [
  { field: "input", label: "Input", required: true },
  { field: "expected_output", label: "Expected output", required: true },
  { field: "id", label: "Item ID", required: false },
  { field: "subject", label: "Subject", required: false },
  { field: "split", label: "Split", required: false },
  { field: "tags", label: "Tags", required: false },
];

interface DatasetImportPanelProps {
  maxUploadBytes: number;
  datasetId?: string;
  initialName?: string;
  initialDescription?: string;
  initialSourceUri?: string;
  initialLicense?: string;
  initialAuthor?: string;
  onImported: (datasetId: string) => void;
}

async function responseError(response: Response): Promise<string> {
  const body = await response.json().catch(() => ({})) as { error?: string; detail?: string };
  return body.detail || body.error || `Request returned HTTP ${response.status}`;
}

export function DatasetImportPanel({
  maxUploadBytes,
  datasetId,
  initialName = "",
  initialDescription = "",
  initialSourceUri = "",
  initialLicense = "",
  initialAuthor = "",
  onImported,
}: DatasetImportPanelProps) {
  const inputId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [mapping, setMapping] = useState<DatasetMapping>(EMPTY_MAPPING);
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(initialDescription);
  const [sourceUri, setSourceUri] = useState(initialSourceUri);
  const [licenseName, setLicenseName] = useState(initialLicense);
  const [author, setAuthor] = useState(initialAuthor);
  const [validation, setValidation] = useState<DatasetValidation | null>(null);
  const [busy, setBusy] = useState<"validate" | "import" | null>(null);
  const [error, setError] = useState("");
  const { toast } = useToast();

  const canValidate = Boolean(
    file && mapping.input && mapping.expected_output && !busy,
  );
  const canImport = Boolean(validation?.valid && name.trim() && !busy);
  const mappingJson = useMemo(() => JSON.stringify(mapping), [mapping]);

  const selectFile = async (nextFile: File | null) => {
    setFile(nextFile);
    setValidation(null);
    setError("");
    if (!nextFile) {
      setColumns([]);
      setMapping(EMPTY_MAPPING);
      return;
    }
    if (nextFile.size > maxUploadBytes) {
      setError(`The file exceeds the ${formatDatasetBytes(maxUploadBytes)} limit.`);
      return;
    }
    const text = await nextFile.slice(0, 128 * 1024).text();
    const inferred = inferDatasetColumns(nextFile.name, text);
    setColumns(inferred);
    setMapping(suggestDatasetMapping(inferred));
    if (!name.trim() && !datasetId) {
      setName(nextFile.name.replace(/\.(csv|jsonl)$/i, "").replace(/[-_]+/g, " "));
    }
  };

  const createFormData = () => {
    const body = new FormData();
    if (file) body.set("file", file);
    body.set("mapping", mappingJson);
    return body;
  };

  const validate = async () => {
    if (!canValidate) return;
    setBusy("validate");
    setError("");
    try {
      const response = await fetch("/api/datasets/validate", {
        method: "POST",
        body: createFormData(),
        signal: AbortSignal.timeout(60_000),
      });
      const body = await response.json() as DatasetValidation;
      if (!response.ok && !body.issues) throw new Error(body.detail || body.error || "Validation failed");
      setValidation(body);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Dataset validation failed");
    } finally {
      setBusy(null);
    }
  };

  const publish = async () => {
    if (!canImport) return;
    setBusy("import");
    setError("");
    try {
      const body = createFormData();
      body.set("name", name.trim());
      body.set("description", description.trim());
      body.set("source_uri", sourceUri.trim());
      body.set("license", licenseName.trim());
      body.set("author", author.trim());
      if (datasetId) body.set("dataset_id", datasetId);
      const response = await fetch("/api/datasets/import", {
        method: "POST",
        body,
        signal: AbortSignal.timeout(120_000),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = await response.json() as { dataset?: { id?: string }; item_count?: number };
      const importedId = result.dataset?.id || datasetId;
      if (!importedId) throw new Error("The import response has no dataset identifier");
      toast({ type: "success", message: `Published ${result.item_count ?? validation?.row_count ?? 0} dataset items.` });
      onImported(importedId);
      setFile(null);
      setColumns([]);
      setMapping(EMPTY_MAPPING);
      setValidation(null);
      if (!datasetId) setName("");
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Dataset import failed");
    } finally {
      setBusy(null);
    }
  };

  const setFieldMapping = (field: DatasetField, value: string) => {
    setMapping((current) => ({ ...current, [field]: value }));
    setValidation(null);
  };

  return (
    <section className="dataset-import" aria-labelledby={`${inputId}-title`}>
      <header className="dataset-import__header">
        <div>
          <p className="page-eyebrow">{datasetId ? "New immutable version" : "New dataset"}</p>
          <h3 id={`${inputId}-title`}>{datasetId ? "Publish a dataset version" : "Import benchmark data"}</h3>
          <p>Validate the source fields before Mission Control publishes an immutable version.</p>
        </div>
        <ShieldCheck size={24} aria-hidden="true" />
      </header>

      <div
        className="dataset-dropzone"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          void selectFile(event.dataTransfer.files[0] ?? null);
        }}
      >
        <FileUp size={24} aria-hidden="true" />
        <div>
          <strong>{file ? file.name : "Choose or drop a CSV or JSONL file"}</strong>
          <span>{file ? formatDatasetBytes(file.size) : `UTF-8 text, up to ${formatDatasetBytes(maxUploadBytes)}`}</span>
        </div>
        <label className="button button--secondary" htmlFor={`${inputId}-file`}>Choose file</label>
        <input
          ref={fileInputRef}
          id={`${inputId}-file`}
          className="sr-only"
          type="file"
          accept=".csv,.jsonl,text/csv,application/x-ndjson"
          onChange={(event) => void selectFile(event.target.files?.[0] ?? null)}
        />
      </div>

      {error ? <ActionableError component="Dataset import" cause={error} compact /> : null}

      {file ? (
        <>
          <div className="dataset-import__metadata">
            <label>Dataset name <span>Required</span><input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} /></label>
            <label>Description <span>Optional</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={2} maxLength={4000} /></label>
            <label>Source URL <span>Optional</span><input type="url" value={sourceUri} onChange={(event) => setSourceUri(event.target.value)} /></label>
            <label>License <span>Optional</span><input value={licenseName} onChange={(event) => setLicenseName(event.target.value)} /></label>
            <label>Author <span>Optional</span><input value={author} onChange={(event) => setAuthor(event.target.value)} /></label>
          </div>

          <fieldset className="dataset-mapping">
            <legend>Field mapping</legend>
            <p>Map the source columns to the stable benchmark item contract.</p>
            <div className="dataset-mapping__grid">
              {MAPPING_LABELS.map(({ field, label, required }) => (
                <label key={field}>
                  {label} <span>{required ? "Required" : "Optional"}</span>
                  <Select value={mapping[field]} onChange={(event) => setFieldMapping(field, event.target.value)}>
                    <option value="">{field === "id" ? "Generate IDs" : "Not mapped"}</option>
                    {columns.map((column) => <option key={column} value={column}>{column}</option>)}
                  </Select>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="dataset-import__actions">
            <ActionButton variant="secondary" onClick={() => void validate()} loading={busy === "validate"} disabled={!canValidate}>
              <Database size={15} /> Validate dataset
            </ActionButton>
            <ActionButton onClick={() => void publish()} loading={busy === "import"} disabled={!canImport}>
              <ShieldCheck size={15} /> Publish immutable version
            </ActionButton>
          </div>
        </>
      ) : null}

      {validation ? (
        <div className={`dataset-validation dataset-validation--${validation.valid ? "valid" : "invalid"}`}>
          <header>
            {validation.valid ? <CheckCircle2 size={18} /> : <XCircle size={18} />}
            <div>
              <strong>{validation.valid ? "Validation passed" : "Validation needs attention"}</strong>
              <span>{validation.row_count.toLocaleString()} rows, {validation.format.toUpperCase()}</span>
            </div>
          </header>
          {validation.issues.length ? (
            <ul className="dataset-validation__issues">
              {validation.issues.map((issue, index) => (
                <li key={`${issue.row}-${issue.field}-${index}`}>
                  Row {issue.row}, {issue.field}: {issue.message}
                </li>
              ))}
            </ul>
          ) : null}
          {validation.preview.length ? (
            <div className="dataset-preview-table-wrap">
              <table className="dataset-preview-table">
                <caption>First {validation.preview.length} canonical dataset items</caption>
                <thead><tr><th>Item ID</th><th>Input</th><th>Expected output</th><th>Subject</th><th>Split</th></tr></thead>
                <tbody>{validation.preview.map((item, index) => (
                  <tr key={String(item.item_key ?? index)}>
                    <td>{String(item.item_key ?? "")}</td>
                    <td>{String(item.input ?? "")}</td>
                    <td>{String(item.expected_output ?? "")}</td>
                    <td>{String(item.subject ?? "Not set")}</td>
                    <td>{String(item.split ?? "Not set")}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
