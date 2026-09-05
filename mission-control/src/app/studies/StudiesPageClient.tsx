"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { FlaskRound, Plus, RefreshCw, X } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { Select } from "@/components/ui/Select";
import { useToast } from "@/hooks/useToast";
import type { DatasetItem, DatasetSummary } from "@/lib/datasets";
import { errorText, isoNow, moneyText, statusWords, type AuthoredStudy, type StoredStudy } from "@/lib/evaluation-operations";
import { STUDY_TYPES, buildStudyRequest, defaultStudyForm, estimateRows, studyFormErrors, type StudyForm } from "@/lib/study-presentation";

interface ScorerOption {
  id: string;
  name?: string;
}

/**
 * Author a study, preview its arms and estimates, and publish it into
 * one test revision and one run plan. Every published study lists
 * with its links so an operator opens the revision and starts runs.
 */
export function StudiesPageClient() {
  const { toast } = useToast();
  const [studies, setStudies] = useState<StoredStudy[] | null>(null);
  const [scorers, setScorers] = useState<ScorerOption[]>([]);
  const [datasets, setDatasets] = useState<DatasetSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [authoring, setAuthoring] = useState(false);
  const [pending, setPending] = useState<"preview" | "publish" | null>(null);
  const [form, setForm] = useState<StudyForm>(() => defaultStudyForm());
  const [preview, setPreview] = useState<AuthoredStudy | null>(null);
  const load = useCallback(async () => {
    try {
      const [studyResponse, scorerResponse, datasetResponse] = await Promise.all([
        fetch("/api/evaluation/studies", { cache: "no-store" }),
        fetch("/api/benchmarks/scorers", { cache: "no-store" }),
        fetch("/api/datasets", { cache: "no-store" }),
      ]);
      const studyData = await studyResponse.json() as { studies?: StoredStudy[]; error?: string; detail?: string };
      if (!studyResponse.ok) throw new Error(errorText(studyData, "The studies are unavailable"));
      setStudies(studyData.studies ?? []);
      if (scorerResponse.ok) setScorers(((await scorerResponse.json()) as { scorers?: ScorerOption[] }).scorers ?? []);
      if (datasetResponse.ok) setDatasets(((await datasetResponse.json()) as { datasets?: DatasetSummary[] }).datasets ?? []);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The studies are unavailable");
    }
  }, []);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  const errors = useMemo(() => studyFormErrors(form), [form]);
  const update = (patch: Partial<StudyForm>) => { setPreview(null); setForm((current) => ({ ...current, ...patch })); };

  const chooseDataset = async (datasetId: string) => {
    const dataset = datasets.find((entry) => entry.id === datasetId);
    const versionId = dataset?.latest_version_id ?? "";
    update({ dataset_version_id: versionId });
    if (!dataset || !versionId) return;
    try {
      const response = await fetch(`/api/datasets/${encodeURIComponent(dataset.id)}/versions/${encodeURIComponent(versionId)}/items?limit=100`, { cache: "no-store" });
      if (!response.ok) return;
      const body = await response.json() as { items: DatasetItem[] };
      const families = new Map<string, string[]>();
      for (const item of body.items) {
        const family = item.subject?.trim() || "all";
        families.set(family, [...(families.get(family) ?? []), item.item_key]);
      }
      update({
        case_ids: body.items.map((item) => item.item_key).join(", "),
        families: [...families.entries()].map(([name, ids]) => `${name}: ${ids.join(", ")}`).join("\n"),
      });
    } catch {
      // The case list stays editable by hand when the items request fails.
    }
  };

  const submit = async (publish: boolean) => {
    if (errors.length) return;
    setPending(publish ? "publish" : "preview");
    setError(null);
    try {
      const response = await fetch("/api/evaluation/studies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildStudyRequest(form, { publish, authoredAt: isoNow() })),
      });
      const data = await response.json() as AuthoredStudy & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, publish ? "The study could not publish" : "The study could not preview"));
      if (publish) {
        toast({ type: "success", message: `Study ${data.name} published as revision ${data.revision ?? "?"} with run plan ${data.run_plan_id ?? "?"}.` });
        setPreview(null);
        setForm(defaultStudyForm());
        setAuthoring(false);
        await load();
      } else {
        setPreview(data);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The study request failed");
    } finally {
      setPending(null);
    }
  };
  const previewRows = useMemo(() => (preview ? estimateRows(preview) : []), [preview]);

  return (
    <div className="benchmarks-page">
      <header className="page-header">
        <div><p className="page-eyebrow">Experiment</p><h2>Studies</h2><p>A study predeclares the arms, the case and seed schedule, the estimand, and the comparison family. Publication writes one immutable test revision and one run plan, and admission enforces the study conditions on every run.</p></div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
          <ActionButton variant={authoring ? "secondary" : "primary"} onClick={() => setAuthoring((value) => !value)}>{authoring ? <X size={15} /> : <Plus size={15} />} {authoring ? "Close" : "Author study"}</ActionButton>
        </div>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      {authoring ? (
        <form className="benchmark-form study-form" onSubmit={(event) => { event.preventDefault(); void submit(true); }} aria-labelledby="study-form-title">
          <header><div><h3 id="study-form-title"><FlaskRound size={15} /> Author a study</h3><p>Preview the arms and the estimate first. Publication needs a scorer version and freezes the plan.</p></div></header>
          <div className="benchmark-form__grid">
            <label>Name<input required value={form.name} onChange={(event) => update({ name: event.target.value })} placeholder="temperature sweep" /></label>
            <label>Study type<Select value={form.study_type} onChange={(event) => update({ study_type: event.target.value as StudyForm["study_type"] })}>{STUDY_TYPES.map((type) => <option key={type} value={type}>{statusWords(type)}</option>)}</Select></label>
            <label>Hypothesis<Select value={form.hypothesis} onChange={(event) => update({ hypothesis: event.target.value as StudyForm["hypothesis"] })}><option value="non_inferiority">Non-inferiority</option><option value="superiority">Superiority</option></Select></label>
            <label>Comparison margin<input type="number" min={0} max={1} step={0.01} value={form.comparison_margin} onChange={(event) => update({ comparison_margin: Number(event.target.value) })} /></label>
          </div>
          <fieldset className="benchmark-form__section"><legend>Treatment</legend><div className="benchmark-form__grid study-form__treatment">
            <label className="study-form__wide">Base configuration (JSON)<textarea rows={5} value={form.base_configuration} onChange={(event) => update({ base_configuration: event.target.value })} /></label>
            <label>Treatment path<input required value={form.treatment_path} onChange={(event) => update({ treatment_path: event.target.value })} placeholder="classic.max_rounds" /></label>
            <label>Treatment values (comma separated)<input required value={form.treatment_values} onChange={(event) => update({ treatment_values: event.target.value })} /></label>
            <label>Runtime<input required value={form.runtime_id} onChange={(event) => update({ runtime_id: event.target.value })} /></label>
          </div></fieldset>
          <fieldset className="benchmark-form__section"><legend>Invariants</legend><div className="benchmark-form__grid">
            <label>Dataset<Select value={datasets.find((dataset) => dataset.latest_version_id === form.dataset_version_id)?.id ?? ""} onChange={(event) => void chooseDataset(event.target.value)}><option value="">Select a dataset</option>{datasets.filter((dataset) => dataset.latest_version_id).map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.name} (v{dataset.latest_version})</option>)}</Select></label>
            <label>Dataset version id<input required value={form.dataset_version_id} onChange={(event) => update({ dataset_version_id: event.target.value })} /></label>
            <label>Scorer<Select required value={form.scorer_id} onChange={(event) => update({ scorer_id: event.target.value })}><option value="">Select a scorer</option>{scorers.map((scorer) => <option key={scorer.id} value={scorer.id}>{scorer.name ?? scorer.id}</option>)}</Select></label>
            <label>Repetitions<input type="number" min={1} max={1000} value={form.repetitions} onChange={(event) => update({ repetitions: Number(event.target.value) })} /></label>
            <label>Base seed<input type="number" min={0} value={form.base_seed} onChange={(event) => update({ base_seed: Number(event.target.value) })} /></label>
            <label>Master seed<input type="number" min={0} value={form.master_seed} onChange={(event) => update({ master_seed: Number(event.target.value) })} /></label>
            <label className="study-form__wide">Case ids (comma separated)<textarea rows={3} value={form.case_ids} onChange={(event) => update({ case_ids: event.target.value })} /></label>
            <label className="study-form__wide">Families (one per line as family: id, id; empty puts every case in one family)<textarea rows={3} value={form.families} onChange={(event) => update({ families: event.target.value })} /></label>
          </div></fieldset>
          <fieldset className="benchmark-form__section"><legend>Budget</legend><div className="benchmark-form__grid">
            <label>Cost per attempt<input required value={form.per_attempt_cost} onChange={(event) => update({ per_attempt_cost: event.target.value })} /></label>
            <label>Currency<input required maxLength={3} value={form.currency} onChange={(event) => update({ currency: event.target.value.toUpperCase() })} /></label>
            <label>Seconds per attempt<input type="number" min={1} value={form.seconds_per_attempt} onChange={(event) => update({ seconds_per_attempt: Number(event.target.value) })} /></label>
            <label>Max concurrency<input type="number" min={1} value={form.max_concurrency} onChange={(event) => update({ max_concurrency: Number(event.target.value) })} /></label>
          </div></fieldset>
          {errors.length ? <ul className="benchmark-report__warnings metric-form__errors" aria-label="Study problems">{errors.map((entry) => <li key={entry}>{entry}</li>)}</ul> : null}
          {preview ? (
            <div className="verdict-banner verdict-banner--indeterminate study-form__preview" role="status" aria-label="Study preview">
              <strong>Preview of {preview.name}: {preview.arms.length} arms, digest {preview.study_digest.slice(0, 12)}</strong>
              <dl className="benchmark-metadata">{previewRows.map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>
              <ul className="study-form__arms">{preview.arms.map((arm) => <li key={arm.slug}><code>{arm.slug}</code><small>treatment {JSON.stringify(arm.treatment)} · configuration {arm.configuration_digest.slice(0, 12)}</small></li>)}</ul>
            </div>
          ) : null}
          <div className="benchmark-form__actions">
            <ActionButton type="button" variant="secondary" loading={pending === "preview"} disabled={errors.length > 0} onClick={() => void submit(false)}>Preview estimates</ActionButton>
            <ActionButton type="submit" loading={pending === "publish"} disabled={errors.length > 0}>Publish study</ActionButton>
          </div>
        </form>
      ) : null}
      <section className="benchmark-catalog" aria-labelledby="study-catalog-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="study-catalog-title">Published studies</h3><span>{studies ? `${studies.length} studies` : "Loading"}</span></div></header>
        {studies && studies.length === 0 && !error ? <ResourceState kind="empty" title="No study" description="Author a study to predeclare its arms and estimand before any run starts." /> : null}
        {studies && studies.length ? (
          <div className="benchmark-table-wrap">
            <table className="benchmark-table">
              <caption>Every published study with its plan and revision</caption>
              <thead><tr><th>Study</th><th>Type</th><th>Arms</th><th>Attempts</th><th>Estimated cost</th><th>Revision</th><th>Published</th></tr></thead>
              <tbody>{studies.map((study) => <tr key={study.study_id}><td><Link href={`/studies/${encodeURIComponent(study.study_id)}`}>{study.record.name}</Link><small>{study.study_id}</small></td><td>{statusWords(study.study_type)}</td><td>{study.record.arms.map((arm) => arm.slug).join(", ")}</td><td>{study.record.sample_plan.attempts}</td><td>{moneyText(study.record.estimates.cost)}</td><td><code>{study.test_revision_id}</code></td><td>{new Date(study.created_at).toLocaleString()}</td></tr>)}</tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
