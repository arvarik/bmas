"""Foundation Stage 0E: the complete run admission gate.

Admission creates the run, the immutable admission, the initial
reservation, the journal genesis, and the queue row in one atomic
transaction — or creates none of them. A failed admission keeps the
task open, and no cost-bearing action starts without a valid
reservation.
"""
from __future__ import annotations

import dataclasses

import pytest

import budget_service as budget
import database as db
import run_admission
import runtime_journal as journal
from core import failpoints
from core.asset_store import (
    AssetManifest,
    AssetManifestEntry,
    DataClass,
    TrustLevel,
)
from core.failpoints import InjectedFaultError
from core.human_controls import HumanControlService
from core.run_context import PolicySet
from core.run_contracts import (
    OutcomeLedger,
    ReasonRegistry,
    RunLedger,
    RunState,
    VersionSet,
)
from core.runtime_services import AuthorityError, create_runtime_services
from core.variants import RuntimeKey

TASK_ID = "task-admit"
RUN_ID = "run-admit"
CLASSIC_KEY = RuntimeKey("classic", "1")
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

POLICY_SET = PolicySet(
    schema_version="1",
    access_policy_digest="a" * 64,
    model_policy_digest="b" * 64,
    tool_policy_digest="c" * 64,
    environment_policy_digest="d" * 64,
    source_trust_policy_digest="e" * 64,
    redaction_policy_digest="f" * 64,
    retention_policy_digest="0" * 64,
)

MANIFEST = AssetManifest(
    manifest_id="manifest-admit",
    task_id=TASK_ID,
    entries=(
        AssetManifestEntry(
            asset_id="asset-brief",
            content_digest="1" * 64,
            size_bytes=64,
            media_type="text/markdown",
            source="user-upload",
            data_class=DataClass.INTERNAL,
            trust_level=TrustLevel.UNTRUSTED,
            access_policy="task-scope",
            scanner_version="1",
            extraction_version="1",
        ),
    ),
)

QUALIFICATIONS = {
    "qualification-provider": run_admission.QualificationRecord(
        "qualification-provider", "qualified", "2027-01-01T00:00:00.000Z",
    ),
    "qualification-adapter": run_admission.QualificationRecord(
        "qualification-adapter", "qualified", "2027-01-01T00:00:00.000Z",
    ),
    "qualification-expired": run_admission.QualificationRecord(
        "qualification-expired", "qualified", "2020-01-01T00:00:00.000Z",
    ),
    "qualification-revoked": run_admission.QualificationRecord(
        "qualification-revoked", "revoked", "2027-01-01T00:00:00.000Z",
    ),
}

STORAGE_READY = {"ready": True, "checks": []}

ADMISSION_TABLES = (
    "runtime_journal",
    "runs",
    "runtime_admissions",
    "run_queue",
    "journal_outbox",
    "run_budgets",
    "budget_limits",
    "budget_reservations",
)


def build_request(**overrides) -> run_admission.AdmissionRequest:
    arguments = dict(
        task_id=TASK_ID,
        run_id=RUN_ID,
        tenant_id="tenant-a",
        runtime_key=CLASSIC_KEY,
        version_set=VERSION_SET,
        specification_digest="1" * 64,
        capability_document_digest="2" * 64,
        prompt_profile_digest="3" * 64,
        role_profile_digest="4" * 64,
        asset_manifest=MANIFEST,
        asset_manifest_digest=MANIFEST.digest(),
        policy_set=POLICY_SET,
        policy_set_digest=POLICY_SET.digest(),
        seed_policy="recorded",
        requested_seed=7,
        required_reader_ids=("reader.checkpoint",),
        required_qualification_ids=(
            "qualification-provider", "qualification-adapter",
        ),
        budget_currency="USD",
        budget_limits=(
            budget.LimitSpec(
                "run", RUN_ID, "provider_cost", 10_000, currency="USD",
            ),
            budget.LimitSpec("run", RUN_ID, "model_calls", 20),
        ),
        initial_reservation_resources={
            "provider_cost": 2_000, "model_calls": 1,
        },
        admission_id="admission-fixed",
    )
    arguments.update(overrides)
    return run_admission.AdmissionRequest(**arguments)


@pytest.fixture(autouse=True)
def enabled_writer_gates(monkeypatch):
    """The admission writer consults its gates; these tests enable them."""
    import config
    from core import foundation_gates

    monkeypatch.setattr(
        config, "FOUNDATION_GATES",
        {name: True for name in foundation_gates.PLANNED_WRITER_GATES},
        raising=False,
    )


async def admit(request=None, *, database_time: str = BASE_TIME):
    return await run_admission.admit_run(
        request or build_request(),
        available_reader_ids=frozenset({"reader.checkpoint"}),
        qualification_fixture=QUALIFICATIONS,
        storage_report=STORAGE_READY,
        database_time=database_time,
    )


