"""Foundation Stage 0C: authenticated, audited human controls.

Every control operation authenticates its actor and appends one audit
record. No control changes an admitted specification in place: a
reroute creates a successor run, and ordinary settings edits affect
new run admissions only.
"""
from __future__ import annotations

import dataclasses

import pytest

import database as db
from core.human_controls import (
    HUMAN_CONTROL_OPERATIONS,
    HumanControlAuthenticationError,
    HumanControlService,
)
from core.run_contracts import (
    RunLedger,
    RunLineageReason,
    RunState,
    VersionSet,
    compile_run_admission,
)
from core.variants import RuntimeKey

CLASSIC_KEY = RuntimeKey("classic", "1")
PATCHBOARD_KEY = RuntimeKey("patchboard", "1")

BASE_TIME = "2026-09-01T00:00:00.000Z"

VERSION_SET = VersionSet(
    runtime_spec_schema_version="1",
    runtime_state_schema_version="1",
    checkpoint_schema_version="1",
    activation_schema_version="1",
    activation_dispatch_schema_version="1",
    activation_acknowledgement_schema_version="1",
    digest_profile_version="1",
    runtime_outcome_schema_version="1",
    post_terminal_invalidation_schema_version="1",
    agent_protocol_version="1",
    agent_receipt_schema_version="1",
    effect_schema_version="1",
    trace_schema_version="1",
    evidence_schema_version="1",
    asset_manifest_schema_version="1",
    policy_set_schema_version="1",
    capability_document_version="1",
    database_schema_version=db.SCHEMA_VERSION,
)


async def fixed_time() -> str:
    return BASE_TIME


@pytest.fixture()
async def control_setup(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "controls.db"))
    await db.init_db()
    run_ledger = RunLedger()
    run = run_ledger.create_run(
        task_id="task-controls", tenant_id="tenant-a", runtime_key=CLASSIC_KEY,
    )
    run_ledger.transition(run.run_id, RunState.QUEUED)
    run_ledger.transition(run.run_id, RunState.RUNNING)
    admission = compile_run_admission(
        admission_id="admission-controls",
        task_id="task-controls",
        run_id=run.run_id,
        runtime_key=CLASSIC_KEY,
        version_set=VERSION_SET,
        specification_digest="1" * 64,
        capability_document_digest="2" * 64,
        prompt_profile_digest="3" * 64,
        role_profile_digest="4" * 64,
        seed_policy="recorded",
        requested_seed=None,
        required_reader_ids=(),
        interface_adapter_id="classic",
        available_reader_ids=frozenset(),
    )
    run_ledger.bind_admission(run.run_id, admission)
    await db.create_run_control(
        run.run_id, "task-controls", "fence-controls", database_time=BASE_TIME,
    )
    service = HumanControlService(
        run_ledger=run_ledger,
        authorized_actor_ids=frozenset({"operator-lead"}),
        database_time=fixed_time,
    )
    return run_ledger, run, admission, service


def test_the_operation_set_is_registered():
    assert HUMAN_CONTROL_OPERATIONS == (
        "pause", "resume", "cancel", "reroute", "approve", "waive",
    )


@pytest.mark.asyncio
async def test_every_operation_authenticates_its_actor(control_setup):
    _, run, _, service = control_setup
    with pytest.raises(HumanControlAuthenticationError):
        await service.pause(run.run_id, actor_id="stranger", reason="curious")
    with pytest.raises(HumanControlAuthenticationError):
        await service.cancel(run.run_id, actor_id="stranger", reason="stop")
    with pytest.raises(HumanControlAuthenticationError):
        await service.reroute(
            run.run_id,
            actor_id="stranger",
            reason="move",
            runtime_key=PATCHBOARD_KEY,
        )
    assert service.audit_journal == []


@pytest.mark.asyncio
async def test_pause_and_resume_record_prior_and_new_state(control_setup):
    _, run, _, service = control_setup
    paused = await service.pause(
        run.run_id, actor_id="operator-lead", reason="inspect outputs",
    )
    assert paused.operation == "pause"
    assert paused.actor_id == "operator-lead"
    assert (paused.prior_state, paused.new_state) == ("active", "paused")
    assert paused.runtime_key == CLASSIC_KEY
    assert paused.task_fence == "fence-controls"
    assert paused.journal_cursor == 0
    assert paused.recorded_at == BASE_TIME
    control = await db.get_run_control(run.run_id)
    assert control["pause_state"] == "paused"

    resumed = await service.resume(
        run.run_id, actor_id="operator-lead", reason="inspection done",
    )
    assert (resumed.prior_state, resumed.new_state) == ("paused", "active")
    assert resumed.journal_cursor == 1
    control = await db.get_run_control(run.run_id)
    assert control["pause_state"] == "active"


@pytest.mark.asyncio
async def test_cancel_moves_the_durable_cancellation_state(control_setup):
    _, run, _, service = control_setup
    record = await service.cancel(
        run.run_id, actor_id="operator-lead", reason="wrong objective",
    )
    assert (record.prior_state, record.new_state) == ("active", "requested")
    control = await db.get_run_control(run.run_id)
    assert control["cancellation_state"] == "requested"


@pytest.mark.asyncio
async def test_reroute_creates_an_audited_successor_without_mutation(
    control_setup,
):
    run_ledger, run, admission, service = control_setup
    admission_before = dataclasses.asdict(admission)
    pair_before = run.runtime_key

    record, successor = await service.reroute(
        run.run_id,
        actor_id="operator-lead",
        reason="move to the parallel runtime",
        runtime_key=PATCHBOARD_KEY,
    )

    # The successor carries the reroute lineage.
    assert successor.rerouted_from_run_id == run.run_id
    assert successor.lineage_reason is RunLineageReason.REROUTE
    assert successor.runtime_key == PATCHBOARD_KEY
    # The admitted history did not change.
    assert run.runtime_key == pair_before
    assert dataclasses.asdict(
        run_ledger.admission_for(run.run_id)
    ) == admission_before
    assert run.state is RunState.CANCELLING
    # The audit record is complete.
    assert record.operation == "reroute"
    assert record.prior_state == "running"
    assert record.new_state == "cancelling"
    assert record.runtime_key == CLASSIC_KEY


@pytest.mark.asyncio
async def test_approve_and_waive_append_audit_records(control_setup):
    _, run, _, service = control_setup
    approved = await service.approve(
        run.run_id,
        actor_id="operator-lead",
        reason="reviewed",
        subject="external effect",
    )
    waived = await service.waive(
        run.run_id,
        actor_id="operator-lead",
        reason="not required",
        subject="budget escalation",
    )
    assert approved.new_state == "approved"
    assert waived.new_state == "waived"
    journal = service.audit_journal
    assert [record.journal_cursor for record in journal] == [0, 1]
    assert all(record.task_fence == "fence-controls" for record in journal)


@pytest.mark.asyncio
async def test_the_service_never_edits_an_admitted_specification(
    control_setup,
):
    _, _, _, service = control_setup
    # No control operation writes an admission or a settings record;
    # an ordinary settings edit reaches new run admissions only.
    mutating_names = [
        name
        for name in dir(service)
        if not name.startswith("_")
        and callable(getattr(service, name))
    ]
    assert sorted(mutating_names) == sorted(HUMAN_CONTROL_OPERATIONS)
