"use client";

import { useEffect, useState } from "react";
import {
  CapabilityContractError,
  parseCapabilities,
  type VariantCapability,
} from "@/lib/capabilities";
import {
  hasMissionControlAdapter,
  supportsMissionControlVariant,
} from "@/lib/variant-support";
import { describeRuntime } from "@/lib/runtime-presentation";

interface VariantSelectProps {
  value: string;
  onChange: (variant: string) => void;
  onAvailabilityChange?: (available: boolean) => void;
}

type LoadState = "loading" | "ready" | "error";

function isSelectable(variant: VariantCapability): boolean {
  return supportsMissionControlVariant(variant);
}

function RuntimeDetails({ variant }: { variant: VariantCapability }) {
  const details = describeRuntime(variant);
  return (
    <dl className="variant-details" aria-label={`${variant.label} runtime effects`}>
      <div><dt>Runtime class</dt><dd>{details.speed}</dd></div>
      <div><dt>Cost class</dt><dd>{details.cost}</dd></div>
      <div><dt>Estimated tool access</dt><dd>{details.tools}</dd></div>
    </dl>
  );
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

  const selectableVariants = variants.filter(isSelectable);
  const selectedVariant = selectableVariants.find(
    (variant) => variant.id === value || variant.aliases.includes(value),
  ) ?? selectableVariants[0];

  if (loadState === "loading") {
    return <span className="variant-status">Checking available runtimes…</span>;
  }

  if (loadState === "error") {
    return (
      <span className="variant-status variant-status--error" role="alert">
        {errorMessage}
      </span>
    );
  }

  if (selectableVariants.length === 1) {
    return (
      <div className="variant-picker">
        <span className="variant-picker__label">Runtime</span>
        <strong className="variant-status">{selectableVariants[0].label}</strong>
        <RuntimeDetails variant={selectableVariants[0]} />
      </div>
    );
  }

  return (
    <div className="variant-picker">
      <label className="variant-picker__label" htmlFor="variant-select">Runtime</label>
      <select
        id="variant-select"
        className="variant-select"
        value={selectedVariant?.id ?? value}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next);
          onAvailabilityChange?.(
            variants.some((variant) => variant.id === next && isSelectable(variant)),
          );
        }}
        aria-describedby="variant-runtime-effects"
      >
        {variants.map((variant) => (
          <option
            key={variant.id}
            value={variant.id}
            disabled={!isSelectable(variant)}
            title={variant.reason ? `${variant.label}: ${variant.reason}` : variant.label}
          >
            {variant.label}
            {!variant.available && variant.reason ? ` (${variant.reason})` : ""}
            {!hasMissionControlAdapter(variant.id) ? " (interface unavailable)" : ""}
            {hasMissionControlAdapter(variant.id) && !isSelectable(variant)
              ? " (unsupported contract)"
              : ""}
          </option>
        ))}
      </select>
      {selectedVariant ? (
        <div id="variant-runtime-effects">
          <RuntimeDetails variant={selectedVariant} />
        </div>
      ) : null}
    </div>
  );
}

export default VariantSelect;
