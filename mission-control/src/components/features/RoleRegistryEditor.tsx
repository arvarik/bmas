"use client";

/**
 * RoleRegistryEditor
 *
 * Allows operators to override the blackboard role registry for the current
 * server session. Controls which Hermes profile and node host each role
 * dispatches to. Changes take effect on the next task submission.
 * Restarting the container reverts to bmas.yaml defaults.
 *
 * UX principles:
 * - Sticky section header with dirty-count badge
 * - Card per role: icon + description + field group
 * - Port field is narrow and sits inline with Profile
 * - Override badge tracks per-card, not per-page
 * - yaml-default hint shown inline when value differs
 * - onDirtyChange callback for parent ribbon
 */

import React, { useState, useCallback, useEffect, useMemo } from "react";
import {
  Network,
  Save,
  RotateCcw,
  CheckCircle,
  AlertCircle,
  Info,
  ChevronDown,
  Server as ServerIcon,
  Hash,
  User,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────

interface RegistryEntry {
  preferred_host: string | null;
  profile: string;
  dispatch_port: number;
  endpoints?: string[];
}

interface HostOption {
  host: string;
  name: string;
  role: string;
}

// ── Role metadata ─────────────────────────────────────────────────────────

const ROLE_META: Record<string, { icon: string; color: string; description: string }> = {
  planner: {
    icon: "🗺",
    color: "hsl(265 80% 65%)",
    description: "Decomposes the task and orchestrates round execution",
  },
  expert: {
    icon: "🔬",
    color: "hsl(197 80% 55%)",
    description: "Performs specialised research and analysis",
  },
  critic: {
    icon: "⚖️",
    color: "hsl(38 90% 55%)",
    description: "Reviews and critiques expert contributions",
  },
  conflict_resolver: {
    icon: "🤝",
    color: "hsl(20 80% 60%)",
    description: "Resolves contradictions between board entries",
  },
  cleaner: {
    icon: "🧹",
    color: "hsl(160 60% 50%)",
    description: "Prunes redundant or low-salience board entries",
  },
  decider: {
    icon: "⚡",
    color: "hsl(50 90% 52%)",
    description: "Makes final decisions and synthesises answers",
  },
  universal: {
    icon: "🌐",
    color: "hsl(215 30% 55%)",
    description: "Fallback role for load-balanced dispatch",
  },
};

const ROLE_ORDER = ["planner", "expert", "critic", "conflict_resolver", "cleaner", "decider", "universal"];

// ── Sub-components ────────────────────────────────────────────────────────

function HostSelect({
  id,
  value,
  onChange,
  hosts,
  disabled,
}: {
  id?: string;
  value: string | null;
  onChange: (v: string | null) => void;
  hosts: HostOption[];
  disabled?: boolean;
}) {
  return (
    <div className="settings-model-select-wrapper">
      <select
        id={id}
        value={value ?? "any"}
        onChange={(e) => onChange(e.target.value === "any" ? null : e.target.value)}
        disabled={disabled}
        className="settings-model-select"
      >
        <option value="any">any — load-balanced</option>
        {hosts.map((h) => (
          <option key={h.host} value={h.host}>
            {h.name ? `${h.name} · ${h.host}` : h.host}
          </option>
        ))}
      </select>
      <ChevronDown size={14} className="settings-model-select-arrow" />
    </div>
  );
}

function isDirtyEntry(entry: RegistryEntry, def: RegistryEntry | null): boolean {
  if (!def) return false;
  return (
    entry.preferred_host !== def.preferred_host ||
    entry.profile !== def.profile ||
    entry.dispatch_port !== def.dispatch_port
  );
}

function RoleCard({
  roleName,
  entry,
  serverEntry,
  defaultEntry,
  hosts,
  onChange,
  disabled,
}: {
  roleName: string;
  entry: RegistryEntry;
  serverEntry: RegistryEntry | null;
  defaultEntry: RegistryEntry | null;
  hosts: HostOption[];
  onChange: (role: string, patch: Partial<RegistryEntry>) => void;
  disabled?: boolean;
}) {
  const meta = ROLE_META[roleName] ?? {
    icon: "🤖",
    color: "hsl(215 30% 55%)",
    description: "Custom role",
  };

  // "overridden" = current local differs from server-authoritative
  const isLocalDirty = serverEntry ? isDirtyEntry(entry, serverEntry) : false;
  // "server override" = server differs from yaml default
  const isServerOverride = defaultEntry ? isDirtyEntry(serverEntry ?? entry, defaultEntry) : false;

  return (
    <div
      className={`settings-role-card ${isLocalDirty ? "settings-role-card--modified" : ""}`}
    >
      {/* Header */}
      <div className="settings-role-card__header">
        <div
          className="settings-role-card__icon"
          style={{ background: `${meta.color}18` }}
          aria-hidden="true"
        >
          <span>{meta.icon}</span>
        </div>
        <div className="settings-role-card__title-block">
          <div className="settings-role-card__title-row">
            <span className="settings-role-card__name">{roleName}</span>
            {isLocalDirty && (
              <span
                className="settings-override-badge"
                title="Unsaved local change"
                style={{ fontSize: "9px", padding: "1px 5px" }}
              >
                <Info size={9} aria-hidden="true" />
                unsaved
              </span>
            )}
            {!isLocalDirty && isServerOverride && (
              <span
                className="settings-override-badge"
                title={`yaml default differs`}
                style={{ fontSize: "9px", padding: "1px 5px" }}
              >
                <Info size={9} aria-hidden="true" />
                overridden
              </span>
            )}
          </div>
          <span className="settings-role-card__desc">{meta.description}</span>
        </div>
      </div>

      <div className="settings-role-card__divider" />

      {/* Fields */}
      <div className="settings-role-card__fields">
        {/* Preferred Host */}
        <div className="settings-role-card__field">
          <label className="settings-field-label" htmlFor={`${roleName}-host`}>
            <ServerIcon size={11} aria-hidden="true" />
            Preferred Host
          </label>
          <HostSelect
            id={`${roleName}-host`}
            value={entry.preferred_host}
            onChange={(v) => onChange(roleName, { preferred_host: v })}
            hosts={hosts}
            disabled={disabled}
          />
          {defaultEntry && entry.preferred_host !== defaultEntry.preferred_host && (
            <span className="settings-field-hint" aria-label="yaml default">
              yaml: {defaultEntry.preferred_host ?? "any"}
            </span>
          )}
        </div>

        {/* Profile + Port side-by-side */}
        <div className="settings-role-card__field-row">
          <div className="settings-role-card__field">
            <label className="settings-field-label" htmlFor={`${roleName}-profile`}>
              <User size={11} aria-hidden="true" />
              Profile
            </label>
            <input
              id={`${roleName}-profile`}
              type="text"
              className="settings-text-input"
              value={entry.profile}
              onChange={(e) => onChange(roleName, { profile: e.target.value })}
              disabled={disabled}
              placeholder="hermes profile"
              aria-label={`${roleName} Hermes profile`}
              spellCheck={false}
            />
            {defaultEntry && entry.profile !== defaultEntry.profile && (
              <span className="settings-field-hint">yaml: {defaultEntry.profile}</span>
            )}
          </div>

          <div className="settings-role-card__field settings-role-card__field--port">
            <label className="settings-field-label" htmlFor={`${roleName}-port`}>
              <Hash size={11} aria-hidden="true" />
              Port
            </label>
            <input
              id={`${roleName}-port`}
              type="number"
              className="settings-text-input settings-number-input"
              value={entry.dispatch_port}
              onChange={(e) =>
                onChange(roleName, {
                  dispatch_port: parseInt(e.target.value, 10) || entry.dispatch_port,
                })
              }
              disabled={disabled}
              min={1}
              max={65535}
              aria-label={`${roleName} dispatch port`}
            />
            {defaultEntry && entry.dispatch_port !== defaultEntry.dispatch_port && (
              <span className="settings-field-hint">yaml: {defaultEntry.dispatch_port}</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────────

interface RoleRegistryEditorProps {
  roleRegistry: Record<string, RegistryEntry>;
  defaultRegistry: Record<string, RegistryEntry>;
  configuredHosts: HostOption[];
  onSave: (entries: Record<string, Partial<RegistryEntry>>) => Promise<void>;
  onDirtyChange?: (count: number) => void;
}

export function RoleRegistryEditor({
  roleRegistry,
  defaultRegistry,
  configuredHosts,
  onSave,
  onDirtyChange,
}: RoleRegistryEditorProps) {
  const [localRegistry, setLocalRegistry] = useState<Record<string, RegistryEntry>>(roleRegistry);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<"idle" | "success" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");

  // Sync when parent registry changes (after global reset / server refresh)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLocalRegistry(roleRegistry);
  }, [roleRegistry]);

  // Dirty = local differs from server-authoritative registry
  const dirtyRoles = useMemo(
    () =>
      Object.keys(localRegistry).filter((role) =>
        isDirtyEntry(localRegistry[role], roleRegistry[role] ?? null)
      ),
    [localRegistry, roleRegistry]
  );
  const dirtyCount = dirtyRoles.length;

  useEffect(() => {
    onDirtyChange?.(dirtyCount);
  }, [dirtyCount, onDirtyChange]);

  const handleChange = useCallback((role: string, patch: Partial<RegistryEntry>) => {
    setLocalRegistry((prev) => ({
      ...prev,
      [role]: { ...prev[role], ...patch },
    }));
    setSaveStatus("idle");
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setErrorMsg("");

    // Build minimal patch (only changed fields per role)
    const patch: Record<string, Partial<RegistryEntry>> = {};
    for (const [role, entry] of Object.entries(localRegistry)) {
      const server = roleRegistry[role];
      const rolePatch: Partial<RegistryEntry> = {};
      if (!server || entry.preferred_host !== server.preferred_host)
        rolePatch.preferred_host = entry.preferred_host;
      if (!server || entry.profile !== server.profile) rolePatch.profile = entry.profile;
      if (!server || entry.dispatch_port !== server.dispatch_port)
        rolePatch.dispatch_port = entry.dispatch_port;
      if (Object.keys(rolePatch).length > 0) patch[role] = rolePatch;
    }

    try {
      await onSave(patch);
      setSaveStatus("success");
      setTimeout(() => setSaveStatus("idle"), 2500);
    } catch (e) {
      setSaveStatus("error");
      setErrorMsg(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [localRegistry, roleRegistry, onSave]);

  const handleResetToDefaults = useCallback(() => {
    setLocalRegistry({ ...defaultRegistry });
    setSaveStatus("idle");
  }, [defaultRegistry]);

  const isDirty = dirtyCount > 0;

  const roles = Object.keys(localRegistry).sort(
    (a, b) => (ROLE_ORDER.indexOf(a) ?? 99) - (ROLE_ORDER.indexOf(b) ?? 99)
  );

  return (
    <div className={`settings-section ${isDirty ? "settings-section--dirty" : ""}`}>
      {/* ── Sticky section header ─────────────────────────────────────── */}
      <div className="settings-section__header">
        <div className="settings-section__title-row">
          <div
            className="settings-section__icon"
            style={{ background: "hsl(197 80% 55% / 0.12)" }}
            aria-hidden="true"
          >
            <Network size={17} style={{ color: "hsl(197 80% 55%)" }} />
          </div>
          <div className="settings-section__text">
            <h2 className="settings-section__title">
              Role Registry
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
              Configure which Hermes profile and node host each blackboard role dispatches to.
              Session-only overrides.
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
            <span>Reset to defaults</span>
          </button>
          <button
            onClick={() => void handleSave()}
            className={`settings-btn ${
              saveStatus === "success" ? "settings-btn--success" : "settings-btn--primary"
            } ${saving ? "settings-btn--loading" : ""}`}
            disabled={saving || !isDirty}
            id="save-role-registry-btn"
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

        <div className="settings-role-grid">
          {roles.map((role) => (
            <RoleCard
              key={role}
              roleName={role}
              entry={localRegistry[role]}
              serverEntry={roleRegistry[role] ?? null}
              defaultEntry={defaultRegistry[role] ?? null}
              hosts={configuredHosts}
              onChange={handleChange}
              disabled={saving}
            />
          ))}
          {roles.length === 0 && (
            <div className="settings-empty-state">
              <Network size={32} aria-hidden="true" />
              <p>No role registry configured in bmas.yaml</p>
              <span>
                Add a <code>coordination.role_registry</code> section to configure roles.
              </span>
            </div>
          )}
        </div>

        <div className="settings-info-note" role="note">
          <Info size={12} aria-hidden="true" />
          <span>
            <strong>any (load-balanced)</strong> distributes dispatch across all configured nodes
            with the matching profile. Dispatch port is the api_server.py bridge port (default:
            8000).
          </span>
        </div>
      </div>
    </div>
  );
}

export default RoleRegistryEditor;
