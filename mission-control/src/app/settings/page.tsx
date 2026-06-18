"use client";

/**
 * Settings Page — /settings
 *
 * Runtime configuration overrides for the bMAS server session.
 * All changes are session-only — restarting the container reverts
 * to bmas.yaml defaults.
 *
 * Sections:
 * 1. Complexity Routing — map tiers to LiteLLM model aliases
 * 2. Role Registry — control which host/profile each role uses
 *
 * UX features:
 * - Scrollbar at screen right edge (overflow on app-shell__main, not view-container)
 * - Global dirty ribbon when either section has unsaved changes
 * - Sticky section headers remain visible while scrolling through role cards
 * - Dirty count badges in each section header
 */

import React, { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  RefreshCw,
  RotateCcw,
  CheckCircle,
  AlertCircle,
  Terminal,
} from "lucide-react";
import dynamic from "next/dynamic";

const ComplexityRoutingEditor = dynamic(
  () => import("@/components/features/ComplexityRoutingEditor"),
  { ssr: false }
);

const RoleRegistryEditor = dynamic(
  () => import("@/components/features/RoleRegistryEditor"),
  { ssr: false }
);

// ── Types ─────────────────────────────────────────────────────────────────

interface EdgeNodeInfo {
  node_name: string;
  host: string;
  port: number;
  model: string;
}

interface ModelInfo {
  alias: string;
  provider: string;
  model: string;
  max_tokens: number | null;
  node_count?: number;
  edge_nodes?: EdgeNodeInfo[];
}

interface HostOption {
  host: string;
  name: string;
  role: string;
}

interface RegistryEntry {
  preferred_host: string | null;
  profile: string;
  dispatch_port: number;
  endpoints?: string[];
}

interface SettingsData {
  routing: Record<string, string>;
  role_registry: Record<string, RegistryEntry>;
  defaults: {
    routing: Record<string, string>;
    role_registry: Record<string, RegistryEntry>;
  };
}

interface SchemaData {
  complexity_tiers: string[];
  available_models: ModelInfo[];
  configured_hosts: HostOption[];
  known_roles: string[];
}

// ── Hook ──────────────────────────────────────────────────────────────────

function useSettings() {
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [schema, setSchema] = useState<SchemaData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [settingsRes, schemaRes] = await Promise.all([
        fetch("/api/settings"),
        fetch("/api/settings/schema"),
      ]);

      if (!settingsRes.ok) throw new Error(`Settings: ${settingsRes.status}`);
      if (!schemaRes.ok) throw new Error(`Schema: ${schemaRes.status}`);

      const [settingsData, schemaData] = await Promise.all([
        settingsRes.json() as Promise<SettingsData>,
        schemaRes.json() as Promise<SchemaData>,
      ]);

      setSettings(settingsData);
      setSchema(schemaData);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load settings");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  return { settings, schema, loading, error, reload: load };
}

// ── API endpoint chips ─────────────────────────────────────────────────────

const API_ENDPOINTS: { method: string; path: string }[] = [
  { method: "GET", path: "/settings" },
  { method: "PATCH", path: "/settings/routing" },
  { method: "PATCH", path: "/settings/role_registry" },
  { method: "POST", path: "/settings/reset" },
  { method: "GET", path: "/settings/schema" },
];

// ── Main Page ─────────────────────────────────────────────────────────────

