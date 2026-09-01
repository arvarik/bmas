"""Persist immutable benchmark baselines, gates, and qualifications."""

from __future__ import annotations

import json
import uuid
from typing import Any

import database as db
from benchmarks import repository
from benchmarks.gates import (
    TERMINAL_RUN_STATUSES,
    GateCompatibilityError,
    GateSettlementError,
    GateTerminalityError,
    evaluate_gate,
    validate_display_exceptions,
    validate_rules,
    validate_treatment_declaration,
)
from benchmarks.provenance import content_checksum


async def create_baseline(
    *,
    baseline_id: str,
    run_id: str,
    name: str,
    description: str,
    rules: list[dict[str, Any]],
    created_by: str,
    treatment_declaration: list[str] | None = None,
) -> dict[str, Any]:
    """Pin one completed run, one immutable rule set, and its treatments.

    The treatment declaration lists every experimental axis a
    candidate may change. An empty declaration allows no treatment
    difference, which preserves the previous same-revision behavior.
    """
    validate_rules(rules)
    declaration = sorted(treatment_declaration or [])
    validate_treatment_declaration(declaration)
    run = await repository.get_run(run_id)
    if run is None:
        raise repository.BenchmarkNotFound("The baseline run does not exist")
    if run["status"] != "completed":
        raise repository.BenchmarkConflict("Only a completed run can become a baseline")
    if str(run.get("analysis_status") or "valid") == "blocked":
        raise repository.BenchmarkConflict(
            "A run with a failed required scorer has no valid analysis "
            "and cannot become a baseline"
        )
    rules_checksum = content_checksum(rules)
    try:
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                "INSERT INTO benchmark_baselines "
                "(id, test_id, run_id, name, description, rules, rules_checksum, "
                "created_by, treatment_declaration) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    baseline_id,
                    run["test_id"],
                    run_id,
                    name,
                    description,
                    json.dumps(rules, separators=(",", ":"), sort_keys=True),
                    rules_checksum,
                    created_by,
                    json.dumps(declaration, separators=(",", ":")),
                ),
            )
            await connection.commit()
    except Exception as error:
        if "UNIQUE constraint failed" in str(error):
            raise repository.BenchmarkConflict(
                "This run or baseline name is already pinned"
            ) from error
        raise
    baseline = await get_baseline(baseline_id)
    if baseline is None:
        raise RuntimeError("The baseline disappeared after creation")
    return baseline


def _decode(row: Any, *columns: str) -> dict[str, Any]:
    result = dict(row)
    for column in columns:
        value = result.get(column)
        result[column] = json.loads(value) if isinstance(value, str) else value
    return result


async def list_baselines(test_id: str | None = None) -> list[dict[str, Any]]:
    """Return immutable baselines with their latest gate status."""
    clause = "WHERE baseline.test_id = ?" if test_id else ""
    parameters = (test_id,) if test_id else ()
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT baseline.*, test.name AS test_name, run.status AS run_status, "
            "(SELECT evaluation.status FROM benchmark_gate_evaluations AS evaluation "
            "WHERE evaluation.baseline_id = baseline.id "
            "ORDER BY evaluation.created_at DESC LIMIT 1) AS latest_gate_status, "
            "(SELECT COUNT(*) FROM benchmark_gate_evaluations AS evaluation "
            "WHERE evaluation.baseline_id = baseline.id) AS evaluation_count "
            "FROM benchmark_baselines AS baseline "
            "JOIN benchmark_tests AS test ON test.id = baseline.test_id "
            "JOIN benchmark_runs AS run ON run.id = baseline.run_id "
            f"{clause} ORDER BY baseline.created_at DESC",
            parameters,
        )
    return [_decode(row, "rules", "treatment_declaration") for row in rows]


