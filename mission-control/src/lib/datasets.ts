export interface DatasetVersion {
  id: string;
  dataset_id: string;
  version: number;
  status: "draft" | "published";
  checksum: string;
  item_count: number;
  schema_json: {
    version?: string;
    source_format?: string;
    mapping?: Record<string, string>;
    columns?: string[];
  };
  source_filename: string | null;
  source_mime: string | null;
  source_checksum: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  published_at: string | null;
  // Each immutable version carries its own field distributions, so a
  // consumer reads the admitted revision's distribution, never the
  // latest upload's.
  subjects?: Record<string, number>;
  splits?: Record<string, number>;
}

export interface DatasetSummary {
  id: string;
  name: string;
  description: string;
  source_uri: string | null;
  license: string | null;
  author: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  latest_version_id: string | null;
  latest_version: number | null;
  latest_status: "draft" | "published" | null;
  latest_checksum: string | null;
  item_count: number | null;
  latest_published_at: string | null;
  version_count: number;
}

export interface DatasetDetail extends Omit<DatasetSummary, "latest_version_id" | "latest_version" | "latest_status" | "latest_checksum" | "item_count" | "latest_published_at" | "version_count"> {
  versions: DatasetVersion[];
  subjects: Record<string, number>;
  splits: Record<string, number>;
  distribution_version_id?: string | null;
}

export interface DatasetItem {
  id: string;
  dataset_version_id: string;
  item_key: string;
  input: string;
  expected_output: string;
  subject: string | null;
  split: string | null;
  tags: string[];
  metadata: Record<string, unknown>;
  sort_order: number;
}

export interface DatasetIssue {
  row: number;
  field: string;
  message: string;
}

export interface DatasetValidation {
  valid: boolean;
  format: "csv" | "jsonl" | "unknown";
  columns: string[];
  row_count: number;
  checksum: string | null;
  preview: Array<Record<string, unknown>>;
  issues: DatasetIssue[];
  filename?: string;
  bytes?: number;
  max_upload_bytes?: number;
  error?: string;
  detail?: string;
}

export type DatasetField = "id" | "input" | "expected_output" | "subject" | "split" | "tags";
export type DatasetMapping = Record<DatasetField, string>;

const FIELD_ALIASES: Record<DatasetField, string[]> = {
  id: ["id", "item_id", "key", "question_id"],
  input: ["input", "question", "prompt", "objective", "text"],
  expected_output: ["expected_output", "answer", "expected", "target", "label", "ground_truth"],
  subject: ["subject", "category", "domain", "topic"],
  split: ["split", "partition", "subset"],
  tags: ["tags", "labels"],
};

export function suggestDatasetMapping(columns: readonly string[]): DatasetMapping {
  const normalized = new Map(columns.map((column) => [column.toLowerCase().trim(), column]));
  const selected = (field: DatasetField) => {
    for (const alias of FIELD_ALIASES[field]) {
      const match = normalized.get(alias);
      if (match) return match;
    }
    return "";
  };
  return {
    id: selected("id"),
    input: selected("input"),
    expected_output: selected("expected_output"),
    subject: selected("subject"),
    split: selected("split"),
    tags: selected("tags"),
  };
}

function parseCsvHeader(line: string): string[] {
  const columns: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"') {
      if (quoted && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      columns.push(current.trim());
      current = "";
    } else {
      current += character;
    }
  }
  columns.push(current.trim());
  return columns.filter(Boolean);
}

export function inferDatasetColumns(filename: string, text: string): string[] {
  if (filename.toLowerCase().endsWith(".jsonl")) {
    const firstLine = text.split(/\r?\n/).find((line) => line.trim());
    if (!firstLine) return [];
    try {
      const value = JSON.parse(firstLine) as unknown;
      return typeof value === "object" && value !== null && !Array.isArray(value)
        ? Object.keys(value as Record<string, unknown>).sort()
        : [];
    } catch {
      return [];
    }
  }
  const firstLine = text.split(/\r?\n/, 1)[0] ?? "";
  return parseCsvHeader(firstLine.replace(/^\uFEFF/, ""));
}

export function formatDatasetBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
