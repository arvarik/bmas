"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Plus, RefreshCw, Scale, X } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { Select } from "@/components/ui/Select";
import { useToast } from "@/hooks/useToast";
import { errorText, isoNow, percentText, type CalibrationOutcome, type CalibrationRecord, type StoredAnchorSet } from "@/lib/evaluation-operations";
import {
  agreementSummary,
  anchorSetFormErrors,
  buildAnchorSetRequest,
  calibrationStateLabel,
  defaultAnchorSetForm,
  scheduleStatus,
  type AnchorSetForm,
} from "@/lib/judge-calibration-presentation";

interface ScorerOption {
  id: string;
  name?: string;
}

/**
 * Judge anchor sets and their calibration schedule.
 *
 * Every anchor set shows when its next calibration is due, the latest
 * agreement with kappa and its interval, the drift against the
 * previous judge version with the policy outcome, and the abstention
 * and invalid-output rates. Calibrate now runs the judge over the
 * anchor items through the configured gateway.
 */
export function JudgesPageClient() {
  const { toast } = useToast();
  const [anchorSets, setAnchorSets] = useState<StoredAnchorSet[] | null>(null);
  const [calibrations, setCalibrations] = useState<Record<string, CalibrationRecord | null>>({});
  const [scorers, setScorers] = useState<ScorerOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [authoring, setAuthoring] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [form, setForm] = useState<AnchorSetForm>(() => defaultAnchorSetForm());
  const now = useMemo(() => new Date(), []);
  const load = useCallback(async () => {
    try {
      const [anchorResponse, scorerResponse] = await Promise.all([
        fetch(`/api/evaluation/judges/anchor-sets?now=${encodeURIComponent(isoNow())}`, { cache: "no-store" }),
        fetch("/api/benchmarks/scorers", { cache: "no-store" }),
      ]);
      const anchorData = await anchorResponse.json() as { anchor_sets?: StoredAnchorSet[]; error?: string; detail?: string };
      if (!anchorResponse.ok) throw new Error(errorText(anchorData, "The anchor sets are unavailable"));
      const sets = anchorData.anchor_sets ?? [];
      setAnchorSets(sets);
      if (scorerResponse.ok) setScorers(((await scorerResponse.json()) as { scorers?: ScorerOption[] }).scorers ?? []);
      const pairs = [...new Set(sets.map((set) => `${set.judge_id} ${set.judge_version}`))];
      const loaded = await Promise.all(pairs.map(async (pair) => {
        const [judgeId, version] = pair.split(" ");
        const response = await fetch(`/api/evaluation/judges/${encodeURIComponent(judgeId)}/versions/${encodeURIComponent(version)}/calibration`, { cache: "no-store" });
        if (!response.ok) return [pair, null] as const;
        return [pair, (await response.json()) as CalibrationRecord] as const;
      }));
      setCalibrations(Object.fromEntries(loaded));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The anchor sets are unavailable");
    }
  }, []);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  const errors = useMemo(() => anchorSetFormErrors(form), [form]);
  const update = (patch: Partial<AnchorSetForm>) => setForm((current) => ({ ...current, ...patch }));

  const register = async (event: React.FormEvent) => {
    event.preventDefault();
    if (errors.length) return;
    setPending("register");
    setError(null);
    try {
      const response = await fetch("/api/evaluation/judges/anchor-sets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildAnchorSetRequest(form, isoNow())),
      });
      const data = await response.json() as { anchor_id?: string; error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The anchor set could not register"));
      toast({ type: "success", message: `Anchor set ${data.anchor_id ?? form.anchor_id} registered; the first calibration is due now.` });
      setForm(defaultAnchorSetForm());
      setAuthoring(false);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The anchor set could not register");
    } finally {
      setPending(null);
    }
  };

  const calibrate = async (anchorId: string) => {
    setPending(anchorId);
    setError(null);
    try {
      const response = await fetch(`/api/evaluation/judges/anchor-sets/${encodeURIComponent(anchorId)}/calibrate?calibrated_at=${encodeURIComponent(isoNow())}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const data = await response.json() as CalibrationOutcome & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The calibration failed"));
      toast({ type: data.state === "current" ? "success" : "error", message: `Calibration ${data.calibration_id} is ${calibrationStateLabel(data.state).toLowerCase()} with ${percentText(data.raw_agreement)} raw agreement; next due ${new Date(data.next_due_at).toLocaleDateString()}.` });
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The calibration failed");
    } finally {
      setPending(null);
    }
  };

  const dueCount = (anchorSets ?? []).filter((set) => set.due).length;
  return (
    <div className="benchmarks-page">
      <header className="page-header">
        <div><p className="page-eyebrow">Evaluate</p><h2>Judge calibration</h2><p>Every model-backed judge version calibrates against an anchor set on a schedule. The screen tracks raw agreement, kappa, drift against the previous version, and abstention, and it alerts when drift exceeds the policy tolerance.</p></div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
          <ActionButton variant={authoring ? "secondary" : "primary"} onClick={() => setAuthoring((value) => !value)}>{authoring ? <X size={15} /> : <Plus size={15} />} {authoring ? "Close" : "Register anchor set"}</ActionButton>
        </div>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      {authoring ? (
        <form className="benchmark-form anchor-form" onSubmit={register} aria-labelledby="anchor-form-title">
          <header><div><h3 id="anchor-form-title"><Scale size={15} /> Register an anchor set</h3><p>The anchor items carry human labels. The judge version, the model, and the prompt digest pin what calibrates.</p></div></header>
          <div className="benchmark-form__grid">
            <label>Anchor id<input required value={form.anchor_id} onChange={(event) => update({ anchor_id: event.target.value })} placeholder="anchor-rubric-a" /></label>
            <label>Judge id<input required value={form.judge_id} onChange={(event) => update({ judge_id: event.target.value })} /></label>
            <label>Judge version<input required value={form.judge_version} onChange={(event) => update({ judge_version: event.target.value })} /></label>
            <label>Judge model<input required value={form.judge_model} onChange={(event) => update({ judge_model: event.target.value })} placeholder="model name at the gateway" /></label>
            <label>Prompt digest<input required value={form.prompt_digest} onChange={(event) => update({ prompt_digest: event.target.value })} pattern="[a-f0-9]{64}" /></label>
            <label>Scorer<Select required value={form.scorer_id} onChange={(event) => update({ scorer_id: event.target.value })}><option value="">Select a scorer</option>{scorers.map((scorer) => <option key={scorer.id} value={scorer.id}>{scorer.name ?? scorer.id}</option>)}</Select></label>
            <label>Scorer version<input required value={form.scorer_version} onChange={(event) => update({ scorer_version: event.target.value })} /></label>
            <label>Label set dataset (dataset id or version id)<input required value={form.dataset_id} onChange={(event) => update({ dataset_id: event.target.value })} /></label>
            <label>Label set version (version id or number)<input required value={form.dataset_version} onChange={(event) => update({ dataset_version: event.target.value })} /></label>
            <label>Candidate models (comma separated)<input value={form.candidate_models} onChange={(event) => update({ candidate_models: event.target.value })} /></label>
            <label>Interval (days)<input type="number" min={1} max={365} value={form.interval_days} onChange={(event) => update({ interval_days: Number(event.target.value) })} /></label>
            <label>Agreement threshold<input type="number" min={0} max={1} step={0.01} value={form.threshold} onChange={(event) => update({ threshold: Number(event.target.value) })} /></label>
            <label>Drift tolerance<input type="number" min={0} max={1} step={0.01} value={form.drift_tolerance} onChange={(event) => update({ drift_tolerance: Number(event.target.value) })} /></label>
            <label className="study-form__wide">Anchor items (one per line as item id, label, optional candidate answer)<textarea rows={5} value={form.items} onChange={(event) => update({ items: event.target.value })} placeholder={"item-1, pass, 42\nitem-2, fail, 4"} /></label>
          </div>
          {errors.length ? <ul className="benchmark-report__warnings metric-form__errors" aria-label="Anchor set problems">{errors.map((entry) => <li key={entry}>{entry}</li>)}</ul> : null}
          <div className="benchmark-form__actions"><ActionButton type="submit" loading={pending === "register"} disabled={errors.length > 0}>Register anchor set</ActionButton></div>
        </form>
      ) : null}
      <section className="benchmark-catalog" aria-labelledby="anchor-catalog-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="anchor-catalog-title">Anchor sets</h3><span>{anchorSets ? `${anchorSets.length} anchor sets · ${dueCount} due` : "Loading"}</span></div></header>
        {anchorSets && anchorSets.length === 0 && !error ? <ResourceState kind="empty" title="No anchor set" description="Register an anchor set so every judge version calibrates on a schedule." /> : null}
        {anchorSets && anchorSets.length ? (
          <div className="benchmark-table-wrap">
            <table className="benchmark-table judge-table">
              <caption>Calibration schedule and the latest calibration per judge version</caption>
              <thead><tr><th>Anchor set</th><th>Judge</th><th>Schedule</th><th>Agreement</th><th>Drift</th><th>Abstention</th><th>State</th><th>Action</th></tr></thead>
              <tbody>
                {anchorSets.map((set) => {
                  const schedule = scheduleStatus(set, now);
                  const calibration = calibrations[`${set.judge_id} ${set.judge_version}`] ?? null;
                  const summary = agreementSummary(calibration);
                  return (
                    <tr key={set.id} data-due={set.due ? "true" : "false"}>
                      <td><strong>{set.record.anchor_id}</strong><small>{set.record.label_set.items.length} items · {set.record.scorer.scorer_id} v{set.record.scorer.version}</small></td>
                      <td>{set.judge_id} <small>v{set.judge_version} · {set.record.judge.model}</small></td>
                      <td><span className={`benchmark-status benchmark-status--${schedule.tone}`}>{schedule.label}</span><small>every {set.record.schedule.interval_days} days · next {new Date(set.next_due_at).toLocaleDateString()}{set.last_calibrated_at ? ` · last ${new Date(set.last_calibrated_at).toLocaleDateString()}` : ""}</small></td>
                      <td>{summary.available ? <>{summary.raw} raw<small>kappa {summary.kappa}{summary.interval ? ` · ${summary.interval}` : ""} · threshold {summary.threshold}</small></> : <small>{summary.raw}</small>}</td>
                      <td>{summary.available ? <><span className={`benchmark-status benchmark-status--${summary.exceedsPolicy ? "failed" : "passed"}`}>{summary.exceedsPolicy ? "Exceeds policy" : "Within policy"}</span><small>{summary.driftDelta}</small></> : <small>No calibration</small>}</td>
                      <td>{summary.available ? <>{summary.abstention}<small>invalid {summary.invalidOutput} · disagreements {summary.disagreements}</small></> : "None"}</td>
                      <td><span className={`benchmark-status benchmark-status--${summary.tone}`}>{summary.available ? calibrationStateLabel(summary.state) : "Uncalibrated"}</span></td>
                      <td><ActionButton variant={set.due ? "primary" : "secondary"} loading={pending === set.id} disabled={set.state !== "active"} onClick={() => void calibrate(set.id)}>Calibrate now</ActionButton></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
