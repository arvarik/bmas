"use client";

import React, { useState, useEffect } from "react";

/**
 * VariantSelect — dropdown for the composer (doc 08 §2.1).
 *
 * Fetches the daemon's capabilities and renders a compact select with
 * one enabled option (traditional) and disabled options with tooltips
 * for planned variants.
 */

interface VariantDescriptor {
  id: string;
  label: string;
  available: boolean;
  reason?: string;
}

interface VariantSelectProps {
  value: string;
  onChange: (variant: string) => void;
}

export function VariantSelect({ value, onChange }: VariantSelectProps) {
  const [variants, setVariants] = useState<VariantDescriptor[]>([
    { id: "traditional", label: "Blackboard (bMAS)", available: true },
  ]);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/capabilities")
      .then((r) => r.json())
      .then((data: { variants: VariantDescriptor[] }) => {
        if (!cancelled && data.variants?.length) {
          setVariants(data.variants);
        }
      })
      .catch(() => {
        // Keep the static default
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <select
      id="variant-select"
      className="variant-select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={{
        padding: "4px 8px",
        background: "var(--surface-hover)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-sm)",
        color: "var(--text-secondary)",
        fontSize: "var(--text-xs)",
        fontFamily: "var(--font-sans)",
        cursor: "pointer",
        outline: "none",
        minWidth: 0,
        maxWidth: 160,
      }}
    >
      {variants.map((v) => (
        <option
          key={v.id}
          value={v.id}
          disabled={!v.available}
          title={v.reason ? `${v.label} — ${v.reason}` : v.label}
        >
          {v.label}
          {!v.available && v.reason ? ` (${v.reason})` : ""}
        </option>
      ))}
    </select>
  );
}

export default VariantSelect;
