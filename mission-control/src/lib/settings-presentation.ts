/**
 * Settings model — the daemon's session-override snapshot, the draft the
 * operator edits, the diff between them, and the YAML patch that makes a
 * session override permanent in bmas.yaml.
 */

export interface SettingsRegistryEntry {
  preferred_host: string | null;
  profile: string;
  dispatch_port: number;
  enabled?: boolean;
  endpoints?: string[];
}

export type ClassicSettings = Record<string, unknown>;

export interface SettingsSnapshot {
  routing: Record<string, string>;
  role_registry: Record<string, SettingsRegistryEntry>;
  classic?: ClassicSettings;
  defaults: {
    routing: Record<string, string>;
    role_registry: Record<string, SettingsRegistryEntry>;
    classic?: ClassicSettings;
  };
}

export interface SettingsDraft {
  routing: Record<string, string>;
  role_registry: Record<string, SettingsRegistryEntry>;
  classic: ClassicSettings;
}

export interface ClassicFieldMeta {
  key: string;
  label: string;
  type: "integer" | "number" | "boolean" | "enum" | "tier_map" | "weight_map";
  group: "limits" | "roster" | "control" | "board";
  description: string;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  options?: string[];
}

export interface SettingsPresentationChange {
  label: string;
  before: string;
  after: string;
}

export interface SettingsChange extends SettingsPresentationChange {
  section: "routing" | "role_registry" | "classic";
  key: string;
}

export interface SettingsSavePayload {
  routing: Record<string, string> | null;
  role_registry: Record<string, Partial<SettingsRegistryEntry>> | null;
  classic: ClassicSettings | null;
}

const ROLE_FIELDS = ["preferred_host", "profile", "dispatch_port", "enabled"] as const;

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "Any";
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, entry]) => `${key} ${String(entry)}`)
      .join(", ");
  }
  return String(value);
}

export function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

export function draftFromSnapshot(snapshot: SettingsSnapshot): SettingsDraft {
  return {
    routing: { ...snapshot.routing },
    role_registry: Object.fromEntries(
      Object.entries(snapshot.role_registry).map(([role, entry]) => [role, { ...entry }]),
    ),
    classic: JSON.parse(JSON.stringify(snapshot.classic ?? {})) as ClassicSettings,
  };
}

function humanKey(key: string): string {
  return key.replace(/_/g, " ").replace(/^\w/, (letter) => letter.toUpperCase());
}

/** Differences between the editable draft and the saved snapshot. */
export function diffDraft(
  snapshot: SettingsSnapshot,
  draft: SettingsDraft,
  classicFields: readonly ClassicFieldMeta[] = [],
): SettingsChange[] {
  const changes: SettingsChange[] = [];
  for (const [tier, model] of Object.entries(draft.routing)) {
    if (snapshot.routing[tier] !== model) {
      changes.push({
        section: "routing",
        key: tier,
        label: `${humanKey(tier)} tier model`,
        before: snapshot.routing[tier] ?? "Not set",
        after: model,
      });
    }
  }
  for (const [role, entry] of Object.entries(draft.role_registry)) {
    const saved = snapshot.role_registry[role];
    for (const field of ROLE_FIELDS) {
      const before = saved?.[field];
      const after = entry[field];
      if (!sameValue(before ?? (field === "enabled" ? true : null), after ?? (field === "enabled" ? true : null))) {
        changes.push({
          section: "role_registry",
          key: `${role}.${field}`,
          label: `${humanKey(role)} ${humanKey(field).toLowerCase()}`,
          before: formatValue(before ?? (field === "enabled" ? true : null)),
          after: formatValue(after ?? (field === "enabled" ? true : null)),
        });
      }
    }
  }
  const labels = new Map(classicFields.map((field) => [field.key, field.label]));
  for (const [key, value] of Object.entries(draft.classic)) {
    const before = snapshot.classic?.[key];
    if (!sameValue(before, value)) {
      changes.push({
        section: "classic",
        key,
        label: labels.get(key) ?? humanKey(key),
        before: formatValue(before),
        after: formatValue(value),
      });
    }
  }
  return changes;
}

