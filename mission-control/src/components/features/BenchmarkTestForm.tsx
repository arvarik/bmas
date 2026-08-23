"use client";

import { useEffect, useMemo, useState } from "react";
import { Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { ActionButton } from "@/components/ui/ActionButton";
import type { DatasetSummary } from "@/lib/datasets";
import type { BenchmarkScorer } from "@/lib/benchmarks";
import { Select } from "@/components/ui/Select";

interface RuntimeOption { id: string; label: string; contract_version: string }
interface ArmDraft { key: string; name: string; runtime_id: string; configuration: string }
interface Preflight { valid: boolean; total_trials: number; total_attempts: number; configuration_checksum: string; arms: Array<{ name: string; runtime_id: string; configuration_checksum: string }> }

export function BenchmarkTestForm({ testId }: { testId?: string }) {
  const router = useRouter();
  const [datasets, setDatasets] = useState<DatasetSummary[]>([]);
  const [runtimes, setRuntimes] = useState<RuntimeOption[]>([]);
  const [scorers, setScorers] = useState<BenchmarkScorer[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [datasetVersionId, setDatasetVersionId] = useState("");
  const [repetitions, setRepetitions] = useState(1);
  const [seed, setSeed] = useState(0);
  const [maxConcurrency, setMaxConcurrency] = useState(1);
  const [timeoutSeconds, setTimeoutSeconds] = useState(3600);
  const [costLimit, setCostLimit] = useState("");
  const [practicalDifference, setPracticalDifference] = useState(0.01);
  const [arms, setArms] = useState<ArmDraft[]>([
    { key: "arm-1", name: "Classic", runtime_id: "classic", configuration: "{}" },
  ]);
  const [selectedScorers, setSelectedScorers] = useState<string[]>([]);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<"preflight" | "publish" | null>(null);

  useEffect(() => {
    void Promise.all([
      fetch("/api/datasets?limit=200").then((response) => response.json()),
      fetch("/api/capabilities").then((response) => response.json()),
      fetch("/api/benchmarks/scorers").then((response) => response.json()),
    ]).then(([datasetData, capabilityData, scorerData]) => {
      const loadedDatasets = (datasetData.datasets ?? []) as DatasetSummary[];
      const loadedRuntimes = (capabilityData.variants ?? []) as RuntimeOption[];
      const loadedScorers = (scorerData.scorers ?? []) as BenchmarkScorer[];
      setDatasets(loadedDatasets);
      setRuntimes(loadedRuntimes);
      setScorers(loadedScorers);
      setDatasetVersionId(loadedDatasets[0]?.latest_version_id ?? "");
      setSelectedScorers(loadedScorers[0] ? [loadedScorers[0].id] : []);
      if (loadedRuntimes[0]) {
        setArms((current) => current.map((arm) => ({ ...arm, runtime_id: loadedRuntimes[0].id })));
      }
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Authoring data is unavailable"));
  }, []);

  const payload = useMemo(() => {
    try {
      return {
        name: name.trim(),
        description: description.trim(),
        dataset_version_id: datasetVersionId,
        repetitions,
        seed,
        max_concurrency: maxConcurrency,
        timeout_seconds: timeoutSeconds,
        cost_limit_usd: costLimit ? Number(costLimit) : null,
        practical_difference: practicalDifference,
        arms: arms.map((arm) => ({
          name: arm.name.trim(),
          runtime_id: arm.runtime_id,
          configuration: JSON.parse(arm.configuration) as Record<string, unknown>,
        })),
        scorers: selectedScorers.map((id) => ({ id, configuration: {} })),
      };
    } catch {
      return null;
    }
  }, [arms, costLimit, datasetVersionId, description, maxConcurrency, name, practicalDifference, repetitions, seed, selectedScorers, timeoutSeconds]);

  const validate = () => {
    if (!payload) return "Each arm configuration must contain valid JSON.";
    if (!payload.name) return "Enter a test name.";
    if (!payload.dataset_version_id) return "Select a published dataset version.";
    if (payload.arms.some((arm) => !arm.name)) return "Enter a name for each arm.";
    if (!payload.scorers.length) return "Select at least one scorer.";
    return null;
  };

  const submit = async (mode: "preflight" | "publish") => {
    const validation = validate();
    if (validation || !payload) {
      setError(validation);
      return;
    }
    setPending(mode);
    setError(null);
    try {
      const path = mode === "preflight"
        ? "/api/benchmarks/tests/preflight"
        : testId
          ? `/api/benchmarks/tests/${encodeURIComponent(testId)}/revisions`
          : "/api/benchmarks/tests";
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json() as Preflight & { id?: string; detail?: string };
      if (!response.ok) throw new Error(data.detail ?? "The benchmark request failed");
      if (mode === "preflight") setPreflight(data);
      else router.push(`/tests/${encodeURIComponent(data.id ?? testId ?? "")}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The benchmark request failed");
    } finally {
      setPending(null);
    }
  };

  return (
    <section className="benchmark-form" aria-labelledby="benchmark-form-title">
      <header><div><p className="page-eyebrow">Immutable revision</p><h3 id="benchmark-form-title">{testId ? "Create a new revision" : "Define a benchmark test"}</h3></div></header>
      <div className="benchmark-form__grid">
        <label><span>Name</span><input value={name} onChange={(event) => { setName(event.target.value); setPreflight(null); }} /></label>
        <label><span>Dataset</span><Select value={datasetVersionId} onChange={(event) => { setDatasetVersionId(event.target.value); setPreflight(null); }}><option value="">Select a dataset</option>{datasets.map((dataset) => <option key={dataset.id} value={dataset.latest_version_id ?? ""}>{dataset.name} · v{dataset.latest_version}</option>)}</Select></label>
        <label className="benchmark-form__wide"><span>Description</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={2} /></label>
        <label><span>Repetitions</span><input type="number" min={1} max={20} value={repetitions} onChange={(event) => { setRepetitions(Number(event.target.value)); setPreflight(null); }} /></label>
        <label><span>Random seed</span><input type="number" min={0} value={seed} onChange={(event) => { setSeed(Number(event.target.value)); setPreflight(null); }} /></label>
        <label><span>Maximum concurrency</span><input type="number" min={1} max={16} value={maxConcurrency} onChange={(event) => { setMaxConcurrency(Number(event.target.value)); setPreflight(null); }} /></label>
        <label><span>Attempt timeout in seconds</span><input type="number" min={30} value={timeoutSeconds} onChange={(event) => { setTimeoutSeconds(Number(event.target.value)); setPreflight(null); }} /></label>
        <label><span>Run cost limit in USD</span><input type="number" min="0.01" step="0.01" value={costLimit} placeholder="No limit" onChange={(event) => { setCostLimit(event.target.value); setPreflight(null); }} /></label>
        <label><span>Minimum practical score difference</span><input type="number" min="0" max="1" step="0.01" value={practicalDifference} onChange={(event) => { setPracticalDifference(Number(event.target.value)); setPreflight(null); }} /><small>Effects below this score difference do not count as practically meaningful.</small></label>
      </div>

      <fieldset className="benchmark-form__section"><legend>Runtime arms</legend>{arms.map((arm, index) => <div className="benchmark-arm" key={arm.key}><label><span>Arm name</span><input value={arm.name} onChange={(event) => { setArms((current) => current.map((item) => item.key === arm.key ? { ...item, name: event.target.value } : item)); setPreflight(null); }} /></label><label><span>Runtime</span><Select value={arm.runtime_id} onChange={(event) => { setArms((current) => current.map((item) => item.key === arm.key ? { ...item, runtime_id: event.target.value } : item)); setPreflight(null); }}>{runtimes.map((runtime) => <option key={runtime.id} value={runtime.id}>{runtime.label} · contract {runtime.contract_version}</option>)}</Select></label><label className="benchmark-arm__config"><span>Configuration JSON</span><textarea rows={3} spellCheck={false} value={arm.configuration} onChange={(event) => { setArms((current) => current.map((item) => item.key === arm.key ? { ...item, configuration: event.target.value } : item)); setPreflight(null); }} /></label><button type="button" className="benchmark-icon-button" disabled={arms.length === 1} aria-label={`Remove arm ${index + 1}`} onClick={() => { setArms((current) => current.filter((item) => item.key !== arm.key)); setPreflight(null); }}><Trash2 size={16} /></button></div>)}<ActionButton variant="secondary" onClick={() => { setArms((current) => [...current, { key: crypto.randomUUID(), name: `Arm ${current.length + 1}`, runtime_id: runtimes[0]?.id ?? "classic", configuration: "{}" }]); setPreflight(null); }}><Plus size={15} /> Add arm</ActionButton></fieldset>

      <fieldset className="benchmark-form__section"><legend>Scorers</legend><div className="benchmark-scorers">{scorers.map((scorer) => <label key={scorer.id}><input type="checkbox" checked={selectedScorers.includes(scorer.id)} onChange={(event) => { setSelectedScorers((current) => event.target.checked ? [...current, scorer.id] : current.filter((id) => id !== scorer.id)); setPreflight(null); }} /><span><strong>{scorer.name}</strong><small>v{scorer.version} · {scorer.description}</small></span></label>)}</div></fieldset>

      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      {preflight ? <div className="benchmark-preflight" role="status"><ShieldCheck size={18} /><div><strong>Preflight passed</strong><p>{preflight.total_trials.toLocaleString()} trials and {preflight.total_attempts.toLocaleString()} attempts. Configuration {preflight.configuration_checksum.slice(0, 12)}.</p></div></div> : null}
      <div className="benchmark-form__actions"><ActionButton variant="secondary" loading={pending === "preflight"} onClick={() => void submit("preflight")}>Run preflight</ActionButton><ActionButton disabled={!preflight} loading={pending === "publish"} onClick={() => void submit("publish")}>{testId ? "Publish revision" : "Publish test"}</ActionButton></div>
    </section>
  );
}