async def table_counts() -> dict[str, int]:
    counts = {}
    async with db._connect() as connection:  # noqa: SLF001
        for table in ADMISSION_TABLES:
            cursor = await connection.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            counts[table] = int(row[0])
    return counts


@pytest.fixture(autouse=True)
def clean_failpoints():
    failpoints.clear()
    yield
    failpoints.clear()


@pytest.fixture()
async def admission_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "admission.db"))
    await db.init_db()
    await db.create_task_with_meta(
        TASK_ID, "admit", "admit", "classic", {},
        runtime_contract_version="1",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_admission_creates_every_required_record(admission_db):
    result = await admit()
    record = result["journal_record"]
    cursor = record.journal_cursor

    counts = await table_counts()
    assert counts["runtime_journal"] == 1
    assert counts["runs"] == 1
    assert counts["runtime_admissions"] == 1
    assert counts["run_queue"] == 1
    assert counts["run_budgets"] == 1
    assert counts["budget_reservations"] == 1

    async with db._connect() as connection:  # noqa: SLF001
        admission_row = dict(await (await connection.execute(
            "SELECT * FROM runtime_admissions WHERE run_id = ?", (RUN_ID,),
        )).fetchone())
        queue_row = dict(await (await connection.execute(
            "SELECT * FROM run_queue WHERE run_id = ?", (RUN_ID,),
        )).fetchone())
        run_row = dict(await (await connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (RUN_ID,),
        )).fetchone())

    # The immutable admission holds every final field.
    assert admission_row["admission_id"] == "admission-fixed"
    assert admission_row["asset_manifest_id"] == "manifest-admit"
    assert admission_row["asset_manifest_digest"] == MANIFEST.digest()
    assert admission_row["policy_set_digest"] == POLICY_SET.digest()
    assert admission_row["seed_policy"] == "recorded"
    assert admission_row["run_budget_id"] == result["run_budget_id"]
    assert admission_row["initial_reservation_id"] == (
        result["initial_reservation_id"]
    )
    assert admission_row["journal_cursor"] == cursor

    # The queue row stores the admission identifier and digest.
    assert queue_row["admission_id"] == "admission-fixed"
    assert queue_row["admission_digest"] == result["admission_digest"]
    assert queue_row["delivery_state"] == "pending"

    # The run is admitted, and the initial reservation is reserved.
    assert run_row["state"] == "admitted"
    reservation = await budget.get_reservation(
        result["initial_reservation_id"],
    )
    assert reservation["state"] == "reserved"
    assert reservation["reserved_amount_nanos"] == 2_000

    # Replay agrees with the durable projections.
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_admission_is_idempotent(admission_db):
    first = await admit()
    repeat = await admit()
    assert repeat["journal_record"].transaction_id == (
        first["journal_record"].transaction_id
    )
    assert (await table_counts())["runtime_journal"] == 1


@pytest.mark.parametrize(
    "breakage, message",
    [
        (
            {"runtime_key": RuntimeKey("classic", "9")},
            "contract",
        ),
        (
            {"required_reader_ids": ("reader.gone",)},
            "reader",
        ),
        (
            {"asset_manifest_digest": "9" * 64},
            "manifest",
        ),
        (
            {"policy_set_digest": "9" * 64},
            "policy",
        ),
        (
            {"required_qualification_ids": ("qualification-unknown",)},
            "missing",
        ),
        (
            {"required_qualification_ids": ("qualification-expired",)},
            "expired",
        ),
        (
            {"required_qualification_ids": ("qualification-revoked",)},
            "revoked",
        ),
        (
            {"seed_policy": "vibes"},
            "seed policy",
        ),
        (
            {
                "initial_reservation_resources": {
                    "provider_cost": 10_001, "model_calls": 1,
                },
            },
            "reservation",
        ),
    ],
)
@pytest.mark.asyncio
async def test_every_failed_prerequisite_leaves_nothing(
    admission_db, breakage, message,
):
    with pytest.raises(Exception, match=message):
        await admit(build_request(**breakage))
    counts = await table_counts()
    assert all(count == 0 for count in counts.values()), counts
    # The task stays open after the failed run admission.
    task = await db.get_task(TASK_ID)
    assert task is not None
    assert task["status"] == "pending"


