export interface SettingsRegistryEntry {
  preferred_host: string | null;
  profile: string;
  dispatch_port: number;
}

export interface SettingsSnapshot {
  routing: Record<string, string>;
  role_registry: Record<string, SettingsRegistryEntry>;
  defaults: {
    routing: Record<string, string>;
    role_registry: Record<string, SettingsRegistryEntry>;
  };
}

export interface SettingsPresentationChange {
  label: string;
  before: string;
  after: string;
}

function yamlValue(value: string | number | null): string {
  if (value === null) return "null";
  if (typeof value === "number") return String(value);
  return JSON.stringify(value);
}

export function buildYamlPatch(settings: SettingsSnapshot): string {
  const lines = ["# Merge these session overrides into bmas.yaml, then restart bMAS."];
  const routingChanges = Object.entries(settings.routing).filter(
    ([tier, model]) => settings.defaults.routing[tier] !== model
  );
  const roleChanges = Object.entries(settings.role_registry).filter(([role, entry]) => {
    const defaultEntry = settings.defaults.role_registry[role];
    return (
      !defaultEntry ||
      entry.preferred_host !== defaultEntry.preferred_host ||
      entry.profile !== defaultEntry.profile ||
      entry.dispatch_port !== defaultEntry.dispatch_port
    );
  });

  if (routingChanges.length > 0) {
    lines.push("routing:");
    for (const [tier, model] of routingChanges) lines.push(`  ${tier}: ${yamlValue(model)}`);
  }

  if (roleChanges.length > 0) {
    lines.push("coordination:", "  role_registry:");
    for (const [role, entry] of roleChanges) {
      lines.push(
        `    ${role}:`,
        `      preferred_host: ${yamlValue(entry.preferred_host)}`,
        `      profile: ${yamlValue(entry.profile)}`,
        `      dispatch_port: ${yamlValue(entry.dispatch_port)}`
      );
    }
  }

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
      changes.push({
        label: `${role} profile`,
        before: entry.profile,
        after: defaultEntry.profile,
      });
    }
    if (entry.dispatch_port !== defaultEntry.dispatch_port) {
      changes.push({
        label: `${role} dispatch port`,
        before: String(entry.dispatch_port),
        after: String(defaultEntry.dispatch_port),
      });
    }
  }

  return changes;
}
