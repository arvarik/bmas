"""Phased evaluation authority migration with durable evidence.

The migration moves through ``expand``, ``backfill``, ``dual_read``,
``cutover``, and ``contract``. Every phase change, dual-read
fallback, backfill digest mismatch, export, and direct legacy call
records as one immutable event. Backfill copies compatible legacy
records through the one canonical writer with idempotent cursors and
digest checks. Cutover stops on any recorded digest mismatch, and the
destructive contract phase refuses until every deletion gate passes.

Rollback follows the declared rules: before cutover it returns to the
legacy writer by stepping the phase back, and after cutover it keeps
the current generation as the data authority while the compatible
legacy projections turn read-only.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import database as db

PHASES = ("expand", "backfill", "dual_read", "cutover", "contract")
_FORWARD = {
    "expand": "backfill",
    "backfill": "dual_read",
    "dual_read": "cutover",
    "cutover": "contract",
}
# Rollback before cutover returns toward the legacy writer.
_ROLLBACK = {
    "backfill": "expand",
    "dual_read": "backfill",
}

BACKFILL_TARGETS = (
    "scorer_specs",
    "run_plans",
    "attempt_evidence",
    "display_exceptions",
)

# The declared deletion gates. The destructive contract phase refuses
# until every one records as passed.
DELETION_GATES = (
    "backfill_without_mismatch",
    "fallback_window_clean",
    "upgrade_fixtures_passed",
    "downgrade_fixtures_passed",
    "compatibility_export_verified",
    "operator_approval",
)


class MigrationPhaseError(RuntimeError):
    """A phase change violates the declared migration rules."""


class CutoverRefusedError(MigrationPhaseError):
    """Cutover stops on incomplete backfill or a digest mismatch."""


class ContractRefusedError(MigrationPhaseError):
    """The destructive contract phase refuses before its gates pass."""


async def get_state() -> dict[str, Any]:
    """Read the durable migration authority state."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM evaluation_migration_state WHERE id = 'authority'",
        )
        row = await cursor.fetchone()
    if row is None:
        raise MigrationPhaseError(
            "The migration authority state row does not exist"
        )
    state = dict(row)
    state["cursors"] = json.loads(state["cursors"])
    state["gates"] = json.loads(state["gates"])
    state["legacy_readonly"] = bool(state["legacy_readonly"])
    return state


async def record_event(event_type: str, payload: dict[str, Any]) -> str:
    """Append one immutable migration event."""
    event_id = f"migration-event-{uuid.uuid4().hex}"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO evaluation_migration_events "
            "(id, event_type, payload) VALUES (?, ?, ?)",
            (
                event_id,
                event_type,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
        )
        await connection.commit()
    return event_id


async def count_events(
    event_type: str, *, since: str | None = None,
) -> int:
    """Count events of one type, optionally after one timestamp."""
    clause = " AND created_at >= ?" if since else ""
    parameters: tuple[Any, ...] = (event_type,)
    if since:
        parameters = (event_type, since)
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS events FROM evaluation_migration_events "
            f"WHERE event_type = ?{clause}",
            parameters,
        )
        row = await cursor.fetchone()
    return int(row["events"]) if row else 0


async def list_events(event_type: str) -> list[dict[str, Any]]:
    """Return every event of one type in order."""
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM evaluation_migration_events "
            "WHERE event_type = ? ORDER BY created_at, id",
            (event_type,),
        )
    return [
        {**dict(row), "payload": json.loads(row["payload"])}
        for row in rows
    ]


