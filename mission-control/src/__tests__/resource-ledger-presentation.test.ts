/**
 * The ledger presentation totals every class, flags unknown and
 * not-billable entries, orders reconciliation versions, and builds one
 * late charge that references its estimate.
 */
import { describe, expect, it } from "vitest";

import { moneyText, nanosFromText, type LedgerEntry, type LedgerSummary, type StoredReconciliation } from "@/lib/evaluation-operations";
import {
  buildLateChargeEntry,
  buildReconciliationRequest,
  classRows,
  defaultLateChargeForm,
  entryRows,
  flaggedEntries,
  lateChargeFormErrors,
  lateChargeFromEstimate,
  reconciliationRows,
} from "@/lib/resource-ledger-presentation";

const usd = (nanos: number) => ({ currency: "USD", amount_nanos: nanos });

function entry(overrides: Partial<LedgerEntry>): LedgerEntry {
  return {
    entry_id: "entry-a", resource_class: "runtime", provider: "provider-a", service: "chat", region: "us-east",
    quantity: { value: 1000, unit: "tokens" }, pricing_version: "pricing-1", charge_state: "estimated",
    estimate: { value: usd(100000000), method: "list_price", estimated_at: "2026-09-01T00:00:00Z" },
    references: { run_id: "run-a", attempt_id: "attempt-1" }, reservation_id: null, reconciliation_id: null, estimate_entry_id: null,
    recorded_at: "2026-09-01T00:00:00Z", ...overrides,
  };
}

const summary: LedgerSummary = {
  currency: "USD", estimate_total: usd(100000000), actual_total: usd(120000000), estimate_error_total: usd(20000000), entries_with_both: 1,
  unknown_entry_ids: ["entry-unknown"], not_billable_entry_ids: ["entry-storage"],
  per_class: { runtime: { estimate: usd(100000000), actual: usd(120000000), entries: 2 }, storage: { estimate: usd(0), actual: usd(0), entries: 1 } },
  no_use_classes: ["judge"],
};

describe("resource ledger presentation", () => {
  it("formats money and parses decimal text into nanos", () => {
    expect(moneyText(usd(120000000))).toBe("0.12 USD");
    expect(moneyText(usd(0))).toBe("0 USD");
    expect(moneyText(null)).toBe("Unavailable");
    expect(nanosFromText("0.40")).toBe(400000000);
    expect(nanosFromText("2")).toBe(2000000000);
    expect(nanosFromText("abc")).toBeNull();
  });

  it("totals every class with the difference against the estimate", () => {
    const rows = classRows(summary);
    expect(rows.map((row) => row.resourceClass)).toEqual(["runtime", "storage"]);
    expect(rows[0]).toMatchObject({ label: "Runtime", entries: 2, estimateText: "0.1 USD", actualText: "0.12 USD", differenceText: "+0.02 USD" });
    expect(rows[1].differenceText).toBe("Matches estimate");
    expect(classRows(null)).toEqual([]);
  });

  it("flags the unknown and the not-billable entries", () => {
    const entries = [entry({}), entry({ entry_id: "entry-unknown", charge_state: "unknown", estimate: undefined }), entry({ entry_id: "entry-storage", resource_class: "storage", charge_state: "not_billable", estimate: undefined, not_billable_evidence: "local disk" })];
    const flagged = flaggedEntries(entries, summary);
    expect(flagged.unknown.map((item) => item.entry_id)).toEqual(["entry-unknown"]);
    expect(flagged.notBillable.map((item) => item.entry_id)).toEqual(["entry-storage"]);
    const rows = new Map(entryRows(entries).map((row) => [row.entryId, row]));
    expect(rows.get("entry-unknown")).toMatchObject({ estimate: "Unknown price", tone: "failed", chargeState: "Unknown" });
    expect(rows.get("entry-storage")).toMatchObject({ actual: "local disk", tone: "paused" });
    expect(rows.get("entry-a")).toMatchObject({ source: "provider-a · chat", quantity: "1,000 tokens", reference: "attempt-1" });
  });

  it("orders reconciliation versions newest first with their rule outcomes", () => {
    const stored = (version: number, reason: string, supersedes: string | null): StoredReconciliation => ({
      id: `settlement-${version}`, run_id: "run-a", settlement_version: version, record_checksum: "c".repeat(64), created_at: "2026-09-01T00:00:00Z",
      record: {
        reconciliation_version: version, reason, reconciled_at: "2026-09-01T00:00:00Z", currency: "USD", summary, reservations: [], unmatched_reservation_references: ["reservation-x"],
        cost_rules: [{ metric: "actual_total", operator: "lte", limit: usd(150000000), observed: usd(120000000), status: version === 2 ? "failed" : "passed" }],
        cost_per_success: usd(60000000), unconditional_successes: 2, supersedes_reconciliation: supersedes, entry_ids: ["entry-a"],
      },
    });
    const rows = reconciliationRows([stored(1, "scheduled", null), stored(2, "late_charge", "settlement-1")]);
    expect(rows.map((row) => row.version)).toEqual([2, 1]);
    expect(rows[0]).toMatchObject({ reasonLabel: "Late charge", lateCharge: true, supersedes: "settlement-1", actualTotal: "0.12 USD", costPerSuccess: "0.06 USD", unknownEntries: 1, unmatchedReservations: 1 });
    expect(rows[0].rules[0]).toMatchObject({ label: "Actual total ≤ 0.15 USD", status: "Failed", tone: "failed", observed: "0.12 USD" });
    expect(rows[1].rules[0].tone).toBe("passed");
  });

  it("prefills a late charge from its estimate and builds the confirmed entry", () => {
    const estimate = entry({ entry_id: "estimate-1" });
    const form = { ...lateChargeFromEstimate(defaultLateChargeForm(), estimate), amount: "0.40", provider_text: "$0.40", invoice_reference: "INV-1" };
    expect(form).toMatchObject({ estimate_entry_id: "estimate-1", resource_class: "runtime", provider: "provider-a", quantity: 1000, unit: "tokens" });
    expect(lateChargeFormErrors(form)).toEqual([]);
    expect(lateChargeFormErrors({ ...form, amount: "x" })).toContain("The charged amount is a decimal such as 0.40.");
    const built = buildLateChargeEntry(form, "run-a", "USD", "2026-09-04T00:00:00Z", "late-charge-1");
    expect(built).toMatchObject({
      schema_id: "resource-ledger-entry", schema_version: 2, entry_id: "late-charge-1", charge_state: "confirmed", estimate_entry_id: "estimate-1",
      actual: { value: usd(400000000), evidence: { provider_text: "$0.40", source: "invoice", invoice_reference: "INV-1" }, charged_at: "2026-09-04T00:00:00Z" },
      references: { run_id: "run-a" }, recorded_at: "2026-09-04T00:00:00Z",
    });
    const request = buildReconciliationRequest("run-a", "USD", "2026-09-04T00:00:00Z", { lateCharge: form, costLimit: "0.15", unconditionalSuccesses: "2" });
    expect(request.cost_rules).toEqual([{ metric: "actual_total", operator: "lte", value: usd(150000000) }]);
    expect(request.unconditional_successes).toBe(2);
    expect(request.late_charge?.charge_state).toBe("confirmed");
    const plain = buildReconciliationRequest("run-a", "USD", "2026-09-04T00:00:00Z", {});
    expect(plain).toEqual({ currency: "USD", cost_rules: [], reconciled_at: "2026-09-04T00:00:00Z" });
  });
});
