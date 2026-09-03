"""The complete resource ledger with separate estimates and charges.

One immutable ledger entry records every resource event across
runtime and control-plane models, deterministic scorers and judges,
environment compute and external tools, import and transformation
compute, artifact and trace storage, and human review. Every
authoritative monetary value is Foundation ``Money``; a provider
decimal string stays only inside charge evidence. An estimate and an
actual charge live in separate entries and an actual charge never
replaces its estimate. Reconciliation compares every reservation
against every observed entry, keeps unknown amounts unknown, keeps
not-billable evidence, totals under the declared currency policy,
computes cost per success with the unconditional denominator, and a
late charge creates the next reconciliation version, reopens
settlement, and supersedes every affected gate when it changes a
cost rule.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import database as db
from benchmarks.costs import BENCHMARK_CURRENCY, money_from_json, money_to_json
from benchmarks.evaluation_contracts import validate_record
from core.money import Money

RESOURCE_CLASSES = (
    "runtime",
    "control_plane",
    "scorer",
    "judge",
    "environment",
    "external_tool",
    "import",
    "transformation",
    "storage",
    "human_review",
)

# The classes every run ledger reports: an entry or an explicit
# statement that no use occurred.
REQUIRED_CLASSES = (
    "runtime", "scorer", "judge", "environment", "import", "storage",
    "human_review",
)

CHARGE_STATES = ("estimated", "confirmed", "unknown", "not_billable")


class ResourceLedgerError(ValueError):
    """The ledger request violates the resource ledger contract."""


# ── Entries ──────────────────────────────────────────────────────────


def ledger_entry(
    *,
    run_id: str,
    resource_class: str,
    provider: str,
    service: str,
    region: str,
    quantity: float,
    unit: str,
    pricing_version: str,
    estimate: Money | None = None,
    estimate_method: str = "list_price",
    actual: Money | None = None,
    actual_provider_text: str | None = None,
    actual_source: str = "usage_report",
    invoice_reference: str | None = None,
    not_billable_evidence: str | None = None,
    price_unknown: bool = False,
    attempt_id: str | None = None,
    activation_id: str | None = None,
    scorer_id: str | None = None,
    import_id: str | None = None,
    retry_of: str | None = None,
    reservation_id: str | None = None,
    estimate_entry_id: str | None = None,
    now: str = "1970-01-01T00:00:00Z",
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Build one validating ledger entry with its derived charge state.

    An actual charge parses at the trusted boundary and keeps the
    original provider string as evidence. An unknown price stores no
    amount and stays unknown. A not-billable event stores its
    evidence and never becomes an unknown zero.
    """
    if resource_class not in RESOURCE_CLASSES:
        raise ResourceLedgerError(
            f"Unknown resource class: {resource_class!r}"
        )
    for name, value in (("estimate", estimate), ("actual", actual)):
        if value is not None and not isinstance(value, Money):
            raise ResourceLedgerError(
                f"The {name} value is Money(currency, amount_nanos)"
            )
    if not_billable_evidence is not None:
        if actual is not None:
            raise ResourceLedgerError(
                "A not-billable event carries no actual charge"
            )
        charge_state = "not_billable"
    elif actual is not None:
        if not actual_provider_text:
            raise ResourceLedgerError(
                "An actual charge keeps the original provider text"
            )
        charge_state = "confirmed"
    elif price_unknown:
        if estimate is not None:
            raise ResourceLedgerError(
                "An unknown price records no estimate amount"
            )
        charge_state = "unknown"
    elif estimate is not None:
        charge_state = "estimated"
    else:
        raise ResourceLedgerError(
            "An entry records an estimate, an actual charge, an unknown "
            "price, or not-billable evidence"
        )
    references: dict[str, str] = {"run_id": run_id}
    for name, value in (
        ("attempt_id", attempt_id), ("activation_id", activation_id),
        ("scorer_id", scorer_id), ("import_id", import_id),
        ("retry_of", retry_of),
    ):
        if value:
            references[name] = str(value)
    record: dict[str, Any] = {
        "schema_id": "resource-ledger-entry",
        "schema_version": 2,
        "entry_id": entry_id or f"ledger-{uuid.uuid4().hex}",
        "resource_class": resource_class,
        "provider": provider,
        "service": service,
        "region": region,
        "quantity": {"value": float(quantity), "unit": unit},
        "pricing_version": pricing_version,
        "charge_state": charge_state,
        "references": references,
        "reservation_id": reservation_id,
        "reconciliation_id": None,
        "estimate_entry_id": estimate_entry_id,
        "recorded_at": now,
    }
    if estimate is not None:
        record["estimate"] = {
            "value": money_to_json(estimate),
            "method": estimate_method,
            "estimated_at": now,
        }
    if actual is not None:
        evidence: dict[str, str] = {
            "provider_text": str(actual_provider_text),
            "source": actual_source,
        }
        if invoice_reference:
            evidence["invoice_reference"] = invoice_reference
        record["actual"] = {
            "value": money_to_json(actual),
            "evidence": evidence,
            "charged_at": now,
        }
    if not_billable_evidence is not None:
        record["not_billable_evidence"] = not_billable_evidence
    validate_record(record)
    return record


