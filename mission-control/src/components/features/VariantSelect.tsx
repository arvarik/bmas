"use client";

/**
 * VariantSelect — the runtime picker inside the task composer.
 *
 * It reads the daemon capability document and offers every runtime that
 * Mission Control can render. A runtime that the daemon reports as
 * unavailable stays visible but disabled, with the daemon's reason.
 */

import { useEffect, useState } from "react";
import { Layers } from "lucide-react";
import {
  CapabilityContractError,
  parseCapabilities,
  type VariantCapability,
} from "@/lib/capabilities";
import {
  hasMissionControlAdapter,
  supportsMissionControlVariant,
} from "@/lib/variant-support";
import { SelectMenu, type SelectOption } from "@/components/ui/SelectMenu";

interface VariantSelectProps {
  value: string;
  onChange: (variant: string) => void;
  onAvailabilityChange?: (available: boolean) => void;
}

type LoadState = "loading" | "ready" | "error";

const RUNTIME_SUMMARY: Record<string, string> = {
  classic: "Control unit routes roles over a shared blackboard",
  patchboard: "Independent contributions merged in one integration turn",
  stigmergic: "Workers revise one shared artifact in order",
};

function isSelectable(variant: VariantCapability): boolean {
  return supportsMissionControlVariant(variant);
}

function optionDescription(variant: VariantCapability): string | undefined {
  if (!variant.available && variant.reason) return variant.reason;
  if (!hasMissionControlAdapter(variant.id)) return "Interface unavailable";
  if (!isSelectable(variant)) return "Unsupported contract";
  return RUNTIME_SUMMARY[variant.id];
}

export function VariantSelect({
  value,
  onChange,
  onAvailabilityChange,
}: VariantSelectProps) {
  const [variants, setVariants] = useState<VariantCapability[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [errorMessage, setErrorMessage] = useState("Capabilities unavailable");

  useEffect(() => {
    let cancelled = false;
    fetch("/api/capabilities", { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Capabilities returned HTTP ${response.status}`);
        return response.json() as Promise<unknown>;
      })
      .then((raw) => {
        const document = parseCapabilities(raw);
        if (cancelled) return;
        setVariants(document.variants);
        setLoadState("ready");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const message = error instanceof CapabilityContractError
          ? error.message
          : "Capabilities unavailable";
        setErrorMessage(message);
        setLoadState("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (loadState !== "ready") {
      onAvailabilityChange?.(false);
      return;
    }
    const selected = variants.find(
      (variant) => variant.id === value || variant.aliases.includes(value),
    );
    onAvailabilityChange?.(Boolean(selected && isSelectable(selected)));
    if (selected && selected.id !== value) onChange(selected.id);
  }, [loadState, onAvailabilityChange, onChange, value, variants]);

  if (loadState === "loading") {
    return <span className="variant-status">Checking runtimes…</span>;
  }

  if (loadState === "error") {
    return (
      <span className="variant-status variant-status--error" role="alert">
        {errorMessage}
      </span>
    );
  }

  const selectableVariants = variants.filter(isSelectable);
  const selectedVariant = selectableVariants.find(
    (variant) => variant.id === value || variant.aliases.includes(value),
  ) ?? selectableVariants[0];

  if (selectableVariants.length <= 1) {
    return (
      <span className="variant-status variant-status--fixed" title="Only one runtime is available">
        <Layers size={14} aria-hidden="true" />
        {selectableVariants[0]?.label ?? "No runtime available"}
      </span>
    );
  }

  const options: SelectOption[] = variants.map((variant) => ({
    value: variant.id,
    label: variant.label,
    description: optionDescription(variant),
    disabled: !isSelectable(variant),
  }));

  return (
    <SelectMenu
      aria-label="Runtime"
      variant="pill"
      size="sm"
      value={selectedVariant?.id ?? value}
      options={options}
      prefix={<Layers size={14} aria-hidden="true" />}
      onChange={(next) => {
        onChange(next);
        onAvailabilityChange?.(
          variants.some((variant) => variant.id === next && isSelectable(variant)),
        );
      }}
    />
  );
}

export default VariantSelect;
