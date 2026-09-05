/**
 * Dataset version records, score sandbox evidence, attempt evidence
 * sections with their redaction classes, and replay bundle manifests.
 *
 * A dataset version record pins the content and policy digests, the
 * split manifest, the asset digests, and the source lineage. A score
 * record names the boundary it ran in with the runtime digest, the
 * terminal class, and the fuel it used. An evidence record lists its
 * section digests and a redaction report that names every removed
 * path with its data class and the detector that fired, so the viewer
 * shows the class and the policy version instead of a bare marker.
 */
import {
  statusWords,
  type BundleManifest,
  type DatasetVersionRecord,
  type EvidenceRecord,
  type RedactionReport,
  type ReplayImport,
  type SandboxBoundary,
  type ScoreRecord,
} from "@/lib/evaluation-operations";

export interface LabelledValue {
  label: string;
  value: string;
  kind: "digest" | "text";
}

const DIGEST_FIELDS: Array<[keyof DatasetVersionRecord, string]> = [
  ["content_digest", "Content digest"],
  ["policy_digest", "Policy digest"],
  ["case_manifest_digest", "Case manifest digest"],
  ["transformation_recipe_digest", "Transformation recipe digest"],
  ["validation_report_digest", "Validation report digest"],
  ["contamination_record_digest", "Contamination record digest"],
  ["attribution_bundle_digest", "Attribution bundle digest"],
];

/** The identity rows of one dataset version record. */
export function datasetVersionRows(record: DatasetVersionRecord): LabelledValue[] {
  const rows: LabelledValue[] = [
    { label: "Version", value: record.version_id, kind: "text" },
    { label: "Parent version", value: record.parent_version_id ?? "None (first version)", kind: "text" },
    { label: "Canonical schema", value: record.canonical_schema_version, kind: "text" },
  ];
  for (const [field, label] of DIGEST_FIELDS) {
    const value = record[field];
    rows.push({ label, value: typeof value === "string" ? value : "", kind: "digest" });
  }
  return rows;
}

export interface SplitRow {
  split: string;
  count: number;
  sample: string[];
}

export function splitRows(record: Pick<DatasetVersionRecord, "split_manifest">): SplitRow[] {
  return Object.entries(record.split_manifest ?? {})
    .map(([split, ids]) => ({ split, count: ids.length, sample: ids.slice(0, 3) }))
    .sort((left, right) => left.split.localeCompare(right.split));
}

export interface LineageSummary {
  sources: string[];
  restrictions: string[];
  trust: string[];
  assetCount: number;
}

export function lineageSummary(record: DatasetVersionRecord): LineageSummary {
  return {
    sources: record.source_lineage ?? [],
    restrictions: (record.effective_restrictions ?? []).map((entry) => `${statusWords(entry.name)} (${entry.behavior})`),
    trust: (record.trust_inputs ?? []).map((entry) => `${statusWords(entry.level ?? "unknown")} policy ${entry.policy_version ?? "?"}`),
    assetCount: (record.asset_digests ?? []).length,
  };
}

// ── Score sandbox evidence ────────────────────────────────────────────

const BOUNDARY_LABELS: Record<SandboxBoundary, string> = {
  trusted_service: "Trusted service",
  wasi_component: "WASI component",
  native_microvm: "Native microVM",
};

export function boundaryLabel(boundary: string | undefined): string {
  if (!boundary) return "Unrecorded";
  return BOUNDARY_LABELS[boundary as SandboxBoundary] ?? statusWords(boundary);
}

export type Tone = "passed" | "failed" | "indeterminate";

/** The tone of one score's terminal class. */
export function terminalTone(record: Pick<ScoreRecord, "status" | "sandbox">): Tone {
  const terminal = record.sandbox?.terminal_class;
  if (record.status === "scored" && (!terminal || terminal === "completed")) return "passed";
  if (record.status === "excluded") return "indeterminate";
  return "failed";
}

/** The sandbox rows one score record shows beside its value. */
export function sandboxRows(record: ScoreRecord): LabelledValue[] {
  const sandbox = record.sandbox;
  if (!sandbox) return [{ label: "Boundary", value: "Unrecorded (legacy score)", kind: "text" }];
  const rows: LabelledValue[] = [
    { label: "Boundary", value: boundaryLabel(sandbox.boundary), kind: "text" },
    { label: "Terminal class", value: statusWords(sandbox.terminal_class ?? (record.status === "scored" ? "completed" : record.status)), kind: "text" },
    { label: "Runtime digest", value: sandbox.runtime_digest, kind: "digest" },
    { label: "Policy digest", value: sandbox.policy_digest, kind: "digest" },
  ];
  if (sandbox.component_digest) rows.push({ label: "Component digest", value: sandbox.component_digest, kind: "digest" });
  if (sandbox.wit_digest) rows.push({ label: "WIT digest", value: sandbox.wit_digest, kind: "digest" });
  if (typeof sandbox.fuel_used === "number") rows.push({ label: "Fuel used", value: sandbox.fuel_used.toLocaleString(), kind: "text" });
  rows.push({ label: "Replay eligible", value: sandbox.replay_eligible === undefined ? "Unrecorded" : sandbox.replay_eligible ? "Yes" : "No", kind: "text" });
  if (record.error) rows.push({ label: "Trap or limit", value: record.error, kind: "text" });
  return rows;
}

// ── Attempt evidence sections and redactions ──────────────────────────

export interface EvidenceSectionLink {
  section: string;
  label: string;
  digest: string;
}

