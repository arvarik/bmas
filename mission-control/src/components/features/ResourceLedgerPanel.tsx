"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Coins, RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { Select } from "@/components/ui/Select";
import { useToast } from "@/hooks/useToast";
import {
  errorText,
  isoNow,
  moneyText,
  statusWords,
  type LateChargeOutcome,
  type LedgerEntry,
  type LedgerSummary,
  type StoredReconciliation,
} from "@/lib/evaluation-operations";
import {
  RESOURCE_CLASSES,
  buildReconciliationRequest,
  classRows,
  defaultLateChargeForm,
  entryRows,
  flaggedEntries,
  lateChargeFormErrors,
  lateChargeFromEstimate,
  reconciliationRows,
  type LateChargeForm,
} from "@/lib/resource-ledger-presentation";

interface LedgerResponse {
  run_id: string;
  entries: LedgerEntry[];
  summary: LedgerSummary;
}

/**
 * The resource ledger of one run: the totals per class, the entries
 * without a usable amount, every entry, every reconciliation version,
 * and the two actions that open the next version: reconcile now and
 * apply one late charge.
 */
export function ResourceLedgerPanel({ runId, currency = "USD" }: { runId: string; currency?: string }) {
  const { toast } = useToast();
  const [ledger, setLedger] = useState<LedgerResponse | null>(null);
  const [reconciliations, setReconciliations] = useState<StoredReconciliation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"idle" | "reconcile" | "late_charge">("idle");
  const [pending, setPending] = useState(false);
  const [form, setForm] = useState<LateChargeForm>(() => defaultLateChargeForm());
  const [outcome, setOutcome] = useState<LateChargeOutcome | null>(null);
  const load = useCallback(async () => {
    try {
      const [ledgerResponse, reconciliationResponse] = await Promise.all([
        fetch(`/api/evaluation/runs/${encodeURIComponent(runId)}/resource-ledger?currency=${encodeURIComponent(currency)}`, { cache: "no-store" }),
        fetch(`/api/evaluation/runs/${encodeURIComponent(runId)}/reconciliations`, { cache: "no-store" }),
      ]);
      const ledgerData = await ledgerResponse.json() as LedgerResponse & { error?: string; detail?: string };
      if (!ledgerResponse.ok) throw new Error(errorText(ledgerData, "The resource ledger is unavailable"));
      const reconciliationData = await reconciliationResponse.json() as { reconciliations?: StoredReconciliation[]; error?: string; detail?: string };
      if (!reconciliationResponse.ok) throw new Error(errorText(reconciliationData, "The reconciliations are unavailable"));
      setLedger(ledgerData);
      setReconciliations(reconciliationData.reconciliations ?? []);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The resource ledger is unavailable");
    }
  }, [currency, runId]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);

  const classes = useMemo(() => classRows(ledger?.summary), [ledger]);
  const flagged = useMemo(() => flaggedEntries(ledger?.entries ?? [], ledger?.summary), [ledger]);
  const rows = useMemo(() => entryRows(ledger?.entries ?? []), [ledger]);
  const versions = useMemo(() => reconciliationRows(reconciliations ?? []), [reconciliations]);
  const estimates = useMemo(() => (ledger?.entries ?? []).filter((entry) => entry.charge_state === "estimated"), [ledger]);
  const errors = useMemo(() => (mode === "late_charge" ? lateChargeFormErrors(form) : []), [form, mode]);
  const update = (patch: Partial<LateChargeForm>) => setForm((current) => ({ ...current, ...patch }));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (errors.length) return;
    setPending(true);
    setError(null);
    try {
      const body = buildReconciliationRequest(runId, currency, isoNow(), {
        lateCharge: mode === "late_charge" ? form : undefined,
        costLimit: form.cost_limit,
        unconditionalSuccesses: form.unconditional_successes,
      });
      const response = await fetch(`/api/evaluation/runs/${encodeURIComponent(runId)}/reconciliations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json() as (LateChargeOutcome & { record?: { reconciliation_version?: number } }) & { error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The reconciliation failed"));
      if (mode === "late_charge") {
        setOutcome(data);
        toast({ type: "success", message: `Late charge stored as reconciliation version ${data.reconciliation_version}${data.analysis_recompute_required ? `, ${data.recomputed_analysis_snapshots.length} snapshot(s) recomputed` : ""}.` });
      } else {
        setOutcome(null);
        toast({ type: "success", message: `Reconciliation version ${data.record?.reconciliation_version ?? "stored"} recorded.` });
      }
      setMode("idle");
      setForm(defaultLateChargeForm());
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The reconciliation failed");
    } finally {
      setPending(false);
    }
  };

  const summary = ledger?.summary;
  return (
    <section className="benchmark-catalog resource-ledger" aria-labelledby="resource-ledger-title">
      <header className="dataset-catalog__toolbar">
        <div>
          <h3 id="resource-ledger-title">Resource ledger</h3>
          <span>{ledger ? `${ledger.entries.length} entries · ${versions.length} reconciliation version${versions.length === 1 ? "" : "s"}` : "Loading"}</span>
        </div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
          <ActionButton variant={mode === "reconcile" ? "secondary" : "primary"} onClick={() => setMode((current) => (current === "reconcile" ? "idle" : "reconcile"))}>{mode === "reconcile" ? "Close" : "Reconcile now"}</ActionButton>
          <ActionButton variant={mode === "late_charge" ? "secondary" : "primary"} onClick={() => setMode((current) => (current === "late_charge" ? "idle" : "late_charge"))}><Coins size={15} /> {mode === "late_charge" ? "Close" : "Apply late charge"}</ActionButton>
        </div>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      {mode !== "idle" ? (
        <form className="benchmark-form ledger-form" onSubmit={submit} aria-labelledby="ledger-form-title">
          <header><div><h4 id="ledger-form-title">{mode === "late_charge" ? "Apply one late charge" : "Reconcile the ledger"}</h4><p>{mode === "late_charge" ? "The late charge stores as a new confirmed entry that references its estimate. The estimate never changes. When the charge changes a cost rule outcome, every stored gate supersedes and every current analysis snapshot recomputes." : "Reconciliation opens the next settlement version from the entries stored so far. Every version stays readable."}</p></div></header>
          {mode === "late_charge" ? (
            <>
              <div className="benchmark-form__grid">
                <label>Estimate entry<Select value={form.estimate_entry_id} onChange={(event) => setForm((current) => lateChargeFromEstimate(current, estimates.find((entry) => entry.entry_id === event.target.value) ?? null))}><option value="">No linked estimate</option>{estimates.map((entry) => <option key={entry.entry_id} value={entry.entry_id}>{entry.entry_id} · {statusWords(entry.resource_class)} · {moneyText(entry.estimate?.value)}</option>)}</Select></label>
                <label>Resource class<Select value={form.resource_class} onChange={(event) => update({ resource_class: event.target.value })}>{RESOURCE_CLASSES.map((name) => <option key={name} value={name}>{statusWords(name)}</option>)}</Select></label>
                <label>Provider<input required value={form.provider} onChange={(event) => update({ provider: event.target.value })} /></label>
                <label>Service<input required value={form.service} onChange={(event) => update({ service: event.target.value })} /></label>
                <label>Region<input required value={form.region} onChange={(event) => update({ region: event.target.value })} /></label>
                <label>Quantity<input required type="number" min={0} step="any" value={form.quantity} onChange={(event) => update({ quantity: Number(event.target.value) })} /></label>
                <label>Unit<input required value={form.unit} onChange={(event) => update({ unit: event.target.value })} /></label>
                <label>Pricing version<input required value={form.pricing_version} onChange={(event) => update({ pricing_version: event.target.value })} /></label>
                <label>Charged amount ({currency})<input required value={form.amount} onChange={(event) => update({ amount: event.target.value })} placeholder="0.40" /></label>
                <label>Provider amount text<input required value={form.provider_text} onChange={(event) => update({ provider_text: event.target.value })} placeholder="$0.40" /></label>
                <label>Evidence source<input required value={form.source} onChange={(event) => update({ source: event.target.value })} /></label>
                <label>Invoice reference<input value={form.invoice_reference} onChange={(event) => update({ invoice_reference: event.target.value })} /></label>
              </div>
            </>
          ) : null}
          <fieldset className="benchmark-form__section"><legend>Cost rules</legend><div className="benchmark-form__grid">
            <label>Actual total at most ({currency})<input value={form.cost_limit} onChange={(event) => update({ cost_limit: event.target.value })} placeholder="Leave empty for no rule" /></label>
            <label>Unconditional successes<input value={form.unconditional_successes} onChange={(event) => update({ unconditional_successes: event.target.value })} placeholder="For cost per success" /></label>
          </div></fieldset>
          {errors.length ? <ul className="benchmark-report__warnings metric-form__errors" aria-label="Ledger problems">{errors.map((entry) => <li key={entry}>{entry}</li>)}</ul> : null}
          <div className="benchmark-form__actions"><ActionButton type="submit" loading={pending} disabled={errors.length > 0}>{mode === "late_charge" ? "Store late charge" : "Record reconciliation"}</ActionButton></div>
        </form>
      ) : null}
      {outcome ? (
        <div className="verdict-banner verdict-banner--indeterminate" role="status" aria-label="Late charge outcome">
          <strong>Late charge {outcome.entry_id} opened reconciliation version {outcome.reconciliation_version}</strong>
          <span>{outcome.cost_rule_changed ? `A cost rule outcome changed: ${outcome.superseded_gates} gate(s) superseded and ${outcome.recomputed_analysis_snapshots.length} of ${outcome.affected_analysis_snapshot_ids.length} snapshot(s) recomputed.` : "No cost rule outcome changed, so the stored gates and snapshots stand."}</span>
          {outcome.recompute_failures.length ? <small>{outcome.recompute_failures.length} recompute failure(s) need attention.</small> : null}
        </div>
      ) : null}
      {summary ? (
        <dl className="benchmark-metadata ledger-summary" aria-label="Ledger totals">
          <div><dt>Estimate total</dt><dd>{moneyText(summary.estimate_total)}</dd></div>
          <div><dt>Actual total</dt><dd>{moneyText(summary.actual_total)}</dd></div>
          <div><dt>Estimate error</dt><dd>{moneyText(summary.estimate_error_total)}<small>{summary.entries_with_both} entries carry both</small></dd></div>
          <div><dt>Unknown prices</dt><dd><span className={`benchmark-status benchmark-status--${summary.unknown_entry_ids.length ? "failed" : "passed"}`}>{summary.unknown_entry_ids.length}</span></dd></div>
          <div><dt>Not billable</dt><dd>{summary.not_billable_entry_ids.length}</dd></div>
        </dl>
      ) : null}
      {ledger && ledger.entries.length === 0 ? <ResourceState kind="empty" title="No ledger entries" description="Entries arrive automatically from runtime settlement, scoring, judging, storage, imports, and human review." compact /> : null}
      {classes.length ? (
        <div className="benchmark-table-wrap">
          <table className="benchmark-table ledger-classes">
            <caption>Totals per resource class{summary?.no_use_classes.length ? `. No use recorded for ${summary.no_use_classes.map(statusWords).join(", ")}.` : ""}</caption>
            <thead><tr><th>Class</th><th>Entries</th><th>Estimate</th><th>Actual</th><th>Difference</th></tr></thead>
            <tbody>{classes.map((row) => <tr key={row.resourceClass}><td>{row.label}</td><td>{row.entries}</td><td>{row.estimateText}</td><td>{row.actualText}</td><td>{row.differenceText}</td></tr>)}</tbody>
          </table>
        </div>
      ) : null}
      {flagged.unknown.length || flagged.notBillable.length ? (
        <div className="ledger-flags">
          {flagged.unknown.length ? <div className="verdict-banner verdict-banner--failed"><strong>{flagged.unknown.length} entr{flagged.unknown.length === 1 ? "y" : "ies"} with an unknown price</strong><span>Every cost rule fails closed until these confirm: {flagged.unknown.map((entry) => entry.entry_id).join(", ")}.</span></div> : null}
          {flagged.notBillable.length ? <div className="verdict-banner verdict-banner--indeterminate"><strong>{flagged.notBillable.length} not-billable entr{flagged.notBillable.length === 1 ? "y" : "ies"}</strong><span>{flagged.notBillable.map((entry) => `${statusWords(entry.resource_class)}: ${entry.not_billable_evidence ?? "no evidence text"}`).join(" · ")}</span></div> : null}
        </div>
      ) : null}
      {rows.length ? (
        <details className="ledger-entries">
          <summary>Every entry ({rows.length})</summary>
          <div className="benchmark-table-wrap">
            <table className="benchmark-table">
              <thead><tr><th>Entry</th><th>Class</th><th>Source</th><th>Quantity</th><th>Estimate</th><th>Actual</th><th>State</th><th>Reference</th></tr></thead>
              <tbody>{rows.map((row) => <tr key={row.entryId}><td><code>{row.entryId}</code>{row.estimateEntryId ? <small>confirms {row.estimateEntryId}</small> : null}</td><td>{row.resourceClass}</td><td>{row.source}</td><td>{row.quantity}</td><td>{row.estimate}</td><td>{row.actual}</td><td><span className={`benchmark-status benchmark-status--${row.tone}`}>{row.chargeState}</span></td><td>{row.reference}</td></tr>)}</tbody>
            </table>
          </div>
        </details>
      ) : null}
      <div className="benchmark-table-wrap">
        <table className="benchmark-table ledger-reconciliations">
          <caption>Reconciliation versions, newest first. A version never changes; a late charge opens the next one.</caption>
          <thead><tr><th>Version</th><th>Reason</th><th>Reconciled</th><th>Actual total</th><th>Per success</th><th>Cost rules</th><th>Supersedes</th></tr></thead>
          <tbody>
            {versions.length === 0 ? <tr><td colSpan={7}>{reconciliations ? "No reconciliation yet. Reconcile now to record version 1." : "Loading"}</td></tr> : null}
            {versions.map((row) => (
              <tr key={row.id} data-late-charge={row.lateCharge ? "true" : "false"}>
                <td><strong>{row.version}</strong><small>{row.id}</small></td>
                <td><span className={`benchmark-status benchmark-status--${row.lateCharge ? "provisional" : "passed"}`}>{row.reasonLabel}</span></td>
                <td>{new Date(row.reconciledAt).toLocaleString()}</td>
                <td>{row.actualTotal}<small>estimate {row.estimateTotal}</small></td>
                <td>{row.costPerSuccess}{row.unconditionalSuccesses !== null ? <small>{row.unconditionalSuccesses} successes</small> : null}</td>
                <td>{row.rules.length === 0 ? "None" : row.rules.map((rule) => <div key={rule.label}><span className={`benchmark-status benchmark-status--${rule.tone}`}>{rule.status}</span> {rule.label}<small>observed {rule.observed}</small></div>)}{row.unknownEntries ? <small>{row.unknownEntries} unknown price(s)</small> : null}</td>
                <td>{row.supersedes ? <code>{row.supersedes}</code> : "First version"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
