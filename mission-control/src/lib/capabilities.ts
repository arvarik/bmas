/** Daemon-reported Mission Control capability contract. */

export const SUPPORTED_CAPABILITIES_API_VERSION = "1";

export interface VariantFeatures {
  events: string[];
  panels: string[];
  graphs: string[];
  controls: string[];
  progress: string[];
  result: string[];
}

export interface EffortProfile {
  label: string;
  description: string;
  settings: Record<string, unknown>;
}

export interface VariantCapability {
  id: string;
  label: string;
  available: boolean;
  contract_version: string;
  aliases: string[];
  configuration_schema_version: string;
  supports_recovery: boolean;
  required_agent_features: string[];
  features: VariantFeatures;
  effort_profiles?: Record<string, EffortProfile>;
  reason?: string;
}

export interface CapabilitiesDocument {
  api_version: string;
  variants: VariantCapability[];
}

export type CapabilityContractErrorCode =
  | "malformed"
  | "unsupported-api-version";

export class CapabilityContractError extends Error {
  constructor(
    readonly code: CapabilityContractErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "CapabilityContractError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new CapabilityContractError("malformed", `${field} must be a non-empty string`);
  }
  return value;
}

function readStringArray(
  value: unknown,
  field: string,
  rejectDuplicates = false,
): string[] {
  if (
    !Array.isArray(value)
    || value.some((entry) => typeof entry !== "string" || entry.trim() === "")
  ) {
    throw new CapabilityContractError(
      "malformed",
      `${field} must be an array of non-empty strings`,
    );
  }
  const unique = [...new Set(value)];
  if (rejectDuplicates && unique.length !== value.length) {
    throw new CapabilityContractError("malformed", `${field} contains a duplicate value`);
  }
  return unique;
}

function parseFeatures(value: unknown, path: string): VariantFeatures {
  if (!isRecord(value)) {
    throw new CapabilityContractError("malformed", `${path} must be an object`);
  }
  return {
    events: readStringArray(value.events, `${path}.events`, true),
    panels: readStringArray(value.panels, `${path}.panels`, true),
    graphs: readStringArray(value.graphs, `${path}.graphs`, true),
    controls: readStringArray(value.controls, `${path}.controls`, true),
    progress: readStringArray(value.progress, `${path}.progress`, true),
    result: readStringArray(value.result, `${path}.result`, true),
  };
}

function parseVariant(value: unknown, index: number): VariantCapability {
  const path = `variants[${index}]`;
  if (!isRecord(value)) {
    throw new CapabilityContractError("malformed", `${path} must be an object`);
  }
  if (typeof value.available !== "boolean") {
    throw new CapabilityContractError("malformed", `${path}.available must be a boolean`);
  }
  if (typeof value.supports_recovery !== "boolean") {
    throw new CapabilityContractError(
      "malformed",
      `${path}.supports_recovery must be a boolean`,
    );
  }
  return {
    id: readString(value.id, `${path}.id`),
    label: readString(value.label, `${path}.label`),
    available: value.available,
    contract_version: readString(
      value.contract_version,
      `${path}.contract_version`,
    ),
    aliases: readStringArray(value.aliases, `${path}.aliases`, true),
    configuration_schema_version: readString(
      value.configuration_schema_version,
      `${path}.configuration_schema_version`,
    ),
    supports_recovery: value.supports_recovery,
    required_agent_features: readStringArray(
      value.required_agent_features,
      `${path}.required_agent_features`,
      true,
    ),
    features: parseFeatures(value.features, `${path}.features`),
    ...(isRecord(value.effort_profiles)
      ? { effort_profiles: parseEffortProfiles(value.effort_profiles) }
      : {}),
    ...(typeof value.reason === "string" ? { reason: value.reason } : {}),
  };
}

/** Effort profiles are optional and lenient: a bad entry is dropped. */
function parseEffortProfiles(value: Record<string, unknown>): Record<string, EffortProfile> {
  const profiles: Record<string, EffortProfile> = {};
  for (const [level, raw] of Object.entries(value)) {
    if (!isRecord(raw)) continue;
    profiles[level] = {
      label: typeof raw.label === "string" && raw.label ? raw.label : level,
      description: typeof raw.description === "string" ? raw.description : "",
      settings: isRecord(raw.settings) ? raw.settings : {},
    };
  }
  return profiles;
}

export function parseCapabilities(value: unknown): CapabilitiesDocument {
  if (!isRecord(value)) {
    throw new CapabilityContractError("malformed", "Capabilities must be an object");
  }
  const apiVersion = readString(value.api_version, "api_version");
  if (apiVersion !== SUPPORTED_CAPABILITIES_API_VERSION) {
    throw new CapabilityContractError(
      "unsupported-api-version",
      `Mission Control does not support capabilities API version ${apiVersion}`,
    );
  }
  if (!Array.isArray(value.variants)) {
    throw new CapabilityContractError("malformed", "variants must be an array");
  }
  const variants = value.variants.map(parseVariant);
  const names = new Map<string, string>();
  for (const variant of variants) {
    const previousIdOwner = names.get(variant.id);
    if (previousIdOwner) {
      throw new CapabilityContractError(
        "malformed",
        `Variant identifier ${variant.id} conflicts with ${previousIdOwner}`,
      );
    }
    names.set(variant.id, `variant ${variant.id}`);
    for (const alias of variant.aliases) {
      const previousOwner = names.get(alias);
      if (previousOwner) {
        throw new CapabilityContractError(
          "malformed",
          `Variant alias ${alias} conflicts with ${previousOwner}`,
        );
      }
      names.set(alias, `variant ${variant.id}`);
    }
  }
  return { api_version: apiVersion, variants };
}

export function findVariantCapability(
  document: CapabilitiesDocument,
  persistedVariant: string | null | undefined,
): VariantCapability | null {
  const requested = persistedVariant || "classic";
  return document.variants.find(
    (variant) => variant.id === requested || variant.aliases.includes(requested),
  ) ?? null;
}