def actual_from_provider_text(
    currency: str, provider_text: str,
) -> Money:
    """Parse one provider decimal string at the trusted boundary."""
    return Money.from_decimal_string(currency, provider_text)


async def record_event(record: dict[str, Any]) -> dict[str, Any]:
    """Store one ledger entry through the one facade.

    The entry redacts under the data-class policy before it stores,
    so provider evidence never carries a credential into the ledger.
    """
    from benchmarks import facade
    from benchmarks.data_classes import redact

    record = redact(record)
    saved = await facade.execute(
        "record_resource_event",
        {
            "record": record,
            "run_id": record["references"]["run_id"],
            "reconciliation_id": record.get("reconciliation_id"),
        },
    )
    return {"entry_id": saved["id"], "record": record}


# ── Automatic emission from the execution paths ──────────────────────

# The pricing basis of an entry the daemon derives from observed use.
USAGE_PRICING_VERSION = "observed-usage"
RUNTIME_PROVIDER = "litellm-gateway"
LOCAL_COMPUTE_EVIDENCE = (
    "local compute inside the daemon boundary; no provider invoice"
)
LOCAL_STORAGE_EVIDENCE = (
    "content-addressed local artifact store; no provider invoice"
)


async def attempt_run_id(attempt_id: str) -> str | None:
    """Resolve the run of one attempt through its trial."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT trial.run_id AS run_id FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE attempt.id = ?",
            (attempt_id,),
        )
        row = await cursor.fetchone()
    return str(row["run_id"]) if row else None


async def _entry_exists(entry_id: str) -> bool:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT 1 FROM resource_ledger_entries WHERE id = ?", (entry_id,),
        )
        return await cursor.fetchone() is not None


async def _now(now: str | None) -> str:
    return now or await db.database_utc_now()


async def emit_entry(record: dict[str, Any]) -> dict[str, Any] | None:
    """Store one derived entry exactly once by its deterministic id."""
    if await _entry_exists(str(record["entry_id"])):
        return None
    return await record_event(record)


def _decimal_text(value: float) -> str:
    return f"{float(value):.9f}"


async def emit_runtime_usage(
    attempt: dict[str, Any], *, now: str | None = None,
) -> dict[str, Any] | None:
    """Record the runtime use of one finished attempt.

    The observed task cost becomes the actual charge with the reported
    provider text as evidence; a task with no reported usage records
    an unknown price that never becomes zero.
    """
    attempt_id = str(attempt["id"])
    run_id = str(attempt.get("run_id") or await attempt_run_id(attempt_id) or "")
    if not run_id:
        return None
    tokens = attempt.get("total_tokens")
    raw_cost = attempt.get("total_cost_usd")
    when = await _now(now)
    arguments: dict[str, Any] = {
        "run_id": run_id,
        "resource_class": "runtime",
        "provider": RUNTIME_PROVIDER,
        "service": str(attempt.get("model_used") or "unknown-model"),
        "region": "unspecified",
        "quantity": float(tokens) if tokens is not None else 1.0,
        "unit": "tokens" if tokens is not None else "attempts",
        "pricing_version": USAGE_PRICING_VERSION,
        "attempt_id": attempt_id,
        "reservation_id": f"benchmark-reservation-{attempt_id}",
        "retry_of": attempt.get("retry_of") or None,
        "now": when,
        "entry_id": f"ledger-runtime-{attempt_id}",
    }
    if raw_cost is not None:
        text = _decimal_text(float(raw_cost))
        arguments["actual"] = Money.from_decimal_string(
            BENCHMARK_CURRENCY, text,
        )
        arguments["actual_provider_text"] = text
        arguments["actual_source"] = "usage_report"
    else:
        arguments["price_unknown"] = True
    return await emit_entry(ledger_entry(**arguments))


async def emit_scorer_execution(
    *,
    attempt_id: str,
    scorer_id: str,
    outcome: dict[str, Any],
    boundary: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Record the scorer compute of one score execution."""
    run_id = await attempt_run_id(attempt_id)
    if run_id is None:
        return None
    resources = outcome.get("resources") or {}
    fuel = resources.get("fuel_used")
    cpu_seconds = resources.get("cpu_seconds")
    if boundary == "native_microvm" and cpu_seconds is not None:
        quantity, unit = float(cpu_seconds), "vcpu-seconds"
    else:
        quantity, unit = float(fuel or 0), "fuel"
    return await emit_entry(ledger_entry(
        run_id=run_id,
        resource_class="scorer",
        provider="daemon",
        service=boundary,
        region="local",
        quantity=quantity,
        unit=unit,
        pricing_version=USAGE_PRICING_VERSION,
        not_billable_evidence=LOCAL_COMPUTE_EVIDENCE,
        attempt_id=attempt_id,
        scorer_id=scorer_id,
        now=await _now(now),
        entry_id=f"ledger-scorer-{attempt_id}-{scorer_id}-{outcome.get('result_digest') or outcome.get('terminal_class')}"[:200],
    ))


