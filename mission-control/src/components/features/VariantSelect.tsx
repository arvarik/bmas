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
