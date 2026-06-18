"use client";

/**
 * ComplexityRoutingEditor
 *
 * Allows operators to override complexity → model routing for the current
 * server session. Changes take effect on the next task submission.
 * Restarting the container reverts to bmas.yaml defaults.
 *
 * UX principles:
 * - Section card with sticky header (visible while scrolling)
 * - Colored pip bars for immediate tier recognition
 * - Dirty-count badge in header shows how many tiers are modified
 * - Save button activates only when there are unsaved changes
 * - Override badge positioned inline with metadata (not clipping select)
 * - Provider badge + max output tokens confirm selection at a glance
 * - Local/edge model shows model name + node count + inference endpoints
 * - Mobile-first: stack on small screens, side-by-side on md+
 */

import React, { useState, useCallback, useEffect } from "react";
import {
  Zap,
  RotateCcw,
  Save,
  CheckCircle,
  AlertCircle,
  Info,
  ChevronDown,
  ArrowRight,
  Server,
  Cpu,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────

interface EdgeNodeInfo {
  node_name: string;
  host: string;
  port: number;
  model: string;
}

export interface ModelInfo {
  alias: string;
  provider: string;
  model: string;
  max_tokens: number | null;
  // Local/edge-only fields
  node_count?: number;
  edge_nodes?: EdgeNodeInfo[];
}

interface RoutingRow {
  tier: string;
  label: string;
  description: string;
  color: string;
}

const COMPLEXITY_ROWS: RoutingRow[] = [
  {
    tier: "simple",
    label: "Simple",
    description: "Factual lookups, unit conversions, single-step ops",
    color: "hsl(142 65% 45%)",
  },
  {
    tier: "light",
    label: "Light",
    description: "Short extractions, regex, 1–3 sentence summaries",
    color: "hsl(198 80% 50%)",
  },
  {
    tier: "medium",
    label: "Medium",
    description: "Single-function code, focused explanations, drafts",
    color: "hsl(38 90% 52%)",
  },
  {
    tier: "complex",
    label: "Complex",
    description: "Architecture, multi-component systems, research synthesis",
    color: "hsl(265 75% 62%)",
  },
];

// ── Sub-components ────────────────────────────────────────────────────────

function ModelSelect({
  value,
  onChange,
  models,
  disabled,
  id,
}: {
  value: string;
  onChange: (v: string) => void;
  models: ModelInfo[];
  disabled?: boolean;
  id?: string;
}) {
  return (
    <div className="settings-model-select-wrapper">
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className="settings-model-select"
        aria-label="Model selection"
      >
        {models.map((m) => (
          <option key={m.alias} value={m.alias}>
            {m.alias}
            {m.provider === "local"
              ? ` — ${m.model}${m.node_count ? ` (×${m.node_count} nodes)` : ""}`
              : ` — ${m.model}`}
          </option>
        ))}
      </select>
      <ChevronDown size={14} className="settings-model-select-arrow" />
    </div>
  );
}

/** Edge inference panel — shows model + node details for the local alias */
function EdgeInfraPanel({ modelInfo }: { modelInfo: ModelInfo }) {
  if (modelInfo.provider !== "local") return null;
  const nodes = modelInfo.edge_nodes ?? [];
  return (
    <div className="settings-edge-panel">
      <div className="settings-edge-panel__header">
        <Cpu size={11} aria-hidden="true" />
        <span>Edge inference — {modelInfo.model}</span>
        {modelInfo.node_count != null && (
          <span className="settings-edge-panel__node-count">
            {modelInfo.node_count} node{modelInfo.node_count !== 1 ? "s" : ""}
          </span>
        )}
      </div>
      {nodes.length > 0 && (
        <ul className="settings-edge-panel__nodes" aria-label="Edge inference nodes">
          {nodes.map((n) => (
            <li key={n.node_name} className="settings-edge-panel__node">
              <Server size={10} aria-hidden="true" />
              <span className="settings-edge-panel__node-name">{n.node_name}</span>
              <span className="settings-edge-panel__node-addr">
                {n.host}:{n.port}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────────

interface ComplexityRoutingEditorProps {
  routing: Record<string, string>;
  defaultRouting: Record<string, string>;
  availableModels: ModelInfo[];
  onSave: (overrides: Record<string, string>) => Promise<void>;
  /** Called whenever dirty count changes so parent can track global dirty state */
  onDirtyChange?: (count: number) => void;
}

export function ComplexityRoutingEditor({
  routing,
  defaultRouting,
  availableModels,
  onSave,
  onDirtyChange,
}: ComplexityRoutingEditorProps) {
  const [localRouting, setLocalRouting] = useState<Record<string, string>>(routing);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<"idle" | "success" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");

  // Sync when parent routing changes (e.g. after global reset)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLocalRouting(routing);
  }, [routing]);

  // Count modified tiers vs. server-authoritative routing
  const dirtyTiers = COMPLEXITY_ROWS.filter(
    (r) => (localRouting[r.tier] ?? "") !== (routing[r.tier] ?? "")
  );
  const dirtyCount = dirtyTiers.length;

  useEffect(() => {
    onDirtyChange?.(dirtyCount);
  }, [dirtyCount, onDirtyChange]);

  const handleChange = useCallback((tier: string, model: string) => {
    setLocalRouting((prev) => ({ ...prev, [tier]: model }));
    setSaveStatus("idle");
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setErrorMsg("");
    try {
      await onSave(localRouting);
      setSaveStatus("success");
      setTimeout(() => setSaveStatus("idle"), 2500);
    } catch (e) {
      setSaveStatus("error");
      setErrorMsg(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [localRouting, onSave]);

  const handleResetToDefaults = useCallback(() => {
    setLocalRouting({ ...defaultRouting });
    setSaveStatus("idle");
  }, [defaultRouting]);

  const isDirty = dirtyCount > 0;

  return (
    <div className={`settings-section ${isDirty ? "settings-section--dirty" : ""}`}>
      {/* ── Sticky section header ─────────────────────────────────────── */}
      <div className="settings-section__header">
        <div className="settings-section__title-row">
          <div
            className="settings-section__icon"
            style={{ background: "hsl(265 80% 65% / 0.12)" }}
            aria-hidden="true"
          >
            <Zap size={17} style={{ color: "hsl(265 80% 65%)" }} />
          </div>
          <div className="settings-section__text">
            <h2 className="settings-section__title">
              Complexity Routing
              {isDirty && (
                <span
                  className="settings-section__dirty-count"
                  title={`${dirtyCount} unsaved change${dirtyCount !== 1 ? "s" : ""}`}
                >
                  {dirtyCount}
                </span>
              )}
            </h2>
            <p className="settings-section__subtitle">
              Map each complexity tier to a LiteLLM model alias. Session-only — restarts revert to{" "}
              <code className="settings-inline-code">bmas.yaml</code>.
            </p>
          </div>
        </div>

        <div className="settings-section__actions">
          <button
            onClick={handleResetToDefaults}
            className="settings-btn settings-btn--ghost settings-btn--sm"
            title="Revert fields to yaml defaults (still requires Save)"
            disabled={saving}
          >
            <RotateCcw size={13} />
            <span>Reset</span>
          </button>
          <button
            onClick={() => void handleSave()}
            className={`settings-btn ${
              saveStatus === "success" ? "settings-btn--success" : "settings-btn--primary"
            } ${saving ? "settings-btn--loading" : ""}`}
            disabled={saving || !isDirty}
            id="save-routing-btn"
            aria-live="polite"
          >
            {saving ? (
              <span className="settings-spinner" aria-hidden="true" />
            ) : saveStatus === "success" ? (
              <CheckCircle size={13} />
            ) : (
              <Save size={13} />
            )}
            <span>{saving ? "Saving…" : saveStatus === "success" ? "Saved!" : "Save"}</span>
          </button>
        </div>
      </div>

      {/* ── Section body ──────────────────────────────────────────────── */}
      <div className="settings-section__body">
        {saveStatus === "error" && (
          <div className="settings-error-banner" role="alert">
            <AlertCircle size={14} />
            <span>{errorMsg}</span>
          </div>
        )}

        <div className="settings-routing-grid" role="list">
          {COMPLEXITY_ROWS.map((row) => {
            const currentModel = localRouting[row.tier] ?? "";
            const serverModel = routing[row.tier] ?? "";
            const defaultModel = defaultRouting[row.tier] ?? "";
            const isOverriddenVsServer = currentModel !== serverModel;
            const isOverriddenVsDefault = serverModel !== defaultModel;
            const modelInfo = availableModels.find((m) => m.alias === currentModel);
            const isLocal = modelInfo?.provider === "local";

            return (
              <div
                key={row.tier}
                role="listitem"
                className={`settings-routing-row ${isOverriddenVsServer ? "settings-routing-row--modified" : ""}`}
              >
                {/* Colored pip + tier info */}
                <div
                  className="settings-routing-row__tier"
                  style={{ "--tier-color": row.color } as React.CSSProperties}
                >
                  <div
                    className="settings-routing-tier-pip"
                    aria-hidden="true"
                    style={{ background: row.color }}
                  />
                  <div className="settings-routing-tier-info">
                    <span className="settings-routing-tier-name">{row.label}</span>
                    <p className="settings-routing-row__desc">{row.description}</p>
                  </div>
                </div>

                {/* Arrow — hidden on mobile (layout stacks) */}
                <div className="settings-routing-arrow" aria-hidden="true">
                  <ArrowRight size={16} strokeWidth={1.5} />
                </div>

                {/* Model selector + metadata */}
                <div className="settings-routing-row__model">
                  <label className="sr-only" htmlFor={`routing-${row.tier}`}>
                    {row.label} model
                  </label>
                  <ModelSelect
                    id={`routing-${row.tier}`}
                    value={currentModel}
                    onChange={(v) => handleChange(row.tier, v)}
                    models={availableModels}
                    disabled={saving}
                  />

                  {/* Metadata row */}
                  <div className="settings-routing-row__model-meta">
                    {modelInfo && (
                      <span
                        className={`settings-provider-badge ${isLocal ? "settings-provider-badge--local" : ""}`}
                      >
                        {isLocal ? "Local" : modelInfo.provider}
                      </span>
                    )}
                    {/* Max output tokens — only shown for cloud models */}
                    {modelInfo?.max_tokens != null && !isLocal && (
                      <span
                        className="settings-token-badge"
                        title="Maximum output tokens per request"
                      >
                        {modelInfo.max_tokens.toLocaleString()} max out
                      </span>
                    )}
                    {/* Edge node count for local */}
                    {isLocal && modelInfo?.node_count != null && (
                      <span
                        className="settings-token-badge"
                        title="Number of edge inference nodes (round-robin)"
                      >
                        {modelInfo.node_count} node{modelInfo.node_count !== 1 ? "s" : ""}
                      </span>
                    )}
                    {/* Override vs. yaml default (server-persisted) */}
                    {isOverriddenVsDefault && !isOverriddenVsServer && (
                      <span
                        className="settings-override-badge"
                        title={`yaml default: ${defaultModel}`}
                        aria-label="Session override active"
                      >
                        <Info size={9} aria-hidden="true" />
                        overridden
                      </span>
                    )}
                    {isOverriddenVsServer && (
                      <span
                        className="settings-override-badge"
                        title="Unsaved local change"
                        aria-label="Unsaved change"
                      >
                        <Info size={9} aria-hidden="true" />
                        unsaved
                      </span>
                    )}
                  </div>

                  {/* Edge inference panel — only when local is selected */}
                  {isLocal && modelInfo && <EdgeInfraPanel modelInfo={modelInfo} />}
                </div>
              </div>
            );
          })}
        </div>

        <div className="settings-info-note" role="note">
          <Info size={12} aria-hidden="true" />
          <span>
            <strong>local</strong> routes to edge inference nodes (round-robin). All other values
            are LiteLLM model aliases defined in your{" "}
            <code className="settings-inline-code">bmas.yaml</code>. Max output tokens are set in{" "}
            <code className="settings-inline-code">models[alias].max_tokens</code>.
          </span>
        </div>
      </div>
    </div>
  );
}

export default ComplexityRoutingEditor;
