"use client";

import { useEffect, useId, useRef, useState } from "react";
import {
  CLASSIC_SPEC_ROUTE, displayValue, effectiveRows, isRecord, parseClassicPreview, parseClassicSchema,
  type ClassicControl, type ClassicInput, type ClassicPreview, type ClassicSchema, type FieldError, type JsonValue,
} from "@/lib/classic-spec";
import styles from "./ClassicSpecEditor.module.css";

export function ClassicSpecEditor() {
  const id = useId();
  const [schema, setSchema] = useState<ClassicSchema | null>(null);
  const [input, setInput] = useState<ClassicInput | null>(null);
  const [preview, setPreview] = useState<ClassicPreview | null>(null);
  const [errors, setErrors] = useState<FieldError[]>([]);
  const [advanced, setAdvanced] = useState<string | null>(null);
  const [advancedInvalid, setAdvancedInvalid] = useState(false);
  const [busy, setBusy] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const [lastPreview, setLastPreview] = useState<ClassicPreview | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch(CLASSIC_SPEC_ROUTE, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("The Classic input schema is unavailable. Retry the preview.");
        return parseClassicSchema(await response.json());
      }).then((document) => {
        if (controller.signal.aborted) return;
        setSchema(document);
        setInput((current) => current ?? document["x-editor"].defaults);
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) setErrors([{ field: "service", message: error instanceof Error ? error.message : "The schema is unavailable." }]);
      });
    return () => controller.abort();
  }, [retry]);

  useEffect(() => {
    if (!input || advancedInvalid) return;
    const controller = new AbortController();
    const current = ++generation.current;
    const timer = setTimeout(async () => {
      setBusy(true);
      try {
        const response = await fetch(CLASSIC_SPEC_ROUTE, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input), signal: controller.signal,
        });
        const data: unknown = await response.json();
        if (controller.signal.aborted || current !== generation.current) return;
        if (!response.ok) {
          const detail = isRecord(data) ? data.detail : null;
          const fields = isRecord(detail) && Array.isArray(detail.errors) ? detail.errors.filter((error) => isRecord(error) && typeof error.field === "string" && typeof error.message === "string") : [];
          setErrors(fields.length ? fields as FieldError[] : [{ field: "service", message: typeof detail === "string" ? detail : "The preview failed. Retry the preview." }]);
          return;
        }
        const compiled = parseClassicPreview(data);
        setLastPreview(compiled);
        setPreview(compiled);
        setErrors([]);
      } catch (error: unknown) {
        if (!controller.signal.aborted && current === generation.current) setErrors([{ field: "service", message: error instanceof Error ? error.message : "The preview failed." }]);
      } finally {
        if (!controller.signal.aborted && current === generation.current) setBusy(false);
      }
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [input, advancedInvalid, retry]);

  const change = (next: ClassicInput) => {
    generation.current += 1;
    setPreview(null);
    setConfirmed(false);
    setErrors([]);
    setBusy(true);
    setAdvancedInvalid(false);
    setInput(next);
    setAdvanced(JSON.stringify(next, null, 2));
  };
  const fieldErrors = (path: string) => errors.filter((error) => error.field === path || error.field === `task_overrides.classic.${path}`);
  const errorText = (path: string) => fieldErrors(path).map((error) => error.message).join(" ");
  const errorNode = (path: string) => errorText(path) ? <span id={`${id}-${path}-error`} className={styles.error} role="alert">{errorText(path)}</span> : null;

  if (!schema || !input) return <section className={styles.editor} aria-label="Classic specification"><p role="status">Loading the Classic input schema…</p>{errors.map((error) => <p role="alert" key={error.field}>{error.message}</p>)}<button type="button" onClick={() => setRetry((value) => value + 1)}>Retry preview</button></section>;

  const metadata = schema["x-editor"];
  const fixed = metadata.fidelity_profiles.find((profile) => profile.profile_id === input.fidelity)?.fixed_fields ?? [];
  const resolution = (preview ?? lastPreview)?.specification.resolution;
  const control = (item: ClassicControl) => {
    const record = isRecord(resolution) ? resolution[item.path] : null;
    const effective = isRecord(record) && isRecord(record.offered) ? record.offered[String(record.layer)] : item.schema.default;
    const value = input.task_overrides.classic[item.path] ?? effective;
    const automatic = !(item.path in input.task_overrides.classic);
    const nodeId = `${id}-${item.path}`;
    const error = errorText(item.path);
    const choices = item.schema.enum ?? (item.schema.type === "boolean" ? [true, false] : undefined);
    const nullable = item.schema.anyOf?.some((option) => option.type === "null");
    const kind = item.schema.type ?? item.schema.anyOf?.find((option) => option.type !== "null")?.type;
    const set = (next: JsonValue | undefined) => {
      const overrides = { ...input.task_overrides.classic };
      if (next === undefined) delete overrides[item.path];
      else overrides[item.path] = next;
      change({ ...input, task_overrides: { ...input.task_overrides, classic: overrides } });
    };
    return <div className={styles.control} key={item.path}>
      <label htmlFor={nodeId}>{item.label}</label>
      {choices ? <select id={nodeId} value={JSON.stringify(value ?? null)} aria-invalid={Boolean(error)} aria-describedby={`${nodeId}-help${error ? ` ${nodeId}-error` : ""}`} onChange={(event) => set(JSON.parse(event.target.value) as JsonValue)}>
        {choices.map((choice) => <option key={JSON.stringify(choice)} value={JSON.stringify(choice)}>{displayValue(choice).replaceAll("_", " ")}</option>)}
      </select> : <input id={nodeId} type={kind === "integer" || kind === "number" ? "number" : "text"} inputMode={kind === "integer" ? "numeric" : item.path === "limits.max_cost" || kind === "number" ? "decimal" : undefined}
        step={kind === "integer" ? 1 : "any"} value={value === null || value === undefined ? "" : String(value)}
        aria-invalid={Boolean(error)} aria-describedby={`${nodeId}-help${error ? ` ${nodeId}-error` : ""}`}
        onChange={(event) => set(event.target.value === "" ? nullable ? null : undefined : kind === "integer" || kind === "number" ? Number(event.target.value) : event.target.value)} />}
      <small id={`${nodeId}-help`}>Schema default: {displayValue(item.schema.default)}. {item.cap ? `Safe range: ${item.cap.map(displayValue).join(" to ")}. The compiler shows each cap adjustment.` : item.schema.minimum !== undefined ? `Minimum: ${item.schema.minimum}.` : "The schema defines the allowed choices."} {fixed.includes(item.path) ? "The fidelity profile fixes the effective value." : automatic ? "The profiles and deployment set this value." : "This task requests an override."}</small>
      {errorNode(item.path)}
      {!automatic ? <button type="button" onClick={() => set(undefined)}>Use profile value for {item.label.toLowerCase()}</button> : null}
    </div>;
  };
  const visiblePaths = new Set(metadata.groups.flatMap((group) => group.controls.map((item) => item.path)));
  const advancedErrors = errors.filter((error) => !["fidelity", "effort", "task_overrides.seed", "service"].includes(error.field) && !visiblePaths.has(error.field.replace("task_overrides.classic.", "")));
  const summary = preview?.specification;
  return <section className={styles.editor} aria-labelledby={`${id}-title`}>
    <header><h2 id={`${id}-title`}>Classic specification</h2><strong>Native pair · test only</strong><p>Compile and inspect this configuration. Public admission stays unavailable until runtime qualification.</p></header>
    <div className={styles.grid}>
      {(["fidelity", "effort"] as const).map((name) => <div className={styles.control} key={name}>
        <label htmlFor={`${id}-${name}`}>{name === "fidelity" ? "Fidelity profile" : "Effort profile"}</label>
        <select id={`${id}-${name}`} value={input[name]} aria-invalid={Boolean(errorText(name))} aria-describedby={`${id}-${name}-help${errorText(name) ? ` ${id}-${name}-error` : ""}`} onChange={(event) => change({ ...input, [name]: event.target.value })}>
          {schema.properties[name].enum.map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
        </select>
        <small id={`${id}-${name}-help`}>{name === "fidelity" ? "Fidelity selects coordination rules. It stays separate from effort." : "Effort selects resource intensity and procedure strength."}</small>{errorNode(name)}
      </div>)}
    </div>
    {metadata.groups.map((group) => <fieldset key={group.name} className={styles.group}>
      <legend>{group.name}</legend><p>{group.description}</p>
      <div className={styles.grid}>{group.controls.filter((item) => item.beginner).map(control)}</div>
      {group.name === "Team" ? <div className={styles.control}><label htmlFor={`${id}-seed`}>Random seed</label><input id={`${id}-seed`} type="number" min={0} step={1} value={input.task_overrides.seed ?? ""} aria-invalid={Boolean(errorText("task_overrides.seed"))} aria-describedby={`${id}-seed-help${errorText("task_overrides.seed") ? ` ${id}-task_overrides.seed-error` : ""}`} onChange={(event) => change({ ...input, task_overrides: { ...input.task_overrides, seed: event.target.value === "" ? null : Number(event.target.value) } })} /><small id={`${id}-seed-help`}>A recorded seed does not guarantee provider reproducibility. Inspect seed support below.</small>{errorNode("task_overrides.seed")}</div> : null}
      <details open={group.controls.some((item) => !item.beginner && fieldErrors(item.path).length > 0) || undefined}><summary>More {group.name.toLowerCase()} controls</summary><div className={styles.grid}>{group.controls.filter((item) => !item.beginner).map(control)}</div></details>
    </fieldset>)}
    <details open={advancedErrors.length > 0 || advancedInvalid || undefined}><summary>Advanced JSON</summary><label htmlFor={`${id}-advanced`}>Specification input choices</label>
      <p>The server supplies deployment settings. This view accepts all task overrides, routing, role endpoints, seed, and the asset manifest digest.</p>
      <textarea id={`${id}-advanced`} rows={12} spellCheck={false} value={advanced ?? JSON.stringify(input, null, 2)} aria-invalid={advancedInvalid || advancedErrors.length > 0} aria-describedby={`${id}-advanced-errors`} onChange={(event) => {
        const text = event.target.value;
        generation.current += 1;
        setAdvanced(text); setPreview(null); setConfirmed(false); setErrors([]);
        try {
          const next: unknown = JSON.parse(text);
          if (!isRecord(next) || typeof next.fidelity !== "string" || typeof next.effort !== "string" || !isRecord(next.task_overrides) || !isRecord(next.task_overrides.classic)) throw new Error("Keep fidelity, effort, and task_overrides.classic in the input object.");
          setInput(next as unknown as ClassicInput); setAdvancedInvalid(false); setBusy(true);
        } catch (error) {
          setAdvancedInvalid(true); setBusy(false); setErrors([{ field: "advanced", message: error instanceof Error ? error.message : "Enter valid JSON." }]);
        }
      }} />
      <div id={`${id}-advanced-errors`}>{advancedErrors.map((error, index) => <p className={styles.error} role="alert" key={`${error.field}-${index}`}>{error.field}: {error.message}</p>)}</div>
    </details>
    <p>Ordinary endpoint edits affect new runs only. Each admitted run keeps its immutable endpoint sets and effective values.</p>
    <p role="status" aria-live="polite">{busy ? "Compiling preview…" : preview ? "Preview ready. Review the effective values below." : "Correct the input to compile a preview."}</p>
    {errors.filter((error) => error.field === "service").map((error) => <p role="alert" key={error.field}>{error.message}</p>)}
    <button type="button" onClick={() => { generation.current += 1; setPreview(null); setConfirmed(false); setBusy(true); setRetry((value) => value + 1); }}>Retry preview</button>
    {preview && summary ? <section aria-labelledby={`${id}-review`} className={styles.review}>
      <h3 id={`${id}-review`}>Review before submission</h3><p>Specification digest: <code data-testid="classic-spec-digest">{preview.specification_digest}</code></p>
      <h4>Cost and latency estimate</h4><Estimate value={summary.estimate} /><ValueTable value={summary.estimate} />
      <ul>{preview.estimate_assumptions.map((note) => <li key={note}>{note}</li>)}</ul>
      <h4>Provider limits</h4><ValueTable value={preview.provider_limits as JsonValue} />
      <h4>Required roles</h4><ValueTable value={isRecord(summary.team) ? summary.team.required_roles as JsonValue : null} />
      <h4>Seed support</h4><ValueTable value={summary.provider_capabilities} /><ValueTable value={summary.randomness} />
      <h4>Every deployment cap</h4><ValueTable value={Object.fromEntries(Object.entries(preview.caps).map(([path, bounds]) => [path, { minimum: bounds[0], maximum: bounds[1] }]))} />
      <h4>Cap adjustments</h4><ValueTable value={isRecord(summary.deployment_caps) ? summary.deployment_caps.adjustments as JsonValue : null} />
      <h4>Warnings</h4><ValueTable value={summary.warnings} />
      {Object.entries(preview.differences).map(([profile, values]) => <div key={profile}><h4>Effective differences from the {profile} profile</h4><ValueTable value={values as unknown as JsonValue} /></div>)}
      <details><summary>Every immutable effective value</summary><p>This record includes model bindings, endpoints, prices, prompts, limits, and the source of each policy value.</p><ValueTable value={preview.specification} /></details>
      <label className={styles.confirm}><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I reviewed the caps, warnings, and immutable effective values.</label>
      {confirmed ? <p role="status">Preview confirmed. The native pair remains test only. No run starts from this preview.</p> : null}
      <button type="button" disabled>Submit native run · test only</button>
    </section> : null}
  </section>;
}

function ValueTable({ value }: { value: JsonValue | undefined }) {
  if (Array.isArray(value) && !value.length) return <p>None.</p>;
  return <dl className={styles.values}>{effectiveRows(value ?? null).map(([path, item]) => <div key={path}><dt>{path.replaceAll("_", " ") || "Value"}</dt><dd>{displayValue(item)}</dd></div>)}</dl>;
}

function Estimate({ value }: { value: JsonValue }) {
  if (!isRecord(value)) return null;
  const amount = (money: unknown) => isRecord(money) && typeof money.amount_nanos === "number"
    ? `${String(money.currency)} ${(money.amount_nanos / 1_000_000_000).toFixed(4)}` : "Unknown";
  return <p>Estimated cost: {amount(value.cost_minimum)} to {amount(value.cost_maximum)}. Estimated latency: {String(value.latency_minimum_seconds)} to {String(value.latency_maximum_seconds)} seconds.</p>;
}