@pytest.mark.asyncio
async def test_storage_readiness_gates_admission(admission_db):
    with pytest.raises(run_admission.AdmissionPrerequisiteError, match="storage"):
        await run_admission.admit_run(
            build_request(),
            available_reader_ids=frozenset({"reader.checkpoint"}),
            qualification_fixture=QUALIFICATIONS,
            storage_report={"ready": False, "checks": []},
            database_time=BASE_TIME,
        )
    assert (await table_counts())["runtime_journal"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint_name",
    [name for name in journal.JOURNAL_FAILPOINTS if name != "journal.after_commit"],
)
async def test_an_injected_crash_admits_completely_or_not_at_all(
    admission_db, failpoint_name,
):
    failpoints.arm(failpoint_name)
    with pytest.raises(InjectedFaultError):
        await admit()
    counts = await table_counts()
    assert all(count == 0 for count in counts.values()), (
        failpoint_name, counts,
    )
    # The same admission then commits completely exactly once.
    result = await admit()
    counts = await table_counts()
    assert counts["runtime_journal"] == 1
    assert counts["run_budgets"] == 1
    assert counts["budget_reservations"] == 1
    reservation = await budget.get_reservation(
        result["initial_reservation_id"],
    )
    assert reservation["state"] == "reserved"


@pytest.mark.asyncio
async def test_concurrent_admissions_respect_task_aggregates(admission_db):
    # Three runs under one task share one task-scope cost aggregate.
    # Each run reserves 2000 nanos against a 5000-nano task limit, so
    # at most two admissions fit.
    import asyncio

    def request_for(index: int) -> run_admission.AdmissionRequest:
        run_id = f"run-shared-{index}"
        return build_request(
            run_id=run_id,
            admission_id=f"admission-shared-{index}",
            budget_limits=(
                budget.LimitSpec(
                    "run", run_id, "provider_cost", 10_000, currency="USD",
                ),
                budget.LimitSpec(
                    "task", TASK_ID, "provider_cost", 5_000, currency="USD",
                ),
            ),
        )

    outcomes = await asyncio.gather(
        *(admit(request_for(index)) for index in range(3)),
        return_exceptions=True,
    )
    admitted = [
        outcome for outcome in outcomes if isinstance(outcome, dict)
    ]
    rejected = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, run_admission.AdmissionReservationError)
    ]
    assert len(admitted) == 2
    assert len(rejected) == 1
    assert (await table_counts())["runs"] == 2


@pytest.mark.asyncio
async def test_no_cost_bearing_action_starts_without_a_reservation(
    admission_db,
):
    result = await admit()
    reservation_id = result["initial_reservation_id"]

    run_ledger = RunLedger()
    run = run_ledger.create_run(
        task_id=TASK_ID, tenant_id="tenant-a", runtime_key=CLASSIC_KEY,
    )
    run_ledger.transition(run.run_id, RunState.QUEUED)
    run_ledger.transition(run.run_id, RunState.RUNNING)
    await db.create_run_control(
        run.run_id, TASK_ID, "fence-admit", database_time=BASE_TIME,
    )

    async def fixed_time() -> str:
        return BASE_TIME

    services = create_runtime_services(
        run_id=run.run_id,
        lease_owner="worker-one",
        lease_fence="lease-fence-one",
        scheduler=True,
        run_ledger=run_ledger,
        outcome_ledger=OutcomeLedger(run_ledger, ReasonRegistry()),
        invalidations=None,  # type: ignore[arg-type]
        assets=None,  # type: ignore[arg-type]
        artifacts=None,  # type: ignore[arg-type]
        controls=HumanControlService(
            run_ledger=run_ledger,
            authorized_actor_ids=frozenset({"operator-lead"}),
            database_time=fixed_time,
        ),
        database_time=fixed_time,
        reservation_validator=budget.reservation_is_valid,
    )
    assert await services.task_leases.acquire() is True
    from test_fence_and_cancellation import build_context

    context = build_context(run.run_id)

    # No reservation: the effect cannot approve.
    with pytest.raises(AuthorityError) as failure:
        await services.effects.approve(context, "effect-a", "model")
    assert failure.value.reason == "reservation"
    # An unknown reservation: denied.
    with pytest.raises(AuthorityError):
        await services.models.invoke(
            context, {"prompt": "hello"},
        )
    # A valid reserved reservation: approved.
    entry = await services.effects.approve(
        context, "effect-a", "model", reservation_id=reservation_id,
    )
    assert entry.payload["reservation_id"] == reservation_id
    # After release, the same reservation no longer authorizes cost.
    await budget.release(reservation_id)
    with pytest.raises(AuthorityError):
        await services.effects.approve(
            context, "effect-b", "model", reservation_id=reservation_id,
        )


@pytest.mark.asyncio
async def test_admission_validates_the_task_fence_in_transaction(
    admission_db,
):
    await db.create_run_control(
        RUN_ID, TASK_ID, "fence-live", database_time=BASE_TIME,
    )
    with pytest.raises(journal.JournalFenceError):
        await admit(build_request(task_fence="fence-stale"))
    assert (await table_counts())["runtime_journal"] == 0
    result = await admit(build_request(task_fence="fence-live"))
    assert result["journal_record"].run_sequence == 0


def test_the_admission_request_is_immutable():
    request = build_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.run_id = "run-other"  # type: ignore[misc]