export default function SettingsPage() {
  const { settings, schema, loading, error, reload } = useSettings();
  const [resetStatus, setResetStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [resetMsg, setResetMsg] = useState("");

  // Track dirty counts from each section for the global ribbon
  const [routingDirty, setRoutingDirty] = useState(0);
  const [registryDirty, setRegistryDirty] = useState(0);
  const totalDirty = routingDirty + registryDirty;

  // ── Save routing ──────────────────────────────────────────────────────
  const handleSaveRouting = useCallback(
    async (overrides: Record<string, string>) => {
      const res = await fetch("/api/settings/routing", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(overrides),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(data.detail ?? `HTTP ${res.status}`);
      }
      // Refresh to get authoritative state from server
      await reload();
    },
    [reload]
  );

  // ── Save role registry ────────────────────────────────────────────────
  const handleSaveRoleRegistry = useCallback(
    async (patch: Record<string, Partial<RegistryEntry>>) => {
      if (Object.keys(patch).length === 0) return;

      const res = await fetch("/api/settings/role_registry", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entries: patch }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(data.detail ?? `HTTP ${res.status}`);
      }
      await reload();
    },
    [reload]
  );

  // ── Reset all ─────────────────────────────────────────────────────────
  const handleResetAll = useCallback(async () => {
    setResetStatus("loading");
    setResetMsg("");
    try {
      const res = await fetch("/api/settings/reset", { method: "POST" });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(data.detail ?? `HTTP ${res.status}`);
      }
      await reload();
      setResetStatus("success");
      setResetMsg("All settings reset to bmas.yaml defaults");
      setTimeout(() => setResetStatus("idle"), 4000);
    } catch (e) {
      setResetStatus("error");
      setResetMsg(e instanceof Error ? e.message : "Reset failed");
    }
  }, [reload]);

  return (
    <div className="view-container settings-view">
      {/* ── Breadcrumb ────────────────────────────────────────────── */}
      <div className="settings-breadcrumb">
        <Link href="/" className="settings-back-link">
          <ArrowLeft size={13} />
          <span>Home</span>
        </Link>
        <span className="settings-breadcrumb__sep">/</span>
        <span className="settings-breadcrumb__current">Settings</span>
      </div>

      {/* ── Page header ───────────────────────────────────────────── */}
      <div className="settings-page-header">
        <div>
          <h1 className="settings-page-title">Runtime Settings</h1>
          <p className="settings-page-subtitle">
            Session-only configuration overrides. Changes apply immediately and persist until the
            server restarts, at which point{" "}
            <code className="settings-inline-code">bmas.yaml</code> defaults are restored.
          </p>
        </div>
        <div className="settings-page-header__actions">
          <button
            onClick={() => void reload()}
            className="settings-btn settings-btn--ghost"
            disabled={loading}
            title="Reload from server"
            id="refresh-settings-btn"
          >
            <RefreshCw size={14} className={loading ? "spin" : ""} />
            <span>Refresh</span>
          </button>
          <button
            onClick={() => void handleResetAll()}
            className={`settings-btn settings-btn--danger ${resetStatus === "loading" ? "settings-btn--loading" : ""}`}
            disabled={resetStatus === "loading" || loading}
            id="reset-all-settings-btn"
            title="Reset all session overrides to bmas.yaml defaults"
          >
            {resetStatus === "loading" ? (
              <span className="settings-spinner" aria-hidden="true" />
            ) : resetStatus === "success" ? (
              <CheckCircle size={14} />
            ) : (
              <RotateCcw size={14} />
            )}
            <span>
              {resetStatus === "loading"
                ? "Resetting…"
                : resetStatus === "success"
                  ? "Reset!"
                  : "Reset All to Defaults"}
            </span>
          </button>
        </div>
      </div>

      {/* ── Global status messages ─────────────────────────────────── */}
      {(resetStatus === "success" || resetStatus === "error") && resetMsg && (
        <div
          className={`settings-flash-banner settings-flash-banner--${resetStatus}`}
          role="alert"
        >
          {resetStatus === "success" ? <CheckCircle size={14} /> : <AlertCircle size={14} />}
          <span>{resetMsg}</span>
        </div>
      )}

      {/* Global dirty ribbon — visible when ANY section has unsaved changes */}
      {totalDirty > 0 && (
        <div className="settings-dirty-bar" role="status" aria-live="polite">
          <span className="settings-dirty-bar__dot" aria-hidden="true" />
          <span>
            {totalDirty} unsaved change{totalDirty !== 1 ? "s" : ""} — use{" "}
            <strong>Save</strong> in each section to apply, or{" "}
            <strong>Reset All to Defaults</strong> to discard everything.
          </span>
        </div>
      )}

      {/* ── Error state ───────────────────────────────────────────── */}
      {error && (
        <div className="settings-error-full" role="alert">
          <AlertCircle size={28} />
          <h3>Failed to load settings</h3>
          <p>{error}</p>
          <button className="settings-btn settings-btn--primary" onClick={() => void reload()}>
            <RefreshCw size={14} />
            Retry
          </button>
        </div>
      )}

      {/* ── Loading skeleton ───────────────────────────────────────── */}
      {loading && !error && (
        <div className="settings-skeleton" aria-busy="true" aria-label="Loading settings">
          <div className="shimmer settings-skeleton__section" />
          <div className="shimmer settings-skeleton__section" />
        </div>
      )}

      {/* ── Content ───────────────────────────────────────────────── */}
      {!loading && !error && settings && schema && (
        <div className="settings-content">
          <ComplexityRoutingEditor
            routing={settings.routing}
            defaultRouting={settings.defaults.routing}
            availableModels={schema.available_models}
            onSave={handleSaveRouting}
            onDirtyChange={setRoutingDirty}
          />

          <RoleRegistryEditor
            roleRegistry={settings.role_registry}
            defaultRegistry={settings.defaults.role_registry}
            configuredHosts={schema.configured_hosts}
            onSave={handleSaveRoleRegistry}
            onDirtyChange={setRegistryDirty}
          />
        </div>
      )}

      {/* ── API reference footer ───────────────────────────────────── */}
      <div className="settings-api-note" aria-label="API reference">
        <div className="settings-api-note__header">
          <Terminal size={13} aria-hidden="true" style={{ color: "var(--text-tertiary)" }} />
          <span className="settings-api-note__label">REST API</span>
        </div>
        <div className="settings-api-note__endpoints">
          {API_ENDPOINTS.map((ep) => (
            <span key={`${ep.method}-${ep.path}`} className="settings-api-note__chip">
              <span
                className={`settings-api-note__method settings-api-note__method--${ep.method.toLowerCase()}`}
              >
                {ep.method}
              </span>
              {ep.path}
            </span>
          ))}
        </div>
        <p className="settings-api-note__tip">
          Pass <code>overrides</code> in <code>POST /submit</code> to apply settings for a single
          task only — these are not persisted to the session store.
        </p>
      </div>
    </div>
  );
}
