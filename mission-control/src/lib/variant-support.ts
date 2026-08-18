import type { VariantCapability } from "@/lib/capabilities";

export const CLASSIC_CONTRACT_VERSIONS = ["1"] as const;

const CONTRACT_VERSIONS: Readonly<Record<string, readonly string[]>> = {
  classic: CLASSIC_CONTRACT_VERSIONS,
};

export function hasMissionControlAdapter(variantId: string): boolean {
  return variantId in CONTRACT_VERSIONS;
}

export function supportsMissionControlVariant(capability: VariantCapability): boolean {
  return capability.available
    && Boolean(CONTRACT_VERSIONS[capability.id]?.includes(capability.contract_version));
}