async def _write_state(
    *,
    phase: str | None = None,
    legacy_readonly: bool | None = None,
    cursors: dict[str, Any] | None = None,
    gates: dict[str, Any] | None = None,
) -> None:
    state = await get_state()
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE evaluation_migration_state SET phase = ?, "
            "legacy_readonly = ?, cursors = ?, gates = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = 'authority'",
            (
                phase if phase is not None else state["phase"],
                int(
                    legacy_readonly
                    if legacy_readonly is not None
                    else state["legacy_readonly"],
                ),
                json.dumps(
                    cursors if cursors is not None else state["cursors"],
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    gates if gates is not None else state["gates"],
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
        await connection.commit()


async def advance_phase(target: str) -> dict[str, Any]:
    """Move the migration one declared phase forward."""
    state = await get_state()
    current = str(state["phase"])
    if _FORWARD.get(current) != target:
        raise MigrationPhaseError(
            f"The migration cannot move from {current} to {target}"
        )
    if target == "cutover":
        await assert_cutover_allowed()
    if target == "contract":
        await assert_contract_allowed()
    await _write_state(phase=target)
    await record_event(
        "phase_change", {"from": current, "to": target},
    )
    return await get_state()


async def rollback_phase() -> dict[str, Any]:
    """Roll the migration back under the declared rules.

    Before cutover the rollback steps toward the legacy writer and
    preserves every legacy record. After cutover the current
    generation stays the data authority, and the compatible legacy
    projections turn read-only for an older application.
    """
    state = await get_state()
    current = str(state["phase"])
    if current in _ROLLBACK:
        target = _ROLLBACK[current]
        await _write_state(phase=target)
        await record_event(
            "phase_change",
            {"from": current, "to": target, "rollback": True},
        )
        return await get_state()
    if current == "cutover":
        await _write_state(legacy_readonly=True)
        await record_event(
            "phase_change",
            {
                "from": current,
                "to": current,
                "rollback": True,
                "legacy_readonly": True,
            },
        )
        return await get_state()
    raise MigrationPhaseError(
        f"The {current} phase has no declared rollback"
    )


async def record_fallback(kind: str, record_id: str) -> None:
    """Record one dual-read fallback to the complete legacy record."""
    await record_event(
        "fallback", {"kind": kind, "record_id": record_id},
    )


async def record_digest_mismatch(
    target: str, source_id: str, expected: str, actual: str,
) -> None:
    """Record one backfill digest mismatch with its exact source row."""
    await record_event(
        "digest_mismatch",
        {
            "target": target,
            "source_id": source_id,
            "expected_checksum": expected,
            "actual_checksum": actual,
        },
    )


async def record_export(
    run_id: str, export_digest: str, *, verified: bool,
) -> None:
    """Record one completed compatibility export and its verification."""
    await record_event(
        "export",
        {"run_id": run_id, "export_digest": export_digest,
         "verified": verified},
    )


async def record_gate(
    name: str, passed: bool, *, actor: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Record one deletion-gate decision with its actor and evidence."""
    if name not in DELETION_GATES:
        raise MigrationPhaseError(f"Unknown deletion gate: {name}")
    state = await get_state()
    gates = dict(state["gates"])
    gates[name] = {"passed": bool(passed), "actor": actor,
                   "evidence": dict(evidence or {})}
    await _write_state(gates=gates)


# The declared fallback threshold: the number of legacy fallbacks and
# direct legacy calls one removal window tolerates before a gate can
# pass. The threshold is a declared value, never an inferred one.
DECLARED_FALLBACK_THRESHOLD = 0
# The gates whose passage needs measured evidence, not only a decision.
MEASURED_GATES = {
    "fallback_window_clean": ("fallback_events", "threshold", "window_start"),
    "downgrade_fixtures_passed": ("legacy_records", "current_records",
                                  "archived_records"),
    "compatibility_export_verified": ("verified_exports",),
}


async def measure_fallback_window(
    *, window_start: str, threshold: int = DECLARED_FALLBACK_THRESHOLD,
) -> dict[str, Any]:
    """Measure legacy fallback use inside one removal window.

    The measurement counts dual-read fallbacks and direct legacy calls
    since the window start and compares the sum with the declared
    threshold. A gate passes only through this measurement.
    """
    fallbacks = await count_events("fallback", since=window_start)
    direct_calls = await count_events("direct_legacy_call", since=window_start)
    total = fallbacks + direct_calls
    return {
        "window_start": window_start,
        "fallback_events": fallbacks,
        "direct_legacy_call_events": direct_calls,
        "total_legacy_use": total,
        "threshold": int(threshold),
        "passed": total <= int(threshold),
    }


async def record_measured_fallback_gate(
    *, window_start: str, actor: str,
    threshold: int = DECLARED_FALLBACK_THRESHOLD,
) -> dict[str, Any]:
    """Measure the window and record the fallback gate with evidence."""
    measurement = await measure_fallback_window(
        window_start=window_start, threshold=threshold,
    )
    await record_gate(
        "fallback_window_clean", measurement["passed"], actor=actor,
        evidence=measurement,
    )
    return measurement


async def removal_gate_evidence() -> dict[str, Any]:
    """Return the measured fallback, rollback, and retention evidence."""
    state = await get_state()
    gates = state["gates"]
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS archived FROM evaluation_readonly_archive",
        )
        row = await cursor.fetchone()
    archived = int(row["archived"]) if row else 0
    exports = await list_events("export")
    verified_exports = sum(
        1 for event in exports if event["payload"].get("verified")
    )
    # A downgrade archives the live events; the archived export events
    # still count as retention evidence.
    async with db._connect() as connection:  # noqa: SLF001
        archived_rows = await connection.execute_fetchall(
            "SELECT record FROM evaluation_readonly_archive "
            "WHERE source_table = 'evaluation_migration_events'",
        )
    for archived_row in archived_rows:
        archived_event = json.loads(archived_row["record"])
        if archived_event.get("event_type") != "export":
            continue
        payload = json.loads(archived_event.get("payload") or "{}")
        if payload.get("verified"):
            verified_exports += 1
    fallback_gate = gates.get("fallback_window_clean") or {}
    rollback_gate = gates.get("downgrade_fixtures_passed") or {}
    return {
        "fallback": {
            "measured": bool(fallback_gate.get("evidence")),
            "evidence": fallback_gate.get("evidence") or {},
            "passed": bool(fallback_gate.get("passed")),
        },
        "rollback": {
            "populated": bool(rollback_gate.get("evidence")),
            "evidence": rollback_gate.get("evidence") or {},
            "passed": bool(rollback_gate.get("passed")),
        },
        "retention": {
            "archived_records": archived,
            "verified_exports": verified_exports,
            "passed": verified_exports > 0,
        },
    }


def _missing_evidence(gates: dict[str, Any]) -> list[str]:
    missing = []
    for name, required in MEASURED_GATES.items():
        evidence = (gates.get(name) or {}).get("evidence") or {}
        for field in required:
            if field not in evidence:
                missing.append(f"{name}:{field}")
    return missing


async def set_cursor(target: str, cursor: str) -> None:
    """Persist one idempotent backfill cursor."""
    if target not in BACKFILL_TARGETS:
        raise MigrationPhaseError(f"Unknown backfill target: {target}")
    state = await get_state()
    cursors = dict(state["cursors"])
    cursors[target] = cursor
    await _write_state(cursors=cursors)


async def assert_cutover_allowed() -> None:
    """Refuse cutover on incomplete backfill or any digest mismatch."""
    mismatches = await list_events("digest_mismatch")
    if mismatches:
        first = mismatches[0]["payload"]
        raise CutoverRefusedError(
            "Cutover stops on a recorded backfill digest mismatch at "
            f"{first['target']} row {first['source_id']}"
        )
    state = await get_state()
    missing = [
        target
        for target in BACKFILL_TARGETS
        if target not in state["cursors"]
    ]
    if missing:
        raise CutoverRefusedError(
            "Cutover needs one completed backfill cursor per target; "
            f"missing {sorted(missing)}"
        )


async def assert_contract_allowed() -> None:
    """Refuse the destructive contract before every deletion gate."""
    state = await get_state()
    gates = state["gates"]
    failing = [
        name
        for name in DELETION_GATES
        if not (gates.get(name) or {}).get("passed")
    ]
    if failing:
        raise ContractRefusedError(
            "A destructive migration refuses before every deletion gate "
            f"passes; unpassed gates: {sorted(failing)}"
        )
    missing = _missing_evidence(gates)
    if missing:
        raise ContractRefusedError(
            "A destructive migration refuses without measured gate "
            f"evidence; missing: {sorted(missing)}"
        )
    if await list_events("digest_mismatch"):
        raise ContractRefusedError(
            "A destructive migration refuses while a digest mismatch "
            "stays recorded"
        )


async def authority_snapshot() -> dict[str, Any]:
    """Return the migration authority view for operators."""
    state = await get_state()
    return {
        "phase": state["phase"],
        "legacy_readonly": state["legacy_readonly"],
        "cursors": state["cursors"],
        "gates": {
            name: state["gates"].get(name, {"passed": False})
            for name in DELETION_GATES
        },
        "fallback_events": await count_events("fallback"),
        "digest_mismatch_events": await count_events("digest_mismatch"),
        "direct_legacy_call_events": await count_events(
            "direct_legacy_call",
        ),
    }


# ── Idempotent backfill through the one canonical writer ─────────────


async def run_backfill() -> dict[str, Any]:
    """Copy every compatible legacy record into the current storage.

    Every write passes through the one canonical writer. A record that
    already exists verifies by digest instead of copying again, a
    digest mismatch records the exact source row and later stops
    cutover, and each completed target persists one idempotent
    cursor. Running the backfill twice changes nothing.
    """
    from benchmarks import evaluation_records, repository
    from benchmarks.evaluation_contracts import validate_record
    from benchmarks.legacy_adapters import (
        attempt_evidence_from_attempt,
        run_plan_from_run,
        scorer_spec_from_scorer,
    )

    summary = {
        target: {"copied": 0, "verified": 0, "mismatched": 0}
        for target in BACKFILL_TARGETS
    }

    async def copy_or_verify(
        target: str,
        kind: str,
        record_id: str,
        record: dict[str, Any],
        links: dict[str, Any] | None = None,
        publish_kind: str | None = None,
    ) -> None:
        existing = await evaluation_records.get_record(kind, record_id)
        expected = validate_record(record)["record_checksum"]
        if existing is not None:
            if str(existing["record_checksum"]) != expected:
                summary[target]["mismatched"] += 1
                await record_digest_mismatch(
                    target,
                    record_id,
                    expected,
                    str(existing["record_checksum"]),
                )
            else:
                summary[target]["verified"] += 1
            return
        await evaluation_records.save_record(
            record, record_id=record_id, links=links,
        )
        if publish_kind:
            await evaluation_records.publish_record(publish_kind, record_id)
        summary[target]["copied"] += 1

    last_id = ""
    for scorer in sorted(
        await repository.list_scorers(), key=lambda row: str(row["id"]),
    ):
        record = scorer_spec_from_scorer(scorer)
        last_id = f"{scorer['id']}:{record['version']}"
        await copy_or_verify(
            "scorer_specs",
            "scorer-spec",
            last_id,
            record,
            links={"legacy_scorer_id": str(scorer["id"])},
            publish_kind="scorer-spec",
        )
    await set_cursor("scorer_specs", last_id or "complete")

    async with db._connect() as connection:  # noqa: SLF001
        run_rows = await connection.execute_fetchall(
            "SELECT id FROM benchmark_runs ORDER BY id",
        )
    last_id = ""
    for row in run_rows:
        run = await repository.get_run(str(row["id"]))
        if run is None:
            continue
        record = run_plan_from_run(run)
        last_id = str(record["plan_id"])
        await copy_or_verify(
            "run_plans",
            "run-plan",
            last_id,
            record,
            links={
                "test_revision_id": str(run["test_revision_id"]),
                "run_id": str(run["id"]),
            },
            publish_kind="run-plan",
        )
    await set_cursor("run_plans", last_id or "complete")

    from benchmarks.facade import _legacy_attempt_row

    async with db._connect() as connection:  # noqa: SLF001
        attempt_rows = await connection.execute_fetchall(
            "SELECT id FROM benchmark_attempts "
            "WHERE status IN ('completed','failed','cancelled') "
            "ORDER BY id",
        )
    last_id = ""
    for row in attempt_rows:
        source = await _legacy_attempt_row(str(row["id"]))
        if source is None:
            continue
        record = attempt_evidence_from_attempt(
            source,
            run_id=str(source["run_id"]),
            plan_checksum=str(source["execution_plan_checksum"]),
        )
        last_id = str(row["id"])
        await copy_or_verify(
            "attempt_evidence",
            "attempt-evidence",
            last_id,
            record,
            links={"attempt_id": last_id},
        )
    await set_cursor("attempt_evidence", last_id or "complete")

    async with db._connect() as connection:  # noqa: SLF001
        gate_rows = await connection.execute_fetchall(
            "SELECT id, display_exceptions "
            "FROM benchmark_gate_evaluations ORDER BY id",
        )
        existing_rows = await connection.execute_fetchall(
            "SELECT id FROM gate_display_exceptions",
        )
    existing_ids = {str(row["id"]) for row in existing_rows}
    last_id = ""
    for row in gate_rows:
        exceptions = json.loads(row["display_exceptions"] or "[]")
        for index, exception in enumerate(exceptions):
            exception_id = f"{row['id']}:{index}"
            last_id = exception_id
            if exception_id in existing_ids:
                summary["display_exceptions"]["verified"] += 1
                continue
            await evaluation_records.save_gate_display_exception(
                str(row["id"]), exception, exception_id=exception_id,
            )
            summary["display_exceptions"]["copied"] += 1
    await set_cursor("display_exceptions", last_id or "complete")

    await record_event("backfill_run", {"summary": summary})
    return summary


async def compatibility_export(run_id: str) -> dict[str, Any]:
    """Build and verify one complete compatibility export.

    The export bundles the legacy run representation with the frozen
    report from the current analysis engine, digests the canonical
    bytes, verifies the digest by re-reading its own content, and
    records the completed export for the deletion gates.
    """
    from benchmarks import repository
    from benchmarks.analysis import build_run_report
    from benchmarks.provenance import canonical_json, content_checksum

    run = await repository.get_run(run_id)
    if run is None:
        raise MigrationPhaseError(f"The run {run_id} does not exist")
    report = build_run_report(run)
    bundle = {
        "schema_id": "compatibility-export",
        "schema_version": 2,
        "run": {
            "id": run["id"],
            "status": run["status"],
            "test_revision_id": run["test_revision_id"],
            "execution_plan_checksum": run["execution_plan_checksum"],
        },
        "report": report,
    }
    export_digest = content_checksum(bundle)
    verified = (
        content_checksum(json.loads(canonical_json(bundle)))
        == export_digest
    )
    await record_export(run_id, export_digest, verified=verified)
    return {
        "bundle": bundle,
        "export_digest": export_digest,
        "verified": verified,
    }