async def get_baseline(baseline_id: str) -> dict[str, Any] | None:
    """Return one baseline and all saved gate evaluations."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT baseline.*, test.name AS test_name, run.status AS run_status "
            "FROM benchmark_baselines AS baseline "
            "JOIN benchmark_tests AS test ON test.id = baseline.test_id "
            "JOIN benchmark_runs AS run ON run.id = baseline.run_id "
            "WHERE baseline.id = ?",
            (baseline_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        evaluations = await connection.execute_fetchall(
            "SELECT * FROM benchmark_gate_evaluations WHERE baseline_id = ? "
            "ORDER BY created_at DESC",
            (baseline_id,),
        )
    result = _decode(row, "rules", "treatment_declaration")
    result["evaluations"] = [
        _decode(item, "report", "display_exceptions")
        for item in evaluations
    ]
    return result


async def _load_gate_pair(
    baseline_id: str, candidate_run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    baseline = await get_baseline(baseline_id)
    if baseline is None:
        raise repository.BenchmarkNotFound("The benchmark baseline does not exist")
    candidate = await repository.get_run(candidate_run_id)
    if candidate is None:
        raise repository.BenchmarkNotFound("The candidate run does not exist")
    if candidate["test_id"] != baseline["test_id"]:
        raise repository.BenchmarkConflict(
            "The candidate and baseline must belong to the same test"
        )
    baseline_run = await repository.get_run(str(baseline["run_id"]))
    if baseline_run is None:
        raise repository.BenchmarkNotFound("The pinned baseline run does not exist")
    return baseline, baseline_run, candidate


async def preview_baseline(
    baseline_id: str,
    candidate_run_id: str,
    *,
    display_exceptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate one candidate without saving anything.

    A preview inspects an active run early. It never persists, so it
    can never block or preempt the later final decision.
    """
    baseline, baseline_run, candidate = await _load_gate_pair(
        baseline_id, candidate_run_id,
    )
    try:
        return evaluate_gate(
            baseline_run,
            candidate,
            list(baseline["rules"]),
            mode="preview",
            treatment_declaration=list(
                baseline.get("treatment_declaration") or [],
            ),
            display_exceptions=display_exceptions or [],
            now=await db.database_utc_now(),
        )
    except (GateCompatibilityError, ValueError) as error:
        raise repository.BenchmarkConflict(str(error)) from error


