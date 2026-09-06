"""An interactive task enters one Foundation run through the full admission writer.

With the writer gates on, the admission creates the run, the immutable
admission with the exact pair and the recorded seed, the run budget
with its reserved reservation, the journal genesis, and the run-control
row with the task fence. The call is idempotent. With the gates off the
task stays on the legacy path and no run exists. A prerequisite the
writer rejects fails the task closed in the orchestrator.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import config
import database as db
import interactive_admission as admission
import run_admission
import runtime_journal as journal
from core import foundation_gates
from core.variants import RuntimeKey, VariantConfigurationError

CLASSIC = RuntimeKey("classic", "1")
TASK_ID = "task-interactive"


@pytest_asyncio.fixture
async def admission_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "interactive.db"))
    monkeypatch.setattr(config, "FOUNDATION_GATES", {name: True for name in foundation_gates.PLANNED_WRITER_GATES}, raising=False)
    monkeypatch.setattr(config, "STORAGE_OPERATOR_CONFIRMED", True, raising=False)
    monkeypatch.setattr(config, "REQUIRE_PROVIDER_QUALIFICATION", False, raising=False)
    admission.reset_for_tests()
    await db.init_db()
    await db.create_task_with_meta(
        TASK_ID, "interactive", "Add 20 and 22.", "classic",
        {"effective_configuration": {"budget_ceiling_usd": 0.25, "model_routing": {"medium": "model-a"}}},
        runtime_contract_version="1",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_the_task_run_admits_once_with_budget_fence_and_seed(admission_db):
    first = await admission.admit_task_run(
        task_id=TASK_ID, runtime_key=CLASSIC,
        effective_configuration={"budget_ceiling_usd": 0.25}, requested_seed=7,
    )
    assert first is not None and first["new"] is True
    assert first["run_id"] == "run-task-interactive"
    assert first["task_fence"] == "fence-task-interactive"
    assert first["reservation_id"] and first["budget_id"]
    control = await db.get_run_control(first["run_id"])
    assert control is not None and control["task_fence"] == first["task_fence"]
    chain = await journal.read_journal(run_id=first["run_id"])
    assert [record.operation_type for record in chain] == ["admission_identity"]
    assert (chain[0].runtime_id, chain[0].runtime_contract_version) == ("classic", "1")
    assert chain[0].payload["requested_seed"] == 7
    assert chain[0].payload["seed_policy"] == "recorded"
    async with db._connect() as connection:  # noqa: SLF001
        row = await (await connection.execute(
            "SELECT reserved_amount_nanos, state FROM budget_reservations WHERE run_id = ?", (first["run_id"],),
        )).fetchone()
        limit = await (await connection.execute(
            "SELECT limit_amount FROM budget_limits WHERE budget_id = ?", (first["budget_id"],),
        )).fetchone()
    assert row["state"] == "reserved"
    assert int(limit[0]) == 250_000
    assert await admission.reservation_for_run(first["run_id"]) == first["reservation_id"]
    # A second call returns the stored identity and writes nothing new.
    second = await admission.admit_task_run(task_id=TASK_ID, runtime_key=CLASSIC, effective_configuration={})
    assert second is not None and second["new"] is False and second["run_id"] == first["run_id"]
    assert len(await journal.read_journal(run_id=first["run_id"])) == 1


@pytest.mark.asyncio
async def test_the_gates_keep_the_task_on_the_legacy_path(admission_db, monkeypatch):
    monkeypatch.setattr(config, "FOUNDATION_GATES", {}, raising=False)
    assert await admission.admit_task_run(task_id=TASK_ID, runtime_key=CLASSIC, effective_configuration={}) is None
    assert await db.get_run_control("run-task-interactive") is None
    assert await journal.read_journal() == []


@pytest.mark.asyncio
async def test_the_policy_set_version_set_and_manifest_describe_the_task(admission_db):
    policy = admission.policy_set_from_configuration()
    assert policy.digest() == admission.policy_set_from_configuration().digest()
    versions = admission.version_set_for(CLASSIC)
    assert versions.database_schema_version == db.SCHEMA_VERSION
    assert versions.agent_protocol_version == "1"
    manifest = await admission.asset_manifest_for(TASK_ID)
    assert manifest.task_id == TASK_ID and manifest.entries == ()
    assert admission.budget_ceiling_usd({"classic": {"budget_ceiling_usd": 2.0}}) == 2.0
    assert admission.budget_ceiling_usd(None) == admission.DEFAULT_BUDGET_CEILING_USD
    report = await admission.storage_report()
    assert report["ready"] is True
    assert await admission.storage_report() is report


@pytest.mark.asyncio
async def test_a_rejected_prerequisite_fails_the_task_closed(admission_db, monkeypatch):
    from core.orchestrator import Orchestrator

    async def rejecting(*arguments, **keywords):
        raise run_admission.AdmissionPrerequisiteError("The storage readiness check rejected journal writers")

    monkeypatch.setattr(run_admission, "admit_run", rejecting)
    orchestrator = object.__new__(Orchestrator)
    with pytest.raises(VariantConfigurationError, match="rejected task"):
        await orchestrator._admit_foundation_run(TASK_ID, CLASSIC, {}, {"seed": 3})  # noqa: SLF001
    monkeypatch.setattr(config, "FOUNDATION_GATES", {}, raising=False)
    assert await orchestrator._admit_foundation_run(TASK_ID, CLASSIC, {}, None) is None  # noqa: SLF001