/** Build the PATCH bodies for every section that changed. */
export function buildSavePayload(snapshot: SettingsSnapshot, draft: SettingsDraft): SettingsSavePayload {
  const routing: Record<string, string> = {};
  for (const [tier, model] of Object.entries(draft.routing)) {
    if (snapshot.routing[tier] !== model) routing[tier] = model;
  }
  const roles: Record<string, Partial<SettingsRegistryEntry>> = {};
  for (const [role, entry] of Object.entries(draft.role_registry)) {
    const saved = snapshot.role_registry[role];
    const patch: Partial<SettingsRegistryEntry> = {};
    for (const field of ROLE_FIELDS) {
      const before = saved?.[field] ?? (field === "enabled" ? true : null);
      const after = entry[field] ?? (field === "enabled" ? true : null);
      if (!sameValue(before, after)) {
        (patch as Record<string, unknown>)[field] = entry[field] ?? (field === "enabled" ? true : null);
      }
    }
    if (Object.keys(patch).length) roles[role] = patch;
  }
  const classic: ClassicSettings = {};
  for (const [key, value] of Object.entries(draft.classic)) {
    if (!sameValue(snapshot.classic?.[key], value)) classic[key] = value;
  }
  return {
    routing: Object.keys(routing).length ? routing : null,
    role_registry: Object.keys(roles).length ? roles : null,
    classic: Object.keys(classic).length ? classic : null,
  };
}

/** Keys whose saved value differs from bmas.yaml (session overrides). */
export function sessionOverrideKeys(snapshot: SettingsSnapshot): Set<string> {
  const keys = new Set<string>();
  for (const [tier, model] of Object.entries(snapshot.routing)) {
    if (snapshot.defaults.routing[tier] !== model) keys.add(`routing.${tier}`);
  }
  for (const [role, entry] of Object.entries(snapshot.role_registry)) {
    const base = snapshot.defaults.role_registry[role];
    for (const field of ROLE_FIELDS) {
      const before = base?.[field] ?? (field === "enabled" ? true : null);
      const after = entry[field] ?? (field === "enabled" ? true : null);
      if (!sameValue(before, after)) keys.add(`role_registry.${role}.${field}`);
    }
  }
  for (const [key, value] of Object.entries(snapshot.classic ?? {})) {
    if (!sameValue(snapshot.defaults.classic?.[key], value)) keys.add(`classic.${key}`);
  }
  return keys;
}

