"use client";

/**
 * Settings — /settings
 *
 * Sections (left navigation, `?section=` in the URL):
 *   models     Complexity tier → model alias (daemon session override)
 *   agents     Role registry: enabled, host, profile, port (daemon session override)
 *   runtime    Classic limits, roster, and control unit (daemon session override)
 *   blackboard Board view budget and cleaner (daemon session override)
 *   workspace  Browser-local preferences and local data
 *   system     Read-only stack facts, YAML export, reset
 *
 * Daemon-backed sections share one draft. The save bar lists every unsaved
 * change and applies them with one action. Saved values that differ from
 * bmas.yaml carry a "Session" pill because they reset when the daemon restarts.
 */

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import {
  Bot,
  Check,
  ChevronDown,
  Copy,
  Cpu,
  Database,
  LayoutDashboard,
  Layers,
  RefreshCw,
  RotateCcw,
  Server,
  Trash2,
  X,
} from "lucide-react";
import { SelectMenu, type SelectOption } from "@/components/ui/SelectMenu";
import { SettingsChangeDialog } from "@/components/ui/SettingsChangeDialog";
import { ResourceState } from "@/components/ui/ResourceState";
import { Skeleton } from "@/components/ui/Skeleton";
import {
  NumberField,
  SegmentedControl,
  SettingsCard,
  SettingsRow,
  TextField,
  Toggle,
} from "@/components/settings/controls";
import { useReadiness } from "@/contexts/ReadinessContext";
import { useToast } from "@/hooks/useToast";
import { parseCapabilities, type VariantCapability } from "@/lib/capabilities";
import { supportsMissionControlVariant } from "@/lib/variant-support";
import { parseSavedTaskViews } from "@/lib/task-history-presentation";
import { clearLocalData, LOCAL_DATA_KEYS, usePreferences } from "@/lib/preferences";
import {
  buildSavePayload,
  buildYamlPatch,
  diffDraft,
  draftFromSnapshot,
  getResetChanges,
  sessionOverrideKeys,
  type ClassicFieldMeta,
  type SettingsDraft,
  type SettingsRegistryEntry,
  type SettingsSnapshot,
} from "@/lib/settings-presentation";

// ── Types ─────────────────────────────────────────────────────────────

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

interface SchemaData {
  complexity_tiers: string[];
  available_models: ModelInfo[];
  configured_hosts: HostOption[];
  known_roles: string[];
  classic_fields?: ClassicFieldMeta[];
}

type SectionId = "models" | "agents" | "runtime" | "blackboard" | "workspace" | "system";

const SECTIONS: { id: SectionId; label: string; icon: typeof Cpu; scope: "daemon" | "browser" | "readonly" }[] = [
  { id: "models", label: "Models", icon: Cpu, scope: "daemon" },
  { id: "agents", label: "Agents & roles", icon: Bot, scope: "daemon" },
  { id: "runtime", label: "Runtime", icon: Layers, scope: "daemon" },
  { id: "blackboard", label: "Blackboard", icon: Database, scope: "daemon" },
  { id: "workspace", label: "Workspace", icon: LayoutDashboard, scope: "browser" },
  { id: "system", label: "System", icon: Server, scope: "readonly" },
];

const TIER_META: Record<string, { label: string; description: string }> = {
  simple: { label: "Simple", description: "Factual lookups, unit conversions, single-step operations." },
  light: { label: "Light", description: "Short extractions, regex, one to three sentence summaries." },
  medium: { label: "Medium", description: "Single-function code, focused explanations, drafts." },
  complex: { label: "Complex", description: "Architecture, multi-component systems, research synthesis." },
};

const ROLE_META: Record<string, { description: string }> = {
  planner: { description: "Splits the task and identifies the evidence each round needs." },
  expert: { description: "Supplies domain work. The agent generator creates one expert per slot." },
  critic: { description: "Finds unsupported claims, gaps, and defects on the board." },
  conflict_resolver: { description: "Resolves incompatible contributions between entries." },
  cleaner: { description: "Condenses or removes low-value board content." },
  decider: { description: "Selects and verifies the final answer." },
  universal: { description: "Fallback role for load-balanced dispatch." },
};

const ROLE_ORDER = ["planner", "expert", "critic", "conflict_resolver", "cleaner", "decider", "universal"];
const TIER_ORDER = ["simple", "light", "medium", "complex"];
const REQUEST_TIMEOUT_MS = 12_000;

const OPTION_LABELS: Record<string, string> = {
  llm: "LLM",
  heuristic_first: "Heuristic first",
  auto: "Auto",
  exact: "Exact match",
  embedding: "Embedding",
  judge: "LLM judge",
  concurrent: "Concurrent",
  sequential: "Sequential",
};

function titleCase(value: string): string {
  return OPTION_LABELS[value] ?? value.replace(/_/g, " ").replace(/^\w/, (letter) => letter.toUpperCase());
}