async def evaluate_baseline(
    baseline_id: str,
    candidate_run_id: str,
    *,
    display_exceptions: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Evaluate and save one terminal candidate exactly once.

    The final gate stores only terminal decisions: a nonterminal
    candidate or a blocked analysis rejects, and an incompatible
    revision fails before any rule evaluates.
    """
    baseline, baseline_run, candidate = await _load_gate_pair(
        baseline_id, candidate_run_id,
    )
    if str(candidate.get("status")) not in TERMINAL_RUN_STATUSES:
        raise repository.BenchmarkConflict(
            "A final gate accepts only a terminal candidate; preview an "
            "active run instead"
        )
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM benchmark_gate_evaluations "
            "WHERE baseline_id = ? AND candidate_run_id = ? "
            "ORDER BY evaluation_version",
            (baseline_id, candidate_run_id),
        )
    current = [row for row in rows if row["superseded_at"] is None]
    if current:
        # The current decision is terminal; only a superseding event,
        # such as a late charge, opens the next evaluation version.
        return _decode(current[-1], "report", "display_exceptions"), False
    next_version = (
        max((int(row["evaluation_version"]) for row in rows), default=0) + 1
    )
    exceptions = display_exceptions or []
    cost_evidence = await _cost_evidence(candidate)
    try:
        validate_display_exceptions(exceptions, primary_metric=None)
        report = evaluate_gate(
            baseline_run,
            candidate,
            list(baseline["rules"]),
            mode="final",
            treatment_declaration=list(
                baseline.get("treatment_declaration") or [],
            ),
            display_exceptions=exceptions,
            cost_evidence=cost_evidence,
            now=await db.database_utc_now(),
        )
    except (
        GateCompatibilityError,
        GateSettlementError,
        GateTerminalityError,
        ValueError,
    ) as error:
        raise repository.BenchmarkConflict(str(error)) from error
    evaluation_id = f"gate-{uuid.uuid4().hex}"
    try:
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                "INSERT INTO benchmark_gate_evaluations "
                "(id, baseline_id, candidate_run_id, status, report, "
                "report_checksum, invariant_digest, display_exceptions, "
                "evaluation_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evaluation_id,
                    baseline_id,
                    candidate_run_id,
                    report["status"],
                    json.dumps(report, separators=(",", ":"), sort_keys=True),
                    report["report_checksum"],
                    report["candidate_invariant_digest"],
                    json.dumps(exceptions, separators=(",", ":"),
                               sort_keys=True),
                    next_version,
                ),
            )
            await connection.commit()
    except Exception as error:
        if "UNIQUE constraint failed" not in str(error):
            raise
        async with db._connect() as connection:  # noqa: SLF001
            cursor = await connection.execute(
                "SELECT * FROM benchmark_gate_evaluations "
                "WHERE baseline_id = ? AND candidate_run_id = ? "
                "AND superseded_at IS NULL "
                "ORDER BY evaluation_version DESC LIMIT 1",
                (baseline_id, candidate_run_id),
            )
            existing = await cursor.fetchone()
        if existing:
            return _decode(existing, "report", "display_exceptions"), False
        raise
    return {
        "id": evaluation_id,
        "baseline_id": baseline_id,
        "candidate_run_id": candidate_run_id,
        "status": report["status"],
        "report": report,
        "report_checksum": report["report_checksum"],
        "invariant_digest": report["candidate_invariant_digest"],
        "display_exceptions": exceptions,
        "evaluation_version": next_version,
    }, True


async def _cost_evidence(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Collect the candidate run cost state for cost-sensitive gates."""
    from benchmarks import costs

    cost_status = candidate.get("cost_status")
    if cost_status is None:
        return None
    charges = await repository.list_cost_charges(str(candidate["id"]))
    summary = costs.summarize_charges(charges)
    return {
        "cost_status": str(cost_status),
        "unbounded_unknown": bool(summary["unbounded_unknown"]),
    }


async def save_qualification(report: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Save one immutable runtime qualification result."""
    evidence_key = str(
        report.get("run_id") or f"static:{report['report_checksum']}"
    )
    qualification_id = f"qualification-{uuid.uuid4().hex}"
    try:
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute(
                "INSERT INTO benchmark_runtime_qualifications "
                "(id, runtime_id, contract_version, evidence_key, run_id, status, report, report_checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    qualification_id,
                    report["runtime_id"],
                    report["contract_version"],
                    evidence_key,
                    report.get("run_id"),
                    report["status"],
                    json.dumps(report, separators=(",", ":"), sort_keys=True),
                    report["report_checksum"],
                ),
            )
            await connection.commit()
    except Exception as error:
        if "UNIQUE constraint failed" not in str(error):
            raise
        async with db._connect() as connection:  # noqa: SLF001
            cursor = await connection.execute(
                "SELECT * FROM benchmark_runtime_qualifications "
                "WHERE runtime_id = ? AND contract_version = ? AND evidence_key = ?",
                (report["runtime_id"], report["contract_version"], evidence_key),
            )
            existing = await cursor.fetchone()
        if existing:
            return _decode(existing, "report"), False
        raise
    return {
        "id": qualification_id,
        "runtime_id": report["runtime_id"],
        "contract_version": report["contract_version"],
        "run_id": report.get("run_id"),
        "status": report["status"],
        "report": report,
        "report_checksum": report["report_checksum"],
    }, True


async def list_qualifications() -> list[dict[str, Any]]:
    """Return every saved runtime qualification result."""
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM benchmark_runtime_qualifications ORDER BY created_at DESC"
        )
    return [_decode(row, "report") for row in rows]