async def emit_judge_usage(
    *,
    attempt_id: str,
    scorer_id: str,
    judge: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any] | None:
    """Record the judge model use behind one rubric score."""
    run_id = await attempt_run_id(attempt_id)
    if run_id is None:
        return None
    usage = judge.get("usage") or {}
    tokens = usage.get("total_tokens")
    arguments: dict[str, Any] = {
        "run_id": run_id,
        "resource_class": "judge",
        "provider": str(judge.get("provider") or RUNTIME_PROVIDER),
        "service": str(judge.get("model") or "judge-model"),
        "region": "unspecified",
        "quantity": float(tokens) if tokens is not None else 1.0,
        "unit": "tokens" if tokens is not None else "requests",
        "pricing_version": USAGE_PRICING_VERSION,
        "attempt_id": attempt_id,
        "scorer_id": scorer_id,
        "now": await _now(now),
        "entry_id": f"ledger-judge-{attempt_id}-{judge.get('request_digest') or scorer_id}"[:200],
    }
    cost_text = usage.get("provider_cost_text")
    if cost_text:
        arguments["actual"] = Money.from_decimal_string(
            BENCHMARK_CURRENCY, str(cost_text),
        )
        arguments["actual_provider_text"] = str(cost_text)
    else:
        arguments["price_unknown"] = True
    return await emit_entry(ledger_entry(**arguments))


