#!/usr/bin/env python3
"""Evaluate the release gates from one complete manifest result.

The script maps every documented release gate to the manifest groups
that prove it, reads one ``test-manifest-result.json`` as read-only
data, reports each gate as passed, failed, or unproven, and writes the
gate report. With ``--promote`` it writes the release evidence that
makes evaluation the default generation, and it refuses when any gate
is not passed. The result file stays the only source of pass state;
this script never runs a test.

Run from the repository root:

    python3 scripts/release-gates.py --result test-results/<run>/test-manifest-result.json
    python3 scripts/release-gates.py --result ... --promote
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "conformance" / "release" / "evaluation-default.json"

# Every release gate names the manifest groups that prove it.
RELEASE_GATES: dict[str, list[str]] = {
    # ── Foundation Stage 0 conditions ─────────────────────────────
    "foundation_manifest_parity": ["manifest.validate", "daemon.foundation-release-gate"],
    "foundation_runtime_routing": ["daemon.runtime-routing"],
    "foundation_unit_of_work_atomicity": ["daemon.unit-of-work-atomicity", "daemon.run-admission"],
    "foundation_journal_replay": ["daemon.journal-replay", "daemon.typed-indexes"],
    "foundation_crash_recovery": ["daemon.activation-states", "daemon.agent-protocol", "daemon.external-effects", "daemon.execution-envelope"],
    "foundation_budget_reconciliation": ["daemon.budget-states"],
    "foundation_privacy_boundary": ["daemon.privacy-boundary"],
    "foundation_evidence_and_goals": ["daemon.evidence-authority", "daemon.goal-concurrency"],
    "foundation_shared_conformance": ["daemon.cross-runtime-conformance", "daemon.behavioral-conformance", "daemon.foundation-release-gate"],
    "foundation_populated_migration": ["daemon.populated-migration"],
    "foundation_complete_stack_journey": ["daemon.complete-stack-journey"],
    "foundation_golden_fixtures": ["daemon.foundation-release-gate"],
    "foundation_journal_immutability": ["daemon.journal-replay", "daemon.unit-of-work-atomicity", "daemon.populated-migration"],
    "foundation_backup_and_restore": ["daemon.sqlite-operational"],
    "foundation_monetary_arithmetic": ["daemon.budget-arithmetic"],
    "foundation_digest_access_and_keys": ["daemon.digest-profile", "daemon.keyed-digest", "daemon.security-matrix"],
    "foundation_recovery_center": ["daemon.recovery-center"],
    # ── Evaluation conditions ─────────────────────────────────────
    "phase_zero_regression": ["daemon.tests", "daemon.benchmark-data-contracts"],
    "statistical_oracle": ["daemon.benchmark-statistical-oracle"],
    "unmocked_browser_journey": ["mission-control.full-stack-journey"],
    "scheduler_crash_history": ["daemon.benchmark-admission-recovery", "daemon.run-admission-recovery"],
    "import_security": ["daemon.source-adapters"],
    "legacy_adapters": ["daemon.evaluation-authority"],
    "replay_bundle_checksum": ["daemon.evaluation-studies-replay"],
    "complete_repository_check": ["manifest.validate"],
    "populated_upgrade_downgrade": ["daemon.populated-migration", "daemon.evaluation-contracts"],
    "facade_only_writer": ["repo.legacy-write-guard", "daemon.evaluation-consolidation"],
    "public_content_capabilities": ["daemon.source-adapters"],
    "scorer_sandbox": ["daemon.evaluation-scoring"],
    "portable_transforms": ["daemon.evaluation-editing"],
    "manifest_includes_full_stack": ["manifest.validate", "mission-control.full-stack-journey"],
    "reports_record_estimand_and_replay": ["daemon.evaluation-frozen-analysis"],
    "runner_and_reference_scorer": ["manifest.validate", "conformance.reference-scorer"],
    "retries_no_extra_slots": ["daemon.benchmark-statistical-oracle"],
    "weighted_bootstrap_oracle": ["daemon.evaluation-frozen-analysis"],
    "comparability_rejects_exceptions": ["daemon.benchmark-data-contracts"],
    "snapshots_pin_toolchains": ["daemon.evaluation-frozen-analysis", "repo.toolchain-pins", "mission-control.toolchain-pins"],
    "wasi_and_microvm": ["daemon.evaluation-scoring"],
    "contamination_rights_assets": ["daemon.evaluation-editing"],
    "interaction_canaries": ["daemon.evaluation-studies-replay"],
    "dispatch_fairness_restart": ["daemon.benchmark-schedule-fairness"],
    "cost_settlement_supersession": ["daemon.benchmark-resource-ledger", "daemon.evaluation-calibration-ledger"],
    "bundle_import_executes_nothing": ["daemon.evaluation-studies-replay"],
    "performance_limits": ["daemon.performance-contract"],
    "vectorized_engine_equivalence": ["daemon.evaluation-frozen-analysis", "daemon.performance-contract-smoke"],
    "frozen_gates_and_reports": ["daemon.evaluation-frozen-analysis"],
    "second_implementation_fixtures": ["daemon.evaluation-editing", "daemon.evaluation-frozen-analysis", "mission-control.conformance-fixtures"],
    "data_class_redaction": ["daemon.hardening-faults"],
    "automatic_ledger_emission": ["daemon.benchmark-resource-ledger"],
    "model_backed_judge_schedule": ["daemon.evaluation-scoring"],
    "dataset_version_records": ["daemon.evaluation-editing"],
    "study_admission": ["daemon.evaluation-studies-replay"],
    "legacy_removal": ["repo.legacy-write-guard", "daemon.evaluation-consolidation"],
    "metric_lifecycle_calibration": ["daemon.evaluation-studies-replay"],
    "source_naming": ["repo.source-naming"],
}


def _digest(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()


def evaluate(result: dict) -> dict:
    states = {group["group_id"]: group["state"] for group in result["groups"]}
    gates = {}
    for name, groups in RELEASE_GATES.items():
        group_states = {group: states.get(group, "absent") for group in groups}
        if all(state == "passed" for state in group_states.values()):
            status = "passed"
        elif any(state in ("failed", "timed_out", "infrastructure_error",
                           "cancelled") for state in group_states.values()):
            status = "failed"
        else:
            status = "unproven"
        gates[name] = {"status": status, "groups": group_states}
    return gates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True)
    parser.add_argument("--report", default=None)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()
    result_path = Path(args.result)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    gates = evaluate(result)
    report = {
        "schema_id": "bmas.release_gates",
        "manifest_run_id": result["run_id"],
        "profile_id": result.get("profile_id"),
        "result_digest": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "gates": gates,
        "all_passed": all(g["status"] == "passed" for g in gates.values()),
    }
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    for name, gate in sorted(gates.items()):
        print(f"  {gate['status']:9s} {name}")
    if not report["all_passed"]:
        blocking = sorted(n for n, g in gates.items() if g["status"] != "passed")
        print(f"FAIL: {len(blocking)} release gates are not passed: {', '.join(blocking)}")
        return 1
    print("PASS: every release gate passed in the recorded complete run.")
    if args.promote:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            check=False, cwd=ROOT,
        ).stdout.strip()
        body = {
            "schema_id": "bmas.evaluation_default",
            "commit": commit,
            "manifest_run_id": result["run_id"],
            "result_digest": report["result_digest"],
            "gates": {name: {"status": gate["status"]} for name, gate in gates.items()},
        }
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps({**body, "digest": _digest(body)},
                                       indent=2, sort_keys=True) + "\n")
        print(f"PROMOTED: evaluation is the default generation ({EVIDENCE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
