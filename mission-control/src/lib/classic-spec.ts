/** Both authoring surfaces use this route and the same server-owned compiler. */
export const CLASSIC_PREVIEW_CHOICE = "classic-native-preview";
export const CLASSIC_SPEC_ROUTE = "/api/classic/spec";
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export interface FieldSchema {
  type?: string; enum?: JsonValue[]; anyOf?: FieldSchema[]; default?: JsonValue;
  minimum?: number; maximum?: number; exclusiveMinimum?: number; pattern?: string;
}
export interface ClassicControl {
  path: string; label: string; beginner: boolean; schema: FieldSchema; cap: JsonValue[] | null;
}
export interface ClassicInput {
  fidelity: string; effort: string;
  task_overrides: { classic: Record<string, JsonValue>; seed?: number | null; routing?: Record<string, string>; role_registry?: Record<string, JsonValue> };
  asset_manifest_digest?: string | null;
}
export interface ClassicSchema {
  properties: { fidelity: { enum: string[] }; effort: { enum: string[] } };
  "x-editor": {
    defaults: ClassicInput; availability: "test_only";
    groups: Array<{ name: string; description: string; controls: ClassicControl[] }>;
    fidelity_profiles: Array<{ profile_id: string; description: string; fixed_fields: string[] }>;
    effort_profiles: Array<{ profile_id: string; description: string }>;
    caps: Record<string, JsonValue[]>;
  };
}
export interface FieldError { field: string; message: string }
export interface ClassicPreview {
  specification_digest: string;
  specification: Record<string, JsonValue>;
  differences: Record<string, Array<{ field: string; profile: JsonValue; effective: JsonValue; source: string }>>;
  caps: Record<string, JsonValue[]>;
  provider_limits: Record<string, JsonValue>;
  estimate_assumptions: string[];
  endpoint_notice: string;
  availability: "test_only";
  admissible: false;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
export function parseClassicSchema(value: unknown): ClassicSchema {
  if (!isRecord(value) || !isRecord(value.properties) || !isRecord(value["x-editor"])) throw new Error("The Classic input schema is unavailable.");
  const editor = value["x-editor"];
  const defaults = editor.defaults;
  if (!isRecord(defaults) || typeof defaults.fidelity !== "string" || typeof defaults.effort !== "string"
    || !isRecord(defaults.task_overrides) || !isRecord(defaults.task_overrides.classic)) throw new Error("The Classic schema defaults are incomplete.");
  if (!Array.isArray(editor.groups) || editor.groups.length !== 6 || !isRecord(editor.defaults) || editor.availability !== "test_only"
    || !Array.isArray(editor.fidelity_profiles) || editor.fidelity_profiles.some((profile) => !isRecord(profile) || typeof profile.profile_id !== "string" || !Array.isArray(profile.fixed_fields))
    || !Array.isArray(editor.effort_profiles)
    || !isRecord(value.properties.fidelity) || !Array.isArray(value.properties.fidelity.enum)
    || !isRecord(value.properties.effort) || !Array.isArray(value.properties.effort.enum)
    || editor.groups.some((group) => !isRecord(group) || typeof group.name !== "string" || !Array.isArray(group.controls)
      || group.controls.some((control) => !isRecord(control) || typeof control.path !== "string" || typeof control.label !== "string" || !isRecord(control.schema) || !(control.cap === null || Array.isArray(control.cap))))) {
    throw new Error("The Classic input schema is incomplete.");
  }
  return value as unknown as ClassicSchema;
}
export function parseClassicPreview(value: unknown): ClassicPreview {
  if (!isRecord(value) || typeof value.specification_digest !== "string" || !isRecord(value.specification)
    || !isRecord(value.differences) || !isRecord(value.caps) || !isRecord(value.provider_limits)
    || !Array.isArray(value.estimate_assumptions) || typeof value.endpoint_notice !== "string"
    || value.availability !== "test_only" || value.admissible !== false
    || !isRecord(value.specification.resolution) || !isRecord(value.specification.estimate)
    || !isRecord(value.specification.deployment_caps) || !Array.isArray(value.specification.warnings)) {
    throw new Error("The Classic preview is incomplete. Retry the preview.");
  }
  const differences = value.differences;
  const specification = value.specification;
  if (!["fidelity", "effort"].every((layer) => Array.isArray(differences[layer]))
    || Object.values(value.caps).some((bounds) => !Array.isArray(bounds) || bounds.length !== 2)
    || value.estimate_assumptions.some((note) => typeof note !== "string")
    || !["runtime", "fidelity", "effort", "inputs", "team", "models", "provider_capabilities", "routing", "model_lineage", "prompts", "prices", "randomness", "coordination", "board", "cleaner", "memory", "verification", "consensus", "limits", "recovery", "termination"].every((section) => isRecord(specification[section]))
    || !Array.isArray(value.specification.deployment_caps.adjustments)) {
    throw new Error("The Classic preview omits required disclosures. Retry the preview.");
  }
  return value as unknown as ClassicPreview;
}
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "Not specified";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
export function effectiveRows(value: JsonValue, prefix = ""): Array<[string, JsonValue]> {
  if (isRecord(value) && Object.keys(value).length) {
    return Object.entries(value).flatMap(([key, item]) => effectiveRows(item as JsonValue, prefix ? `${prefix}.${key}` : key));
  }
  if (Array.isArray(value) && value.length) {
    return value.flatMap((item, index) => effectiveRows(item, `${prefix} / ${index + 1}`));
  }
  return [[prefix, value]];
}