function readSection(value: string | null): SectionId {
  return SECTIONS.some((section) => section.id === value) ? (value as SectionId) : "models";
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  return fetch(path, { ...init, cache: "no-store", signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
}

async function readError(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => ({}))) as { detail?: string; error?: string };
  return body.detail || body.error || `${fallback} (HTTP ${response.status})`;
}

// ── Page ──────────────────────────────────────────────────────────────

export function SettingsPageClient() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const section = readSection(searchParams.get("section"));
  const { toast } = useToast();

  const [snapshot, setSnapshot] = useState<SettingsSnapshot | null>(null);
  const [schema, setSchema] = useState<SchemaData | null>(null);
  const [variants, setVariants] = useState<VariantCapability[]>([]);
  const [draft, setDraft] = useState<SettingsDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [reviewOpen, setReviewOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetting, setResetting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const [settingsRes, schemaRes, capabilitiesRes] = await Promise.all([
        request("/api/settings"),
        request("/api/settings/schema"),
        request("/api/capabilities"),
      ]);
      if (!settingsRes.ok) throw new Error(await readError(settingsRes, "The daemon did not provide settings"));
      if (!schemaRes.ok) throw new Error(await readError(schemaRes, "The daemon did not provide the settings schema"));
      const nextSnapshot = (await settingsRes.json()) as SettingsSnapshot;
      const nextSchema = (await schemaRes.json()) as SchemaData;
      setSnapshot(nextSnapshot);
      setSchema(nextSchema);
      setDraft(draftFromSnapshot(nextSnapshot));
      if (capabilitiesRes.ok) {
        try {
          setVariants(parseCapabilities(await capabilitiesRes.json()).variants);
        } catch {
          setVariants([]);
        }
      }
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "The settings service is unavailable.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  const classicFields = useMemo(() => schema?.classic_fields ?? [], [schema]);
  const changes = useMemo(
    () => (snapshot && draft ? diffDraft(snapshot, draft, classicFields) : []),
    [classicFields, draft, snapshot],
  );
  const overrides = useMemo(() => (snapshot ? sessionOverrideKeys(snapshot) : new Set<string>()), [snapshot]);
  const changedKeys = useMemo(() => new Set(changes.map((change) => `${change.section}.${change.key}`)), [changes]);
  const dirtySections = useMemo(() => {
    const set = new Set<SectionId>();
    for (const change of changes) {
      if (change.section === "routing") set.add("models");
      else if (change.section === "role_registry") set.add("agents");
      else {
        const field = classicFields.find((entry) => entry.key === change.key);
        set.add(field?.group === "board" ? "blackboard" : "runtime");
      }
    }
    return set;
  }, [changes, classicFields]);

  const selectSection = (next: SectionId) => {
    const params = new URLSearchParams(searchParams.toString());
    if (next === "models") params.delete("section");
    else params.set("section", next);
    window.history.replaceState(null, "", `${pathname}${params.size ? `?${params.toString()}` : ""}`);
    setReviewOpen(false);
  };

  const updateDraft = (mutate: (current: SettingsDraft) => SettingsDraft) => {
    setDraft((current) => (current ? mutate(current) : current));
    setSaveError("");
  };

  const discard = () => {
    if (snapshot) setDraft(draftFromSnapshot(snapshot));
    setSaveError("");
    setReviewOpen(false);
  };

  const save = async () => {
    if (!snapshot || !draft || changes.length === 0) return;
    setSaving(true);
    setSaveError("");
    const payload = buildSavePayload(snapshot, draft);
    try {
      if (payload.routing) {
        const res = await request("/api/settings/routing", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload.routing),
        });
        if (!res.ok) throw new Error(await readError(res, "Routing save failed"));
      }
      if (payload.role_registry) {
        const res = await request("/api/settings/role_registry", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entries: payload.role_registry }),
        });
        if (!res.ok) throw new Error(await readError(res, "Role registry save failed"));
      }
      if (payload.classic) {
        const res = await request("/api/settings/classic", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload.classic),
        });
        if (!res.ok) throw new Error(await readError(res, "Runtime settings save failed"));
      }
      await load();
      setReviewOpen(false);
      toast({ type: "success", message: `Saved ${changes.length} setting${changes.length === 1 ? "" : "s"}. New tasks use them now.` });
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "The save failed.");
      // Keep the draft so the operator can correct it; refresh the saved state.
      const current = draft;
      await load();
      setDraft(current);
    } finally {
      setSaving(false);
    }
  };

  const resetAll = async () => {
    setResetting(true);
    try {
      const res = await request("/api/settings/reset", { method: "POST" });
      if (!res.ok) throw new Error(await readError(res, "Reset failed"));
      await load();
      setResetOpen(false);
      toast({ type: "success", message: "All session overrides were reset to bmas.yaml." });
    } catch (error) {
      toast({ type: "error", message: error instanceof Error ? error.message : "Reset failed." });
    } finally {
      setResetting(false);
    }
  };

  const copyYaml = async () => {
    if (!snapshot) return;
    try {
      await navigator.clipboard.writeText(buildYamlPatch(snapshot));
      toast({ type: "success", message: "YAML patch copied. Merge it into bmas.yaml and restart." });
    } catch {
      toast({ type: "error", message: "Mission Control could not copy the YAML patch." });
    }
  };

  const resetChanges = useMemo(() => (snapshot ? getResetChanges(snapshot) : []), [snapshot]);

  return (
    <div className="settings">
      <header className="settings__header">
        <div>
          <p className="page-eyebrow">Configure</p>
          <h2>Settings</h2>
          <p className="settings__lede">
            Daemon settings apply to new tasks right away and last until the daemon restarts.
            Workspace settings stay in this browser.
          </p>
        </div>
        <div className="settings__header-actions">
          {overrides.size > 0 ? (
            <span className="settings-pill settings-pill--session settings-pill--lg">
              {overrides.size} session override{overrides.size === 1 ? "" : "s"}
            </span>
          ) : null}
          <button type="button" className="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={14} className={loading ? "spin" : undefined} aria-hidden="true" /> Refresh
          </button>
        </div>
      </header>

      <div className="settings__layout">
        <nav className="settings-nav" aria-label="Settings sections">
          {SECTIONS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                type="button"
                className={`settings-nav__item ${section === item.id ? "settings-nav__item--active" : ""}`}
                aria-current={section === item.id ? "page" : undefined}
                onClick={() => selectSection(item.id)}
              >
                <Icon size={16} aria-hidden="true" />
                <span>{item.label}</span>
                {dirtySections.has(item.id) ? <span className="settings-nav__dot" aria-label="Unsaved changes" /> : null}
              </button>
            );
          })}
          <p className="settings-nav__hint">
            <span className="settings-pill settings-pill--session">Session</span> marks a value that differs from bmas.yaml.
          </p>
        </nav>

        <div className="settings__content">
          {loadError ? (
            <ResourceState
              kind="unavailable"
              title="Settings are unavailable"
              description={loadError}
              detail="Run ./scripts/bmas doctor for exact checks."
              onRetry={load}
              operationsHref="/infra"
            />
          ) : loading && !snapshot ? (
            <SettingsCard><Skeleton variant="list" lines={6} /></SettingsCard>
          ) : snapshot && draft && schema ? (
            <>
              {section === "models" ? (
                <ModelsSection snapshot={snapshot} draft={draft} schema={schema} overrides={overrides} changedKeys={changedKeys} onChange={updateDraft} />
              ) : null}
              {section === "agents" ? (
                <AgentsSection snapshot={snapshot} draft={draft} schema={schema} overrides={overrides} changedKeys={changedKeys} onChange={updateDraft} />
              ) : null}
              {section === "runtime" ? (
                <ClassicSection
                  title="Runtime"
                  lede="Limits and control-unit behaviour for the classic blackboard runtime."
                  groups={[
                    { id: "limits", title: "Limits", description: "Stop conditions for one task." },
                    { id: "roster", title: "Roster", description: "Experts the agent generator creates for each complexity tier." },
                    { id: "control", title: "Control unit", description: "How agents are selected and how a stalled board recovers." },
                  ]}
                  snapshot={snapshot}
                  draft={draft}
                  fields={classicFields}
                  overrides={overrides}
                  changedKeys={changedKeys}
                  onChange={updateDraft}
                />
              ) : null}
              {section === "blackboard" ? (
                <ClassicSection
                  title="Blackboard"
                  lede="How much board context each agent sees, and when the cleaner condenses it."
                  groups={[{ id: "board", title: "Board", description: "View budget and cleaner thresholds." }]}
                  snapshot={snapshot}
                  draft={draft}
                  fields={classicFields}
                  overrides={overrides}
                  changedKeys={changedKeys}
                  onChange={updateDraft}
                />
              ) : null}
              {section === "workspace" ? <WorkspaceSection variants={variants} /> : null}
              {section === "system" ? (
                <SystemSection
                  snapshot={snapshot}
                  schema={schema}
                  variants={variants}
                  overrideCount={overrides.size}
                  onCopyYaml={() => void copyYaml()}
                  onReset={() => setResetOpen(true)}
                />
              ) : null}
            </>
          ) : null}
        </div>
      </div>

      {changes.length > 0 ? (
        <div className="settings-savebar" role="region" aria-label="Unsaved changes">
          {reviewOpen ? (
            <ul className="settings-savebar__review">
              {changes.map((change) => (
                <li key={`${change.section}.${change.key}`}>
                  <span>{change.label}</span>
                  <code>{change.before}</code>
                  <span aria-hidden="true">→</span>
                  <code>{change.after}</code>
                </li>
              ))}
            </ul>
          ) : null}
          {saveError ? <p className="settings-savebar__error" role="alert">{saveError}</p> : null}
          <div className="settings-savebar__row">
            <button type="button" className="settings-savebar__toggle" onClick={() => setReviewOpen((open) => !open)} aria-expanded={reviewOpen}>
              <strong>{changes.length} unsaved change{changes.length === 1 ? "" : "s"}</strong>
              <ChevronDown size={14} aria-hidden="true" style={{ transform: reviewOpen ? "rotate(180deg)" : undefined }} />
            </button>
            <div className="settings-savebar__actions">
              <button type="button" className="button" onClick={discard} disabled={saving}>
                <X size={14} aria-hidden="true" /> Discard
              </button>
              <button type="button" className="button button--primary" onClick={() => void save()} disabled={saving}>
                {saving ? <span className="spin settings-spinner" aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
                {saving ? "Saving…" : "Save changes"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <SettingsChangeDialog
        open={resetOpen}
        title="Reset all session overrides?"
        description="Every daemon setting returns to its bmas.yaml value. Running tasks keep the configuration they started with."
        changes={resetChanges}
        confirmLabel="Reset all"
        busy={resetting}
        danger
        onCancel={() => setResetOpen(false)}
        onConfirm={() => void resetAll()}
      />
    </div>
  );
}

// ── Section: models ───────────────────────────────────────────────────

interface DaemonSectionProps {
  snapshot: SettingsSnapshot;
  draft: SettingsDraft;
  schema: SchemaData;
  overrides: Set<string>;
  changedKeys: Set<string>;
  onChange: (mutate: (current: SettingsDraft) => SettingsDraft) => void;
}

function modelOptions(models: ModelInfo[]): SelectOption[] {
  return models.map((model) => ({
    value: model.alias,
    label: model.alias,
    description: model.provider === "local"
      ? `${model.model} · ${model.node_count ?? 0} edge node${model.node_count === 1 ? "" : "s"}`
      : `${model.provider} · ${model.model}${model.max_tokens ? ` · ${model.max_tokens.toLocaleString()} max out` : ""}`,
  }));
}

function ModelsSection({ snapshot, draft, schema, overrides, changedKeys, onChange }: DaemonSectionProps) {
  const options = useMemo(() => modelOptions(schema.available_models), [schema.available_models]);
  const tiers = TIER_ORDER.filter((tier) => schema.complexity_tiers.includes(tier));
  return (
    <>
      <SectionHeading title="Models" lede="Triage assigns each task one complexity tier. Each tier routes to one LiteLLM model alias." />
      <SettingsCard title="Routing by complexity" description="Changes apply to the next submitted task.">
        {tiers.map((tier) => {
          const meta = TIER_META[tier] ?? { label: titleCase(tier), description: "" };
          const selected = schema.available_models.find((model) => model.alias === draft.routing[tier]);
          return (
            <SettingsRow
              key={tier}
              htmlFor={`routing-${tier}`}
              label={meta.label}
              description={meta.description}
              overridden={overrides.has(`routing.${tier}`)}
              changed={changedKeys.has(`routing.${tier}`)}
              onReset={() => onChange((current) => ({ ...current, routing: { ...current.routing, [tier]: snapshot.defaults.routing[tier] ?? current.routing[tier] } }))}
              control={(
                <div className="settings-stack">
                  <SelectMenu
                    id={`routing-${tier}`}
                    value={draft.routing[tier] ?? ""}
                    options={options}
                    placeholder="Select a model"
                    className="settings-select"
                    onChange={(value) => onChange((current) => ({ ...current, routing: { ...current.routing, [tier]: value } }))}
                  />
                  {selected ? (
                    <span className="settings-meta">
                      <span className={`settings-chip settings-chip--${selected.provider === "local" ? "local" : "cloud"}`}>{selected.provider}</span>
                      {selected.provider === "local"
                        ? `${selected.node_count ?? 0} edge node${selected.node_count === 1 ? "" : "s"} · ${selected.model}`
                        : `${selected.model}${selected.max_tokens ? ` · ${selected.max_tokens.toLocaleString()} max output tokens` : ""}`}
                    </span>
                  ) : null}
                </div>
              )}
            />
          );
        })}
      </SettingsCard>
      <SettingsCard title="Available models" description="Aliases come from the models section of bmas.yaml. Add or change them there.">
        <div className="settings-table-wrap">
          <table className="settings-table">
            <thead><tr><th>Alias</th><th>Provider</th><th>Model</th><th>Max output</th></tr></thead>
            <tbody>
              {schema.available_models.map((model) => (
                <tr key={model.alias}>
                  <td><code>{model.alias}</code></td>
                  <td><span className={`settings-chip settings-chip--${model.provider === "local" ? "local" : "cloud"}`}>{model.provider}</span></td>
                  <td>
                    {model.model}
                    {model.edge_nodes?.length ? (
                      <span className="settings-table__sub">{model.edge_nodes.map((node) => `${node.node_name} ${node.host}:${node.port}`).join(" · ")}</span>
                    ) : null}
                  </td>
                  <td>{model.max_tokens ? model.max_tokens.toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SettingsCard>
    </>
  );
}

// ── Section: agents ───────────────────────────────────────────────────

function AgentsSection({ snapshot, draft, schema, overrides, changedKeys, onChange }: DaemonSectionProps) {
  const roles = useMemo(() => {
    const known = new Set([...schema.known_roles, ...Object.keys(draft.role_registry)]);
    return [...ROLE_ORDER.filter((role) => known.has(role)), ...[...known].filter((role) => !ROLE_ORDER.includes(role)).sort()];
  }, [draft.role_registry, schema.known_roles]);
  const hostOptions: SelectOption[] = useMemo(() => [
    { value: "any", label: "Any host", description: "Load-balanced across every execution node" },
    ...schema.configured_hosts.map((host) => ({ value: host.host, label: host.name, description: `${host.host} · ${titleCase(host.role)}` })),
  ], [schema.configured_hosts]);

  const setRole = (role: string, patch: Partial<SettingsRegistryEntry>) => onChange((current) => ({
    ...current,
    role_registry: { ...current.role_registry, [role]: { ...current.role_registry[role], ...patch } },
  }));
  const resetField = (role: string, field: keyof SettingsRegistryEntry) => {
    const base = snapshot.defaults.role_registry[role];
    if (!base) return;
    setRole(role, { [field]: base[field] } as Partial<SettingsRegistryEntry>);
  };

  return (
    <>
      <SectionHeading title="Agents & roles" lede="Each classic role dispatches to one Hermes profile on an execution node. Disable a role to keep the control unit from selecting it." />
      {roles.map((role) => {
        const entry = draft.role_registry[role];
        if (!entry) return null;
        const enabled = entry.enabled ?? true;
        return (
          <SettingsCard
            key={role}
            id={`role-${role}`}
            title={titleCase(role)}
            description={ROLE_META[role]?.description ?? "Custom role"}
            actions={(
              <label className="settings-inline-toggle">
                <span>{enabled ? "Enabled" : "Disabled"}</span>
                <Toggle checked={enabled} onChange={(next) => setRole(role, { enabled: next })} label={`${titleCase(role)} enabled`} />
              </label>
            )}
          >
            <SettingsRow
              htmlFor={`role-${role}-host`}
              label="Preferred host"
              description="The node that runs this role when it is available."
              overridden={overrides.has(`role_registry.${role}.preferred_host`)}
              changed={changedKeys.has(`role_registry.${role}.preferred_host`)}
              onReset={() => resetField(role, "preferred_host")}
              control={(
                <SelectMenu
                  id={`role-${role}-host`}
                  value={entry.preferred_host ?? "any"}
                  options={hostOptions}
                  className="settings-select"
                  disabled={!enabled}
                  onChange={(value) => setRole(role, { preferred_host: value === "any" ? null : value })}
                />
              )}
            />
            <SettingsRow
              htmlFor={`role-${role}-profile`}
              label="Hermes profile"
              description="Profile directory name on the execution node."
              overridden={overrides.has(`role_registry.${role}.profile`)}
              changed={changedKeys.has(`role_registry.${role}.profile`)}
              onReset={() => resetField(role, "profile")}
              control={<TextField id={`role-${role}-profile`} value={entry.profile} mono disabled={!enabled} onChange={(value) => setRole(role, { profile: value })} />}
            />
            <SettingsRow
              htmlFor={`role-${role}-port`}
              label="Dispatch port"
              description="Agent API port on the node."
              overridden={overrides.has(`role_registry.${role}.dispatch_port`)}
              changed={changedKeys.has(`role_registry.${role}.dispatch_port`)}
              onReset={() => resetField(role, "dispatch_port")}
              control={<NumberField id={`role-${role}-port`} value={entry.dispatch_port} min={1} max={65535} width="sm" disabled={!enabled} onChange={(value) => setRole(role, { dispatch_port: typeof value === "number" ? value : entry.dispatch_port })} />}
            />
            {entry.endpoints?.length ? (
              <div className="settings-endpoints">
                <span>Endpoints</span>
                {entry.endpoints.map((endpoint) => <code key={endpoint}>{endpoint}</code>)}
              </div>
            ) : null}
          </SettingsCard>
        );
      })}
    </>
  );
}

// ── Section: classic runtime (runtime + blackboard) ───────────────────

function ClassicSection({
  title,
  lede,
  groups,
  snapshot,
  draft,
  fields,
  overrides,
  changedKeys,
  onChange,
}: {
  title: string;
  lede: string;
  groups: { id: ClassicFieldMeta["group"]; title: string; description: string }[];
  snapshot: SettingsSnapshot;
  draft: SettingsDraft;
  fields: ClassicFieldMeta[];
  overrides: Set<string>;
  changedKeys: Set<string>;
  onChange: (mutate: (current: SettingsDraft) => SettingsDraft) => void;
}) {
  const setValue = (key: string, value: unknown) => onChange((current) => ({
    ...current,
    classic: { ...current.classic, [key]: value },
  }));
  const resetKey = (key: string) => setValue(key, snapshot.defaults.classic?.[key]);

  if (fields.length === 0) {
    return (
      <>
        <SectionHeading title={title} lede={lede} />
        <ResourceState kind="unavailable" title="Runtime settings need a newer daemon" description="The daemon did not publish the classic settings schema. Update the daemon to edit these values here." compact />
      </>
    );
  }

  return (
    <>
      <SectionHeading title={title} lede={lede} />
      {groups.map((group) => (
        <SettingsCard key={group.id} title={group.title} description={group.description}>
          {fields.filter((field) => field.group === group.id).map((field) => {
            const value = draft.classic[field.key];
            const id = `classic-${field.key}`;
            const common = {
              label: field.label,
              description: `${field.description}${field.min !== undefined && field.max !== undefined && (field.type === "integer" || field.type === "number") ? ` Range ${field.min}–${field.max}${field.unit ? ` ${field.unit}` : ""}.` : ""}`,
              overridden: overrides.has(`classic.${field.key}`),
              changed: changedKeys.has(`classic.${field.key}`),
              onReset: () => resetKey(field.key),
            };
            if (field.type === "boolean") {
              return <SettingsRow key={field.key} {...common} control={<Toggle id={id} checked={value === true} onChange={(next) => setValue(field.key, next)} label={field.label} />} />;
            }
            if (field.type === "enum" && field.options) {
              const options = field.options.map((option) => ({ value: option, label: titleCase(option) }));
              return (
                <SettingsRow
                  key={field.key}
                  {...common}
                  control={options.length <= 3
                    ? <SegmentedControl value={String(value)} options={options} aria-label={field.label} onChange={(next) => setValue(field.key, next)} />
                    : <SelectMenu id={id} value={String(value)} options={options} className="settings-select" aria-label={field.label} onChange={(next) => setValue(field.key, next)} />}
                />
              );
            }
            if (field.type === "tier_map" || field.type === "weight_map") {
              const map = (value && typeof value === "object" ? value : {}) as Record<string, number>;
              const keys = field.type === "tier_map" ? TIER_ORDER : Object.keys(snapshot.defaults.classic?.[field.key] as Record<string, number> ?? map);
              return (
                <SettingsRow
                  key={field.key}
                  {...common}
                  align="start"
                  control={(
                    <div className="settings-map">
                      {keys.map((entry) => (
                        <label key={entry} className="settings-map__cell">
                          <span>{titleCase(entry)}</span>
                          <NumberField
                            value={typeof map[entry] === "number" ? map[entry] : ""}
                            min={field.min}
                            max={field.max}
                            step={field.step ?? 1}
                            width="sm"
                            aria-label={`${field.label} ${entry}`}
                            onChange={(next) => setValue(field.key, { ...map, [entry]: typeof next === "number" ? next : 0 })}
                          />
                        </label>
                      ))}
                    </div>
                  )}
                />
              );
            }
            return (
              <SettingsRow
                key={field.key}
                {...common}
                htmlFor={id}
                control={(
                  <NumberField
                    id={id}
                    value={typeof value === "number" ? value : ""}
                    min={field.min}
                    max={field.max}
                    step={field.step ?? (field.type === "number" ? 0.01 : 1)}
                    unit={field.unit}
                    onChange={(next) => setValue(field.key, typeof next === "number" ? next : snapshot.classic?.[field.key])}
                  />
                )}
              />
            );
          })}
        </SettingsCard>
      ))}
    </>
  );
}

// ── Section: workspace (browser) ──────────────────────────────────────

function WorkspaceSection({ variants }: { variants: VariantCapability[] }) {
  const [preferences, setPreferences] = usePreferences();
  const { toast } = useToast();
  const [confirmClear, setConfirmClear] = useState(false);
  const savedViewsRaw = useSyncExternalStore(
    (callback) => subscribeLocal(LOCAL_DATA_KEYS.savedViews, "bmas-task-views-changed", callback),
    () => window.localStorage.getItem(LOCAL_DATA_KEYS.savedViews) ?? "[]",
    () => "[]",
  );
  const pinsRaw = useSyncExternalStore(
    (callback) => subscribeLocal(LOCAL_DATA_KEYS.pins, "bmas-pins-changed", callback),
    () => window.localStorage.getItem(LOCAL_DATA_KEYS.pins) ?? "[]",
    () => "[]",
  );
  const savedViews = useMemo(() => parseSavedTaskViews(savedViewsRaw), [savedViewsRaw]);
  const pinCount = useMemo(() => {
    try {
      const pins = JSON.parse(pinsRaw) as unknown;
      return Array.isArray(pins) ? pins.length : 0;
    } catch {
      return 0;
    }
  }, [pinsRaw]);

  const runtimeOptions: SelectOption[] = variants.map((variant) => ({
    value: variant.id,
    label: variant.label,
    disabled: !supportsMissionControlVariant(variant),
    description: !variant.available && variant.reason ? variant.reason : undefined,
  }));

  const removeView = (id: string) => {
    const next = savedViews.filter((view) => view.id !== id);
    window.localStorage.setItem(LOCAL_DATA_KEYS.savedViews, JSON.stringify(next));
    window.dispatchEvent(new Event("bmas-task-views-changed"));
  };
  const clearPins = () => {
    window.localStorage.setItem(LOCAL_DATA_KEYS.pins, "[]");
    window.dispatchEvent(new Event("bmas-pins-changed"));
  };

  return (
    <>
      <SectionHeading title="Workspace" lede="These settings live in this browser only. They do not change the daemon." />
      <SettingsCard title="Composer">
        <SettingsRow
          htmlFor="pref-runtime"
          label="Default runtime"
          description="The runtime the composer selects when the home page opens."
          control={runtimeOptions.length
            ? <SelectMenu id="pref-runtime" value={preferences.defaultRuntime} options={runtimeOptions} className="settings-select" onChange={(value) => setPreferences({ defaultRuntime: value })} />
            : <span className="settings-meta">Runtime list unavailable</span>}
        />
        <SettingsRow
          label="Send with"
          description="Shift+Enter always inserts a line break."
          control={(
            <SegmentedControl
              value={preferences.sendKey}
              options={[
                { value: "enter", label: "Enter" },
                { value: "mod-enter", label: "⌘ Enter", description: "Ctrl+Enter on Windows and Linux" },
              ]}
              aria-label="Send key"
              onChange={(value) => setPreferences({ sendKey: value })}
            />
          )}
        />
      </SettingsCard>
      <SettingsCard title="Display">
        <SettingsRow
          label="Start with the sidebar collapsed"
          description="Applies on wide screens. Use the Collapse control to change it at any time."
          control={<Toggle checked={preferences.sidebarCollapsed} onChange={(value) => setPreferences({ sidebarCollapsed: value })} label="Start with the sidebar collapsed" />}
        />
        <SettingsRow
          label="Reduce motion"
          description="Turns off decorative animation such as pulses and floating icons."
          control={<Toggle checked={preferences.reducedMotion} onChange={(value) => setPreferences({ reducedMotion: value })} label="Reduce motion" />}
        />
      </SettingsCard>
      <SettingsCard title="Saved views" description="Filter sets saved from the Tasks page.">
        {savedViews.length === 0 ? (
          <p className="settings-empty">No saved views. Apply a filter on the Tasks page, then choose Save view.</p>
        ) : (
          <ul className="settings-list">
            {savedViews.map((view) => (
              <li key={view.id}>
                <div>
                  <strong>{view.name}</strong>
                  <span>
                    {[view.filters.status ? `status ${view.filters.status}` : null, view.filters.search ? `search “${view.filters.search}”` : null, view.sort && view.sort !== "created-desc" ? `sort ${view.sort}` : null].filter(Boolean).join(" · ") || "All tasks"}
                  </span>
                </div>
                <button type="button" className="icon-button" aria-label={`Delete saved view ${view.name}`} onClick={() => removeView(view.id)}><Trash2 size={15} /></button>
              </li>
            ))}
          </ul>
        )}
      </SettingsCard>
      <SettingsCard title="Local data">
        <SettingsRow
          label="Pinned tasks"
          description={`${pinCount} task${pinCount === 1 ? "" : "s"} pinned to the top of the Tasks list in this browser.`}
          control={<button type="button" className="button" disabled={!pinCount} onClick={clearPins}>Clear pins</button>}
        />
        <SettingsRow
          label="Clear all browser data"
          description="Removes preferences, saved views, and pins from this browser. Daemon settings are not affected."
          control={confirmClear ? (
            <span className="settings-confirm">
              <button type="button" className="button" onClick={() => setConfirmClear(false)}>Cancel</button>
              <button type="button" className="button button--danger" onClick={() => { clearLocalData(); setConfirmClear(false); toast({ type: "success", message: "Browser data cleared." }); }}>Clear everything</button>
            </span>
          ) : (
            <button type="button" className="button button--danger-ghost" onClick={() => setConfirmClear(true)}><Trash2 size={14} aria-hidden="true" /> Clear…</button>
          )}
        />
      </SettingsCard>
    </>
  );
}

// ── Section: system (read only) ───────────────────────────────────────

function SystemSection({
  snapshot,
  schema,
  variants,
  overrideCount,
  onCopyYaml,
  onReset,
}: {
  snapshot: SettingsSnapshot;
  schema: SchemaData;
  variants: VariantCapability[];
  overrideCount: number;
  onCopyYaml: () => void;
  onReset: () => void;
}) {
  const readiness = useReadiness();
  const document_ = readiness.document;
  const agentsOnline = document_ ? Object.values(document_.agent_health).filter((agent) => agent.alive).length : 0;
  const agentsTotal = document_ ? Object.keys(document_.agent_health).length : 0;
  return (
    <>
      <SectionHeading title="System" lede="Facts about the running stack. Change these in bmas.yaml and .env, then restart." />
      <SettingsCard
        title="Session overrides"
        description={overrideCount
          ? `${overrideCount} daemon setting${overrideCount === 1 ? "" : "s"} differ from bmas.yaml. Copy the patch to keep them after a restart.`
          : "Every daemon setting matches bmas.yaml."}
        actions={(
          <>
            <button type="button" className="button" onClick={onCopyYaml} disabled={!overrideCount}><Copy size={14} aria-hidden="true" /> Copy YAML patch</button>
            <button type="button" className="button button--danger-ghost" onClick={onReset} disabled={!overrideCount}><RotateCcw size={14} aria-hidden="true" /> Reset all</button>
          </>
        )}
      >
        {overrideCount ? <pre className="settings-yaml">{buildYamlPatch(snapshot)}</pre> : null}
      </SettingsCard>
      <SettingsCard title="Services">
        <dl className="settings-facts">
          <Fact label="Model gateway" value={document_ ? (document_.litellm_connected ? "Online" : "Offline") : "Unknown"} tone={document_?.litellm_connected ? "ok" : "bad"} />
          <Fact label="Redis" value={document_ ? (document_.redis_connected ? "Connected" : "Disconnected") : "Unknown"} tone={document_?.redis_connected ? "ok" : "bad"} />
          <Fact label="Execution agents" value={document_ ? `${agentsOnline} of ${agentsTotal} ready` : "Unknown"} tone={agentsTotal > 0 && agentsOnline === agentsTotal ? "ok" : "warn"} />
          <Fact label="Task queue" value={document_ ? `${document_.task_queue.active_tasks}/${document_.task_queue.active_capacity} active · ${document_.task_queue.queued_tasks}/${document_.task_queue.queue_capacity} queued` : "Unknown"} />
          <Fact label="File storage" value={document_ ? (document_.storage.enabled ? (document_.storage.ready ? `Writable · ${document_.storage.max_upload_mb} MB per upload · ${document_.storage.max_output_mb} MB per task` : "Enabled, not writable") : "Disabled") : "Unknown"} tone={document_?.storage.enabled ? (document_.storage.ready ? "ok" : "bad") : undefined} />
          <Fact label="Execution nodes" value={schema.configured_hosts.length ? schema.configured_hosts.map((host) => `${host.name} (${titleCase(host.role)}) ${host.host}`).join(" · ") : "None configured"} />
        </dl>
      </SettingsCard>
      <SettingsCard title="Provider credentials" description="Keys come from environment variables in .env.">
        {document_?.provider_credentials.length ? (
          <div className="settings-table-wrap">
            <table className="settings-table">
              <thead><tr><th>Alias</th><th>Provider</th><th>Variable</th><th>State</th></tr></thead>
              <tbody>
                {document_.provider_credentials.map((credential) => (
                  <tr key={credential.alias}>
                    <td><code>{credential.alias}</code></td>
                    <td>{credential.provider}</td>
                    <td><code>{credential.env_var || "—"}</code></td>
                    <td><span className={`settings-state settings-state--${credential.configured ? "ok" : credential.required ? "bad" : "muted"}`}>{credential.configured ? "Configured" : credential.required ? "Missing" : "Not selected"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="settings-empty">{readiness.error || "No credential report yet."}</p>}
      </SettingsCard>
      <SettingsCard title="Runtimes" description="Coordination runtimes the daemon advertises.">
        <ul className="settings-list">
          {variants.map((variant) => (
            <li key={variant.id}>
              <div>
                <strong>{variant.label}</strong>
                <span>{variant.id} · contract {variant.contract_version}{variant.supports_recovery ? " · recovery" : ""}</span>
              </div>
              <span className={`settings-state settings-state--${variant.available ? "ok" : "muted"}`}>{variant.available ? "Available" : variant.reason || "Unavailable"}</span>
            </li>
          ))}
          {variants.length === 0 ? <li><span className="settings-empty">Runtime list unavailable.</span></li> : null}
        </ul>
      </SettingsCard>
    </>
  );
}

function subscribeLocal(key: string, eventName: string, callback: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key === key) callback();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener(eventName, callback);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener(eventName, callback);
  };
}

function Fact({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" | "bad" }) {
  return (
    <div className="settings-fact">
      <dt>{label}</dt>
      <dd data-tone={tone}>{value}</dd>
    </div>
  );
}

function SectionHeading({ title, lede }: { title: string; lede: string }) {
  return (
    <div className="settings-section-heading">
      <h3>{title}</h3>
      <p>{lede}</p>
    </div>
  );
}
