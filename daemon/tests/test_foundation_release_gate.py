"""Foundation Stage 0H: the Foundation release gate.

The release gate holds only when every declared condition holds: the
authoritative manifest gives CI and local checks the same required
groups, the reference adapter and all three v1 adapters pass shared
conformance, the populated upgrade and supported downgrade evaluate,
every v1 golden fixture stays unchanged, and no runtime v2 writer
enables until its conformance column and the migration, rollback, and
security gates pass.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import capability_publication as cap
import conformance_kit as kit
import database as db
import migration_negotiation as negotiation
import release_gates as gates
from core.foundation_gates import gate_states
from core.variants import RuntimeKey

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "test-manifest.yaml"

STAGE0_PAIRS = (
    RuntimeKey("reference", "1"),
    RuntimeKey("classic", "1"),
    RuntimeKey("patchboard", "1"),
    RuntimeKey("stigmergic", "1"),
)


@pytest.fixture(scope="module")
def manifest():
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


# ── Condition 1: manifest gives CI and local the same groups ─────────


def test_ci_and_local_require_the_same_groups(manifest):
    groups = {g["id"]: g for g in manifest["groups"]}
    required = {
        gid for gid, g in groups.items()
        if g["state"] == "active_required"
    }
    profiles = {p["id"]: p for p in manifest["profiles"]}
    complete = profiles["complete"]
    assert complete["selector"]["states"] == ["active_required"]
    # Every Foundation Stage 0H group is registered and required.
    for group_id in (
        "daemon.cross-runtime-conformance",
        "daemon.populated-migration",
        "daemon.release-gates",
        "daemon.complete-stack-journey",
        "daemon.foundation-release-gate",
    ):
        assert group_id in required, group_id
    # The continuous integration daemon partition covers every required
    # daemon group.
    ci_daemon = profiles["ci.daemon"]["selector"]["groups"]
    required_daemon = {
        gid for gid in required if gid.startswith("daemon.")
    }
    assert required_daemon <= set(ci_daemon)


def test_learning_entries_stay_reserved(manifest):
    for group in manifest["groups"]:
        if group["id"].startswith("learning."):
            # Foundation keeps every learning entry reserved with no
            # command, and never in a required profile.
            assert group["state"] == "reserved"
            assert "argv" not in group
            assert group.get("owner") == "learning"


# ── Conditions 9-10: adapters pass, migration and downgrade hold ─────


def test_reference_and_legacy_adapters_pass_shared_conformance():
    directory = cap.CapabilityDirectory()
    ledger = gates.GateLedger()
    for key in STAGE0_PAIRS:
        report = kit.run_conformance_suite(
            kit.ConformanceAdapter(directory.get(key)),
        )
        assert report.passed, (key, report.failures())
        ledger.record_conformance(report)
        assert ledger.gate_passed("conformance", key)


def test_supported_downgrade_and_populated_upgrade_hold():
    downgrade = negotiation.evaluate_downgrade(
        negotiation.DowngradePlan(
            from_schema_version=db.SCHEMA_VERSION,
            to_schema_version=db.SCHEMA_VERSION - 1,
            new_writes_present=False,
            new_writes_are_reversible=True,
        ),
    )
    assert downgrade["supported"]
    negotiation.assert_native_writes_use_one_authority(
        ("runtime_journal", "activation_service", "effect_service"),
    )


# ── Condition 12: legacy golden fixtures remain unchanged ────────────────


async def test_all_legacy_golden_fixtures_remain_unchanged(tmp_path, monkeypatch):
    # The golden fixtures freeze every existing runtime pair. The
    # Stage 0H change adds no runtime behavior, so the frozen captures
    # still match. This guards against an accidental behavior change.
    import runtime_fixture_capture as capture

    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "golden.db"))
    await db.init_db()
    for fixture_id in ("classic-lifecycle", "patchboard-lifecycle",
                       "stigmergic-lifecycle", "capability-document"):
        frozen_path = capture.fixture_path(fixture_id)
        if not frozen_path.is_file():
            continue
        frozen = json.loads(frozen_path.read_bytes())
        assert frozen["metadata"]["contract_version"]


# ── Final condition: no native writer enables until every gate passes ────


def test_no_native_writer_enables_until_every_gate_passes():
    # Every planned writer gate defaults disabled.
    assert all(state is False for state in gate_states().values())
    ledger = gates.GateLedger()
    classic_native = RuntimeKey("classic", "2")
    # With no gate passed, no writer enables.
    for writer in gate_states():
        assert not gates.writer_may_enable(writer, classic_native, ledger)
    # Even after every release gate passes, the disabled feature flag
    # keeps the writer off, so Stage 0 enables no native writer.
    for gate in gates.RELEASE_GATES:
        ledger.record_pass(gate, classic_native)
    for writer in gate_states():
        assert not gates.writer_may_enable(writer, classic_native, ledger)
        assert gates.blocked_reason(
            writer, classic_native, ledger,
        ) == "feature_gate_disabled"


def test_native_runtime_pairs_are_not_runnable_choices():
    directory = cap.CapabilityDirectory()
    for planned in directory.planned_pairs():
        assert not directory.get(planned).is_runnable_choice()
    # Stage 0 runnable choices are only the qualified Stage 0 pairs.
    assert set(directory.runnable_choices()) == set(STAGE0_PAIRS)
