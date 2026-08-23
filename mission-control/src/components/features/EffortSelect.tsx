"use client";

/**
 * EffortSelect — the deliberation lever in the task composer.
 *
 * Effort is independent of complexity: triage still picks the model tier;
 * effort picks how hard the runtime pushes before it accepts an answer.
 * Levels come from the selected runtime's effort profiles in the daemon
 * capability document. A runtime without profiles shows no control.
 */

import { useEffect, useState } from "react";
import { Gauge } from "lucide-react";
import { parseCapabilities, type EffortProfile } from "@/lib/capabilities";
import { SelectMenu, type SelectOption } from "@/components/ui/SelectMenu";

const LEVEL_ORDER = ["quick", "standard", "thorough", "exhaustive"];

function profileSummary(level: string, profile: EffortProfile): string {
  const settings = profile.settings;
  const parts: string[] = [];
  const rounds = settings.max_rounds;
  const budget = settings.budget_ceiling_usd;
  if (typeof rounds === "number") parts.push(`up to ${rounds} rounds`);
  if (typeof budget === "number") parts.push(`$${budget.toFixed(2)} ceiling`);
  const limits = parts.length ? ` (${parts.join(", ")})` : "";
  return `${profile.description}${limits}`;
}

export function EffortSelect({
  variant,
  value,
  onChange,
}: {
  variant: string;
  value: string;
  onChange: (level: string) => void;
}) {
  const [profiles, setProfiles] = useState<Record<string, EffortProfile> | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/capabilities", { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return parseCapabilities(await response.json());
      })
      .then((document) => {
        if (cancelled) return;
        const capability = document.variants.find(
          (candidate) => candidate.id === variant || candidate.aliases.includes(variant),
        );
        setProfiles(capability?.effort_profiles ?? null);
      })
      .catch(() => {
        if (!cancelled) setProfiles(null);
      });
    return () => {
      cancelled = true;
    };
  }, [variant]);

  // The stored preference can name a level this runtime does not offer.
  useEffect(() => {
    if (profiles && !profiles[value]) onChange("standard");
  }, [onChange, profiles, value]);

  if (!profiles || Object.keys(profiles).length === 0) return null;

  const options: SelectOption[] = LEVEL_ORDER
    .filter((level) => profiles[level])
    .map((level) => ({
      value: level,
      label: profiles[level].label,
      description: profileSummary(level, profiles[level]),
    }));
  if (options.length === 0) return null;

  return (
    <SelectMenu
      aria-label="Effort"
      variant="pill"
      size="sm"
      value={profiles[value] ? value : "standard"}
      options={options}
      prefix={<Gauge size={14} aria-hidden="true" />}
      onChange={onChange}
    />
  );
}

export default EffortSelect;
