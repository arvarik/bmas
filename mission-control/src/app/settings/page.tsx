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

import React, { useState, useEffect, useCallback, useMemo } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  RefreshCw,
  RotateCcw,
  CheckCircle,
  AlertCircle,
  Terminal,
  Copy,
} from "lucide-react";
import dynamic from "next/dynamic";
import { SettingsChangeDialog } from "@/components/ui/SettingsChangeDialog";
import { buildYamlPatch, getResetChanges } from "@/lib/settings-presentation";

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

class SettingsLoadError extends Error {
  summary: string;
  detail: string;
  status: number | null;
  timestamp: string;

  constructor(summary: string, detail: string, status: number | null) {
    super(detail);
    this.name = "SettingsLoadError";
    this.summary = summary;
    this.detail = detail;
    this.status = status;
    this.timestamp = new Date().toISOString();
  }
}

interface ApiErrorBody {
  detail?: string;
  error?: string;
}

const SETTINGS_REQUEST_TIMEOUT_MS = 12_000;

async function fetchSettingsResource(path: string, init?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), SETTINGS_REQUEST_TIMEOUT_MS);

  try {
    return await fetch(path, { ...init, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error("The settings service did not respond within 12 seconds.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function readApiError(response: Response, resource: string): Promise<SettingsLoadError> {
  const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
  const permissionFailure = response.status === 401 || response.status === 403;
  const summary = permissionFailure
    ? `You do not have permission to read ${resource}.`
    : `The daemon did not provide ${resource}.`;
  const detail = body.detail || body.error || `The request returned HTTP ${response.status}.`;

  return new SettingsLoadError(summary, detail, response.status);
}

// ── Hook ──────────────────────────────────────────────────────────────────

function useSettings() {
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [schema, setSchema] = useState<SchemaData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<SettingsLoadError | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [settingsRes, schemaRes] = await Promise.all([
        fetchSettingsResource("/api/settings"),
        fetchSettingsResource("/api/settings/schema"),
      ]);

      if (!settingsRes.ok) throw await readApiError(settingsRes, "runtime settings");
      if (!schemaRes.ok) throw await readApiError(schemaRes, "the settings schema");

      const [settingsData, schemaData] = await Promise.all([
        settingsRes.json() as Promise<SettingsData>,
        schemaRes.json() as Promise<SchemaData>,
      ]);

      setSettings(settingsData);
      setSchema(schemaData);
    } catch (e) {
      setSettings(null);
      setSchema(null);
      if (e instanceof SettingsLoadError) {
        setError(e);
      } else {
        const detail = e instanceof Error ? e.message : "The settings request failed.";
        setError(new SettingsLoadError("The settings service is unavailable.", detail, null));
      }
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
  const [showResetConfirmation, setShowResetConfirmation] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"idle" | "success" | "error">("idle");
  const [diagnosticsCopyStatus, setDiagnosticsCopyStatus] = useState<
    "idle" | "success" | "error"
  >("idle");

  // Track dirty counts from each section for the global ribbon
  const [routingDirty, setRoutingDirty] = useState(0);
  const [registryDirty, setRegistryDirty] = useState(0);
  const totalDirty = routingDirty + registryDirty;
  const resetChanges = useMemo(() => (settings ? getResetChanges(settings) : []), [settings]);
  const hasSessionOverrides = resetChanges.length > 0;

  // ── Save routing ──────────────────────────────────────────────────────
  const handleSaveRouting = useCallback(
    async (overrides: Record<string, string>) => {
      const res = await fetchSettingsResource("/api/settings/routing", {
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

      const res = await fetchSettingsResource("/api/settings/role_registry", {
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
      const res = await fetchSettingsResource("/api/settings/reset", { method: "POST" });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(data.detail ?? `HTTP ${res.status}`);
      }
      await reload();
      setShowResetConfirmation(false);
      setResetStatus("success");
      setResetMsg("All settings reset to bmas.yaml defaults");
      setTimeout(() => setResetStatus("idle"), 4000);
    } catch (e) {
      setShowResetConfirmation(false);
      setResetStatus("error");
      setResetMsg(e instanceof Error ? e.message : "Reset failed");
    }
  }, [reload]);

  const handleCopyYaml = useCallback(async () => {
    if (!settings || !hasSessionOverrides) return;
    try {
      await navigator.clipboard.writeText(buildYamlPatch(settings));
      setCopyStatus("success");
      window.setTimeout(() => setCopyStatus("idle"), 2500);
    } catch {
      setCopyStatus("error");
    }
  }, [hasSessionOverrides, settings]);

  const handleCopyDiagnostics = useCallback(async () => {
    if (!error) return;
    const diagnostics = [
      error.summary,
      error.detail,
      error.status ? `HTTP status: ${error.status}` : null,
      `Time: ${error.timestamp}`,
      `Page: ${window.location.href}`,
    ]
      .filter(Boolean)
      .join("\n");
    try {
      await navigator.clipboard.writeText(diagnostics);
      setDiagnosticsCopyStatus("success");
    } catch {
      setDiagnosticsCopyStatus("error");
    }
    window.setTimeout(() => setDiagnosticsCopyStatus("idle"), 2500);
  }, [error]);

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
            onClick={() => setShowResetConfirmation(true)}
            className={`settings-btn settings-btn--danger ${resetStatus === "loading" ? "settings-btn--loading" : ""}`}
            disabled={resetStatus === "loading" || loading || !settings || !hasSessionOverrides}
            id="reset-all-settings-btn"
            title={hasSessionOverrides
              ? "Reset all session overrides to bmas.yaml defaults"
              : "No active session overrides"}
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
            {totalDirty} unsaved change{totalDirty !== 1 ? "s" : ""}. Use{" "}
            <strong>Save</strong> in each section to review and apply the changes. Use each
            section&apos;s <strong>Reset</strong> control to discard its local changes.
          </span>
        </div>
      )}

      {/* ── Error state ───────────────────────────────────────────── */}
      {error && (
        <div className="settings-error-full" role="region" aria-labelledby="settings-error-title">
          <AlertCircle size={28} />
          <h3 id="settings-error-title">Settings are unavailable</h3>
          <p>{error.summary}</p>
          <div className="settings-error-full__actions">
            <button className="settings-btn settings-btn--primary" onClick={() => void reload()}>
              <RefreshCw size={14} />
              Retry
            </button>
            <Link href="/infra" className="settings-btn settings-btn--ghost">
              Open Operations
            </Link>
            <button
              className="settings-btn settings-btn--ghost"
              onClick={() => void handleCopyDiagnostics()}
            >
              <Copy size={13} />
              {diagnosticsCopyStatus === "success"
                ? "Copied"
                : diagnosticsCopyStatus === "error"
                  ? "Copy failed"
                  : "Copy diagnostics"}
            </button>
          </div>
          <details className="settings-error-full__details">
            <summary>Technical details</summary>
            <code>{error.detail}</code>
            <time dateTime={error.timestamp}>{new Date(error.timestamp).toLocaleString()}</time>
          </details>
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
          <div className="settings-persistence-note" role="note">
            <div>
              <strong>These settings last for this server session.</strong>
              <span>Copy the active overrides into bmas.yaml to keep them after a restart.</span>
            </div>
            <button
              type="button"
              className="settings-btn settings-btn--ghost settings-btn--sm"
              onClick={() => void handleCopyYaml()}
              disabled={!hasSessionOverrides}
              title={hasSessionOverrides
                ? "Copy the active session overrides as YAML"
                : "No active session overrides"}
            >
              <Copy size={13} />
              {copyStatus === "success"
                ? "YAML copied"
                : copyStatus === "error"
                  ? "Copy failed"
                  : "Copy YAML patch"}
            </button>
          </div>

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

      <SettingsChangeDialog
        open={showResetConfirmation}
        title="Reset active session overrides?"
        description={`This action resets ${resetChanges.length} active override${resetChanges.length === 1 ? "" : "s"}. It does not edit bmas.yaml.`}
        changes={resetChanges}
        confirmLabel="Reset overrides"
        busy={resetStatus === "loading"}
        danger
        onCancel={() => setShowResetConfirmation(false)}
        onConfirm={() => void handleResetAll()}
      />
    </div>
  );
}