/** Every persisted section of one evidence record with its digest. */
export function evidenceSections(record: EvidenceRecord): EvidenceSectionLink[] {
  const sections: EvidenceSectionLink[] = [];
  const named: Array<[string, string, string | null | undefined]> = [
    ["trace", "Trace", record.trace_digest],
    ["final_output", "Final output", record.final_output_digest],
    ["final_state", "Final state", record.final_state_digest],
    ["tool_calls", "Tool calls", record.tool_calls_digest],
    ["verification", "Verification decisions", record.verification_decisions_digest],
  ];
  for (const [section, label, digest] of named) {
    if (digest) sections.push({ section, label, digest });
  }
  for (const digest of record.artifacts ?? []) sections.push({ section: "artifact", label: `Artifact ${digest.slice(0, 12)}`, digest });
  return sections;
}

export type DataClass = "secret" | "sensitive" | "prohibited";

export interface RedactedPath {
  path: string;
  dataClass: DataClass;
  detector: string | null;
}

function sectionPrefix(path: string, section: string): boolean {
  return path === section || path.startsWith(`${section}.`) || path.startsWith(`${section}[`);
}

/** The redacted paths inside one section, or every path when section is null. */
export function redactedPaths(report: RedactionReport | null | undefined, section: string | null = null): RedactedPath[] {
  if (!report) return [];
  const rows: RedactedPath[] = [];
  for (const dataClass of ["prohibited", "secret", "sensitive"] as const) {
    for (const path of report[dataClass] ?? []) {
      if (section !== null && !sectionPrefix(path, section)) continue;
      rows.push({ path, dataClass, detector: report.detectors?.[path] ?? null });
    }
  }
  return rows.sort((left, right) => left.path.localeCompare(right.path));
}

export interface RedactionCounts {
  secret: number;
  sensitive: number;
  prohibited: number;
  total: number;
  policyDigest: string | null;
}

export function redactionCounts(report: RedactionReport | null | undefined): RedactionCounts {
  const secret = report?.secret?.length ?? 0;
  const sensitive = report?.sensitive?.length ?? 0;
  const prohibited = report?.prohibited?.length ?? 0;
  return { secret, sensitive, prohibited, total: secret + sensitive + prohibited, policyDigest: report?.policy_digest ?? null };
}

export const DATA_CLASS_LABELS: Record<DataClass, string> = {
  secret: "Secret: value removed, marker kept",
  sensitive: "Sensitive: value removed from the public view",
  prohibited: "Prohibited: field dropped before persistence",
};

// ── Replay bundles ────────────────────────────────────────────────────

export interface MemberClassRow {
  memberClass: string;
  label: string;
  count: number;
  bytes: number;
}

/** Manifest members grouped by class, largest group first. */
export function memberRows(manifest: Pick<BundleManifest, "members"> | null | undefined): MemberClassRow[] {
  const groups = new Map<string, MemberClassRow>();
  for (const member of manifest?.members ?? []) {
    const row = groups.get(member.class) ?? { memberClass: member.class, label: statusWords(member.class), count: 0, bytes: 0 };
    row.count += 1;
    row.bytes += member.size_bytes;
    groups.set(member.class, row);
  }
  return [...groups.values()].sort((left, right) => right.count - left.count || left.memberClass.localeCompare(right.memberClass));
}

export function bytesText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
}

export interface ImportSummary {
  title: string;
  tone: Tone;
  rows: LabelledValue[];
}

/** An execution repeat never starts from an import; the row says what one needs. */
export function executionRepeatText(value: ReplayImport["execution_repeat"]): string {
  if (typeof value === "string") return statusWords(value);
  if (!value || typeof value !== "object") return "Unavailable";
  const requires = (value.requires ?? []).map((entry) => statusWords(entry).toLowerCase());
  return `${value.started ? "Started" : "Not started"}${requires.length ? `; requires ${requires.join(", ")}` : ""}`;
}

/** What one import produced: its state, its quarantine, and its replay claim. */
export function importSummary(result: ReplayImport): ImportSummary {
  const rows: LabelledValue[] = [
    { label: "Import id", value: result.import_id, kind: "digest" },
    { label: "Ingestion state", value: statusWords(result.ingestion_state), kind: "text" },
    { label: "Readable members", value: String(result.readable_members.length), kind: "text" },
    { label: "Quarantined members", value: result.quarantined_members.length ? result.quarantined_members.join(", ") : "None", kind: "text" },
    { label: "Stripped fields", value: result.stripped_fields.length ? result.stripped_fields.join(", ") : "None", kind: "text" },
    { label: "Replay approval", value: result.replay_approved ? "Approved" : "Not approved (read only)", kind: "text" },
    { label: "Execution repeat", value: executionRepeatText(result.execution_repeat), kind: "text" },
  ];
  if (result.replay) {
    rows.push({ label: "Analysis claim", value: statusWords(result.replay.claim), kind: "text" });
    rows.push({ label: "Results digest", value: result.replay.results_digest, kind: "digest" });
    rows.push({ label: "Expected digest", value: result.replay.expected_results_digest, kind: "digest" });
  }
  const replayable = result.replay?.analysis_replayable;
  return {
    title: result.replay ? (replayable ? "Replay verified" : "Replay mismatch") : "Imported without replay approval",
    tone: result.replay ? (replayable ? "passed" : "failed") : "indeterminate",
    rows,
  };
}

/** Decode a base64 archive into bytes for a browser download. */
export function archiveBytes(base64: string): Uint8Array {
  const binary = typeof atob === "function" ? atob(base64) : Buffer.from(base64, "base64").toString("binary");
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

/** Encode bytes into base64 for the import request. */
export function base64FromBytes(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return typeof btoa === "function" ? btoa(binary) : Buffer.from(binary, "binary").toString("base64");
}
