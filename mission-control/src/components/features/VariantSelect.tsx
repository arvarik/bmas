"use client";

import React, { useState, useEffect } from "react";
import {
  CapabilityContractError,
  parseCapabilities,
  type VariantCapability,
} from "@/lib/capabilities";
import {
  hasMissionControlAdapter,
  supportsMissionControlVariant,
} from "@/lib/variant-support";

/**
 * VariantSelect — dropdown for the composer (doc 08 §2.1).
 *
 * Fetches the daemon's capabilities and renders only reported variants.
 * Local adapters decide whether Mission Control can open each variant.
 */

interface VariantSelectProps {
  value: string;
  onChange: (variant: string) => void;
  onAvailabilityChange?: (available: boolean) => void;
}

type LoadState = "loading" | "ready" | "error";

function isSelectable(variant: VariantCapability): boolean {
  return supportsMissionControlVariant(variant);
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

  return (
    <select
      id="variant-select"
      className="variant-select"
      value={value}
      onChange={(event) => {
        const next = event.target.value;
        onChange(next);
        onAvailabilityChange?.(
          variants.some((variant) => variant.id === next && isSelectable(variant)),
        );
      }}
      disabled={loadState !== "ready"}
      aria-label="Coordination variant"
      style={{
        padding: "3px 22px 3px 8px",
        background: "var(--surface-hover)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-full)",
        color: "var(--text-tertiary)",
        fontSize: "11px",
        fontFamily: "var(--font-sans)",
        cursor: "pointer",
        outline: "none",
        minWidth: 0,
        maxWidth: 160,
        height: 24,
        transition: "color 150ms ease, border-color 150ms ease",
        WebkitAppearance: "none",
        appearance: "none",
        backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%236b7280' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E")`,
        backgroundRepeat: "no-repeat",
        backgroundPosition: "right 6px center",
      }}
    >
      {loadState === "loading" ? (
        <option value={value}>Loading capabilities…</option>
      ) : null}
      {loadState === "error" ? (
        <option value={value}>{errorMessage}</option>
      ) : null}
      {variants.map((v) => (
        <option
          key={v.id}
          value={v.id}
          disabled={!isSelectable(v)}
          title={v.reason ? `${v.label} — ${v.reason}` : v.label}
        >
          {v.label}
          {!v.available && v.reason ? ` (${v.reason})` : ""}
          {!hasMissionControlAdapter(v.id) ? " (interface unavailable)" : ""}
          {hasMissionControlAdapter(v.id) && !isSelectable(v) ? " (unsupported contract)" : ""}
        </option>
      ))}
    </select>
  );
}

export default VariantSelect;