function yamlValue(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function yamlMapping(indent: string, value: Record<string, unknown>): string[] {
  return Object.entries(value).map(([key, entry]) => `${indent}${key}: ${yamlValue(entry)}`);
}

export function buildYamlPatch(settings: SettingsSnapshot): string {
  const lines = ["# Merge these session overrides into bmas.yaml, then restart bMAS."];
  const routingChanges = Object.entries(settings.routing).filter(
    ([tier, model]) => settings.defaults.routing[tier] !== model,
  );
  const roleChanges = Object.entries(settings.role_registry).filter(([role, entry]) => {
    const defaultEntry = settings.defaults.role_registry[role];
    return (
      !defaultEntry
      || entry.preferred_host !== defaultEntry.preferred_host
      || entry.profile !== defaultEntry.profile
      || entry.dispatch_port !== defaultEntry.dispatch_port
      || (entry.enabled ?? true) !== (defaultEntry.enabled ?? true)
    );
  });
  const classicChanges = Object.entries(settings.classic ?? {}).filter(
    ([key, value]) => !sameValue(settings.defaults.classic?.[key], value),
  );

  if (routingChanges.length > 0) {
    lines.push("routing:");
    for (const [tier, model] of routingChanges) lines.push(`  ${tier}: ${yamlValue(model)}`);
  }

  const coordinationLines: string[] = [];
  const topLevelClassic = classicChanges.filter(([key]) => key === "round_execution" || key === "view_budget_tokens");
  const nestedClassic = classicChanges.filter(([key]) => key !== "round_execution" && key !== "view_budget_tokens");
  for (const [key, value] of topLevelClassic) coordinationLines.push(`  ${key}: ${yamlValue(value)}`);
  if (nestedClassic.length > 0) {
    coordinationLines.push("  classic:");
    for (const [key, value] of nestedClassic) {
      if (value && typeof value === "object") {
        coordinationLines.push(`    ${key}:`, ...yamlMapping("      ", value as Record<string, unknown>));
      } else {
        coordinationLines.push(`    ${key}: ${yamlValue(value)}`);
      }
    }
  }
  if (roleChanges.length > 0) {
    coordinationLines.push("  role_registry:");
    for (const [role, entry] of roleChanges) {
      coordinationLines.push(
        `    ${role}:`,
        `      preferred_host: ${yamlValue(entry.preferred_host)}`,
        `      profile: ${yamlValue(entry.profile)}`,
        `      dispatch_port: ${yamlValue(entry.dispatch_port)}`,
      );
      if ((entry.enabled ?? true) !== (settings.defaults.role_registry[role]?.enabled ?? true)) {
        coordinationLines.push(`      enabled: ${yamlValue(entry.enabled ?? true)}`);
      }
    }
  }
  if (coordinationLines.length > 0) lines.push("coordination:", ...coordinationLines);

  return lines.join("\n");
}

export function getResetChanges(settings: SettingsSnapshot): SettingsPresentationChange[] {
  const changes: SettingsPresentationChange[] = [];

  for (const [tier, model] of Object.entries(settings.routing)) {
    const defaultModel = settings.defaults.routing[tier];
    if (model !== defaultModel) {
      changes.push({
        label: `${tier} routing`,
        before: model,
        after: defaultModel ?? "Not set",
      });
    }
  }

  for (const [role, entry] of Object.entries(settings.role_registry)) {
    const defaultEntry = settings.defaults.role_registry[role];
    if (!defaultEntry) {
      changes.push({
        label: `${role} role`,
        before: `${entry.profile} on ${entry.preferred_host ?? "any host"}:${entry.dispatch_port}`,
        after: "Removed",
      });
      continue;
    }
    if (entry.preferred_host !== defaultEntry.preferred_host) {
      changes.push({
        label: `${role} preferred host`,
        before: entry.preferred_host ?? "Any host",
        after: defaultEntry.preferred_host ?? "Any host",
      });
    }
    if (entry.profile !== defaultEntry.profile) {
      changes.push({ label: `${role} profile`, before: entry.profile, after: defaultEntry.profile });
    }
    if (entry.dispatch_port !== defaultEntry.dispatch_port) {
      changes.push({
        label: `${role} dispatch port`,
        before: String(entry.dispatch_port),
        after: String(defaultEntry.dispatch_port),
      });
    }
    if ((entry.enabled ?? true) !== (defaultEntry.enabled ?? true)) {
      changes.push({
        label: `${role} enabled`,
        before: formatValue(entry.enabled ?? true),
        after: formatValue(defaultEntry.enabled ?? true),
      });
    }
  }

  for (const [key, value] of Object.entries(settings.classic ?? {})) {
    const defaultValue = settings.defaults.classic?.[key];
    if (!sameValue(value, defaultValue)) {
      changes.push({ label: humanKey(key), before: formatValue(value), after: formatValue(defaultValue) });
    }
  }

  return changes;
}

// ── Validation ───────────────────────────────────────────────────────

export interface SettingsValidationIssue {
  section: SettingsChange["section"];
  key: string;
  message: string;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function rangeMessage(field: ClassicFieldMeta): string {
  const unit = field.unit ? ` ${field.unit}` : "";
  if (field.min !== undefined && field.max !== undefined) return `Enter a value from ${field.min} to ${field.max}${unit}.`;
  if (field.min !== undefined) return `Enter a value of at least ${field.min}${unit}.`;
  if (field.max !== undefined) return `Enter a value of at most ${field.max}${unit}.`;
  return "Enter a number.";
}

function checkNumber(value: unknown, field: ClassicFieldMeta): string | null {
  if (!isFiniteNumber(value)) return "Enter a number.";
  if (field.type === "integer" && !Number.isInteger(value)) return "Enter a whole number.";
  if ((field.min !== undefined && value < field.min) || (field.max !== undefined && value > field.max)) {
    return rangeMessage(field);
  }
  return null;
}

/** Every draft value that the daemon would reject, keyed for row display. */
export function validateDraft(draft: SettingsDraft, classicFields: readonly ClassicFieldMeta[]): SettingsValidationIssue[] {
  const issues: SettingsValidationIssue[] = [];
  for (const [tier, model] of Object.entries(draft.routing)) {
    if (!model) issues.push({ section: "routing", key: tier, message: "Select a model." });
  }
  for (const [role, entry] of Object.entries(draft.role_registry)) {
    if (!entry.profile || !entry.profile.trim()) {
      issues.push({ section: "role_registry", key: `${role}.profile`, message: "Enter the Hermes profile name." });
    }
    if (!Number.isInteger(entry.dispatch_port) || entry.dispatch_port < 1 || entry.dispatch_port > 65535) {
      issues.push({ section: "role_registry", key: `${role}.dispatch_port`, message: "Enter a port from 1 to 65535." });
    }
  }
  for (const field of classicFields) {
    const value = draft.classic[field.key];
    if (value === undefined) continue;
    if (field.type === "integer" || field.type === "number") {
      const message = checkNumber(value, field);
      if (message) issues.push({ section: "classic", key: field.key, message });
    } else if (field.type === "tier_map" || field.type === "weight_map") {
      const elementField: ClassicFieldMeta = { ...field, type: field.type === "tier_map" ? "integer" : "number" };
      const entries = value && typeof value === "object" ? Object.entries(value as Record<string, unknown>) : [];
      for (const [name, entry] of entries) {
        const message = checkNumber(entry, elementField);
        if (message) {
          issues.push({ section: "classic", key: field.key, message: `${name}: ${message}` });
          break;
        }
      }
    } else if (field.type === "enum" && field.options && !field.options.includes(String(value))) {
      issues.push({ section: "classic", key: field.key, message: `Choose one of ${field.options.join(", ")}.` });
    } else if (field.type === "boolean" && typeof value !== "boolean") {
      issues.push({ section: "classic", key: field.key, message: "Choose on or off." });
    }
  }
  return issues;
}

/** Re-apply the draft's changes onto a fresh snapshot (used by Refresh). */
export function carryDraftChanges(
  previousSnapshot: SettingsSnapshot,
  previousDraft: SettingsDraft,
  nextSnapshot: SettingsSnapshot,
): SettingsDraft {
  const next = draftFromSnapshot(nextSnapshot);
  for (const [tier, model] of Object.entries(previousDraft.routing)) {
    if (previousSnapshot.routing[tier] !== model && tier in next.routing) next.routing[tier] = model;
  }
  for (const [role, entry] of Object.entries(previousDraft.role_registry)) {
    const saved = previousSnapshot.role_registry[role];
    if (!next.role_registry[role]) continue;
    for (const field of ROLE_FIELDS) {
      if (!sameValue(saved?.[field], entry[field])) {
        (next.role_registry[role] as unknown as Record<string, unknown>)[field] = entry[field];
      }
    }
  }
  for (const [key, value] of Object.entries(previousDraft.classic)) {
    if (!sameValue(previousSnapshot.classic?.[key], value)) next.classic[key] = value;
  }
  return next;
}