async def emit_storage(
    *,
    attempt_id: str,
    byte_count: int,
    artifact_count: int,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Record the evidence bytes persisted for one attempt."""
    run_id = await attempt_run_id(attempt_id)
    if run_id is None:
        return None
    return await emit_entry(ledger_entry(
        run_id=run_id,
        resource_class="storage",
        provider="daemon",
        service=f"evidence-artifacts:{int(artifact_count)}",
        region="local",
        quantity=float(byte_count),
        unit="bytes",
        pricing_version=USAGE_PRICING_VERSION,
        not_billable_evidence=LOCAL_STORAGE_EVIDENCE,
        attempt_id=attempt_id,
        now=await _now(now),
        entry_id=f"ledger-storage-{attempt_id}",
    ))


async def emit_human_review(
    *,
    attempt_id: str,
    review_id: str,
    reviewer_id: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Record one human review as reviewer time with an unknown price."""
    run_id = await attempt_run_id(attempt_id)
    if run_id is None:
        return None
    return await emit_entry(ledger_entry(
        run_id=run_id,
        resource_class="human_review",
        provider="reviewers",
        service=str(reviewer_id),
        region="unspecified",
        quantity=1.0,
        unit="reviews",
        pricing_version=USAGE_PRICING_VERSION,
        price_unknown=True,
        attempt_id=attempt_id,
        now=await _now(now),
        entry_id=f"ledger-review-{review_id}",
    ))


async def emit_import(
    *,
    run_id: str,
    import_id: str,
    byte_count: int,
    source: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Record the bytes one run-scoped import ingested."""
    return await emit_entry(ledger_entry(
        run_id=run_id,
        resource_class="import",
        provider="daemon",
        service=str(source),
        region="local",
        quantity=float(byte_count),
        unit="bytes",
        pricing_version=USAGE_PRICING_VERSION,
        not_billable_evidence=LOCAL_STORAGE_EVIDENCE,
        import_id=import_id,
        now=await _now(now),
        entry_id=f"ledger-import-{import_id}",
    ))


async def list_entries(run_id: str) -> list[dict[str, Any]]:
    """Read every ledger entry for one run in recording order."""
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT record FROM resource_ledger_entries WHERE run_id = ? "
            "ORDER BY created_at, id",
            (run_id,),
        )
    return [json.loads(row["record"]) for row in rows]


# ── Totals ───────────────────────────────────────────────────────────


def summarize(
    entries: list[dict[str, Any]], *, currency: str,
) -> dict[str, Any]:
    """Total the ledger under one currency policy.

    Estimates and actual charges total separately. A foreign currency
    fails the total instead of converting, an unknown amount stays
    unknown, and a not-billable event contributes its evidence and no
    amount. Every required class reports an entry or "no use".
    """
    estimate_total = Money.zero(currency)
    actual_total = Money.zero(currency)
    per_class: dict[str, dict[str, Any]] = {}
    unknown_ids: list[str] = []
    not_billable_ids: list[str] = []
    estimate_error_total = Money.zero(currency)
    compared = 0
    for entry in entries:
        resource_class = str(entry["resource_class"])
        bucket = per_class.setdefault(resource_class, {
            "estimate": Money.zero(currency),
            "actual": Money.zero(currency),
            "entries": 0,
        })
        bucket["entries"] += 1
        state = str(entry["charge_state"])
        if state == "unknown":
            unknown_ids.append(str(entry["entry_id"]))
            continue
        if state == "not_billable":
            not_billable_ids.append(str(entry["entry_id"]))
            continue
        if "estimate" in entry:
            estimate = money_from_json(entry["estimate"]["value"])
            if estimate.currency != currency:
                raise ResourceLedgerError(
                    f"The entry {entry['entry_id']} estimates in "
                    f"{estimate.currency}; the policy totals {currency} "
                    "and never converts without a versioned rate"
                )
            estimate_total = estimate_total.add(estimate)
            bucket["estimate"] = bucket["estimate"].add(estimate)
        if "actual" in entry:
            actual = money_from_json(entry["actual"]["value"])
            if actual.currency != currency:
                raise ResourceLedgerError(
                    f"The entry {entry['entry_id']} charges in "
                    f"{actual.currency}; the policy totals {currency} "
                    "and never converts without a versioned rate"
                )
            actual_total = actual_total.add(actual)
            bucket["actual"] = bucket["actual"].add(actual)
            if "estimate" in entry:
                estimate_error_total = estimate_error_total.add(
                    actual.subtract(
                        money_from_json(entry["estimate"]["value"]),
                    ),
                )
                compared += 1
    return {
        "currency": currency,
        "estimate_total": money_to_json(estimate_total),
        "actual_total": money_to_json(actual_total),
        "estimate_error_total": money_to_json(estimate_error_total),
        "entries_with_both": compared,
        "unknown_entry_ids": unknown_ids,
        "not_billable_entry_ids": not_billable_ids,
        "per_class": {
            name: {
                "estimate": money_to_json(bucket["estimate"]),
                "actual": money_to_json(bucket["actual"]),
                "entries": bucket["entries"],
            }
            for name, bucket in sorted(per_class.items())
        },
        "no_use_classes": sorted(
            name for name in REQUIRED_CLASSES if name not in per_class
        ),
    }


def cost_per_success(
    total: Money, unconditional_successes: int,
) -> dict[str, Any] | None:
    """Divide the total by the unconditional success count.

    The denominator counts every successful attempt over every planned
    attempt, never only the attempts that completed. Zero successes
    leave the value undefined instead of infinite or zero.
    """
    if unconditional_successes <= 0:
        return None
    return money_to_json(total.scale_ratio(1, unconditional_successes))


# ── Cost rules ───────────────────────────────────────────────────────


def evaluate_cost_rules(
    rules: list[dict[str, Any]], summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Evaluate declared cost rules against one ledger summary.

    An unknown amount fails every cost rule closed; it never passes
    as zero.
    """
    outcomes = []
    for rule in rules:
        metric = str(rule.get("metric") or "actual_total")
        limit = money_from_json(rule["value"])
        observed = money_from_json(summary[metric])
        if summary["unknown_entry_ids"]:
            status = "failed_unknown"
        elif str(rule.get("operator") or "lte") == "lte":
            status = "passed" if observed.fits_within(limit) else "failed"
        else:
            raise ResourceLedgerError(
                f"Unknown cost rule operator: {rule.get('operator')!r}"
            )
        outcomes.append({
            "metric": metric,
            "operator": str(rule.get("operator") or "lte"),
            "limit": money_to_json(limit),
            "observed": money_to_json(observed),
            "status": status,
        })
    return outcomes


# ── Reconciliation ───────────────────────────────────────────────────


async def _reservations_for_run(run_id: str) -> list[dict[str, Any]]:
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT reservation_id, state, currency, "
            "requested_amount_nanos, reserved_amount_nanos, "
            "consumed_amount_nanos, released_amount_nanos "
            "FROM budget_reservations WHERE run_id = ? "
            "ORDER BY reservation_id",
            (run_id,),
        )
    return [dict(row) for row in rows]


async def _latest_reconciliation(
    run_id: str,
) -> tuple[int, dict[str, Any] | None]:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT settlement_version, record FROM cost_settlement_versions "
            "WHERE run_id = ? ORDER BY settlement_version DESC LIMIT 1",
            (run_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return 0, None
    return int(row["settlement_version"]), json.loads(row["record"])


async def reconcile_run(
    run_id: str,
    *,
    currency: str,
    cost_rules: list[dict[str, Any]] | None = None,
    unconditional_successes: int | None = None,
    supersedes_gates: bool = False,
    reason: str = "scheduled",
    now: str = "1970-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Reconcile every reservation against every observed entry.

    The reconciliation stores as the next immutable settlement
    version. It keeps estimates and actual charges separate, lists
    unknown and not-billable entries, compares each reservation with
    the ledger, records cost rule outcomes, and computes cost per
    success with the unconditional denominator.
    """
    from benchmarks import evaluation_records

    entries = await list_entries(run_id)
    summary = summarize(entries, currency=currency)
    reservations = await _reservations_for_run(run_id)
    by_reservation: dict[str, dict[str, Any]] = {}
    for entry in entries:
        reservation_id = entry.get("reservation_id")
        if not reservation_id:
            continue
        bucket = by_reservation.setdefault(str(reservation_id), {
            "estimate": Money.zero(currency),
            "actual": Money.zero(currency),
            "entry_ids": [],
        })
        bucket["entry_ids"].append(entry["entry_id"])
        if "estimate" in entry:
            bucket["estimate"] = bucket["estimate"].add(
                money_from_json(entry["estimate"]["value"]),
            )
        if "actual" in entry:
            bucket["actual"] = bucket["actual"].add(
                money_from_json(entry["actual"]["value"]),
            )
    reservation_rows = []
    for reservation in reservations:
        observed = by_reservation.get(str(reservation["reservation_id"]))
        reserved = Money(
            str(reservation["currency"]),
            int(reservation["reserved_amount_nanos"]),
        )
        actual = observed["actual"] if observed else Money.zero(currency)
        reservation_rows.append({
            "reservation_id": str(reservation["reservation_id"]),
            "state": str(reservation["state"]),
            "reserved": money_to_json(reserved),
            "consumed": money_to_json(Money(
                str(reservation["currency"]),
                int(reservation["consumed_amount_nanos"]),
            )),
            "ledger_estimate": money_to_json(
                observed["estimate"] if observed
                else Money.zero(currency),
            ),
            "ledger_actual": money_to_json(actual),
            "entry_ids": observed["entry_ids"] if observed else [],
            "overshoot": (
                actual.compare(reserved) > 0
                if reserved.currency == actual.currency else None
            ),
        })
    unmatched = sorted(
        set(by_reservation)
        - {str(reservation["reservation_id"]) for reservation in reservations}
    )
    previous_version, previous = await _latest_reconciliation(run_id)
    rule_outcomes = evaluate_cost_rules(cost_rules or [], summary)
    record: dict[str, Any] = {
        "reconciliation_version": previous_version + 1,
        "reason": reason,
        "reconciled_at": now,
        "currency": currency,
        "summary": summary,
        "reservations": reservation_rows,
        "unmatched_reservation_references": unmatched,
        "cost_rules": rule_outcomes,
        "cost_per_success": (
            cost_per_success(
                money_from_json(summary["actual_total"]),
                unconditional_successes,
            )
            if unconditional_successes is not None else None
        ),
        "unconditional_successes": unconditional_successes,
        "supersedes_reconciliation": (
            previous.get("reconciliation_id") if previous else None
        ),
        "entry_ids": [entry["entry_id"] for entry in entries],
    }
    reconciliation_id = await evaluation_records.save_cost_settlement_version(
        run_id, previous_version + 1, record,
    )
    return {"reconciliation_id": reconciliation_id, "record": record,
            "previous": previous}


def cost_rule_outcome_changed(
    previous: dict[str, Any] | None, current: dict[str, Any],
) -> bool:
    """Report whether any cost rule outcome changed between versions."""
    if previous is None:
        return False
    before = {
        (rule["metric"], rule["operator"]): rule["status"]
        for rule in previous.get("cost_rules") or []
    }
    after = {
        (rule["metric"], rule["operator"]): rule["status"]
        for rule in current.get("cost_rules") or []
    }
    return before != after


async def apply_late_charge(
    run_id: str,
    *,
    currency: str,
    entry: dict[str, Any],
    cost_rules: list[dict[str, Any]] | None = None,
    unconditional_successes: int | None = None,
    now: str = "1970-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Apply one late charge without replacing its estimate.

    The late charge stores as a new confirmed entry that references
    its estimate entry. The run gets the next reconciliation version,
    settlement reopens, and when the late charge changes any cost
    rule outcome every stored gate supersedes and every current
    analysis snapshot for the run recomputes with the new ledger
    summary; the old snapshot stays immutable behind a supersession.
    """
    from benchmarks import evaluation_records, frozen_analysis, repository

    if entry.get("charge_state") != "confirmed":
        raise ResourceLedgerError("A late charge is one confirmed entry")
    stored = await record_event(entry)
    reconciled = await reconcile_run(
        run_id,
        currency=currency,
        cost_rules=cost_rules,
        unconditional_successes=unconditional_successes,
        reason="late_charge",
        now=now,
    )
    changed = cost_rule_outcome_changed(
        reconciled["previous"], reconciled["record"],
    )
    superseded_gates = 0
    analysis_ids: list[str] = []
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT cost_status FROM benchmark_runs WHERE id = ?",
            (run_id,),
        )
        run_row = await cursor.fetchone()
        rows = await connection.execute_fetchall(
            "SELECT id FROM analysis_snapshots WHERE run_id = ? "
            "ORDER BY created_at, id",
            (run_id,),
        )
        analysis_ids = [str(row["id"]) for row in rows]
    if run_row is not None and str(run_row["cost_status"]) == "settled":
        await repository.set_run_cost_status(run_id, "settling")
    recomputed: list[dict[str, Any]] = []
    recompute_failures: list[dict[str, Any]] = []
    if changed:
        superseded_gates = await repository.supersede_gate_evaluations(
            run_id, superseded_by=reconciled["reconciliation_id"],
        )
        already_superseded = {
            str(row["snapshot_id"])
            for row in await evaluation_records.list_snapshot_supersessions(
                run_id,
            )
        }
        current_ids = [
            snapshot_id for snapshot_id in analysis_ids
            if snapshot_id not in already_superseded
        ]
        for snapshot_id in current_ids:
            try:
                recomputed.append(await frozen_analysis.recompute_snapshot(
                    snapshot_id,
                    ledger_summary=reconciled["record"]["summary"],
                    reason="late_charge_changed_cost_rule",
                    reconciliation_id=reconciled["reconciliation_id"],
                ))
            except (
                frozen_analysis.FrozenAnalysisError, KeyError, TypeError,
                ValueError,
            ) as failure:
                # A snapshot that cannot rebuild stays flagged; the
                # late charge itself never fails on it.
                recompute_failures.append({
                    "superseded_snapshot_id": snapshot_id,
                    "error": str(failure)[:500],
                })
    return {
        "entry_id": stored["entry_id"],
        "reconciliation_id": reconciled["reconciliation_id"],
        "reconciliation_version": reconciled["record"][
            "reconciliation_version"
        ],
        "cost_rule_changed": changed,
        "superseded_gates": superseded_gates,
        "analysis_recompute_required": changed and bool(analysis_ids),
        "affected_analysis_snapshot_ids": (
            [entry["superseded_snapshot_id"] for entry in recomputed]
            + [entry["superseded_snapshot_id"] for entry in recompute_failures]
            if changed else []
        ),
        "recomputed_analysis_snapshots": recomputed,
        "recompute_failures": recompute_failures,
    }
