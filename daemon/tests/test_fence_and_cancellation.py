"""Foundation Stage 0C: fence, cancellation, deadline, and clock tests.

Every state mutation validates the live run-control row: the lease
owner, the fence, the lease expiry, the cancellation state, and the
deadline. The frozen run context never holds a live control value, and
authority decisions use database UTC time only.
"""
from __future__ import annotations

import dataclasses

import pytest

import database as db
from core.human_controls import HumanControlService
from core.run_context import (
    PolicyDigestMismatchError,
    PolicySet,
    RunContext,
    SeedPolicy,
    assert_frozen_contract_holds_no_live_value,
    compile_fenced_admission,
    create_run_context,
    validate_policy_set_digest,
)
from core.run_contracts import (
    OutcomeClass,
    OutcomeLedger,
    ReasonBinding,
    ReasonRegistry,
    RunContractError,
    RunLedger,
    RunState,
    VersionSet,
)
from core.runtime_services import (
    AuthorityError,
    LeaseAuthorityError,
    MonotonicClock,
    create_runtime_services,
)
from core.variants import RuntimeKey

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


class FakeDatabaseClock:
    """A settable database UTC clock for authority fault injection."""

    def __init__(self, start: str = BASE_TIME) -> None:
        self.current = start

    async def now(self) -> str:
        return self.current

    def shift(self, timestamp: str) -> None:
        self.current = timestamp


def build_admission(run_id: str = "run-fence") -> object:
    return compile_fenced_admission(
        policy_set=POLICY_SET,
        policy_set_digest=POLICY_SET.digest(),
        admission_id="admission-fence",
        task_id="task-fence",
        run_id=run_id,
        runtime_key=CLASSIC_KEY,
        version_set=VERSION_SET,
        specification_digest="1" * 64,
        capability_document_digest="2" * 64,
        prompt_profile_digest="3" * 64,
        role_profile_digest="4" * 64,
        seed_policy="recorded",
        requested_seed=7,
        required_reader_ids=(),
        interface_adapter_id="classic",
        available_reader_ids=frozenset(),
    )


def build_context(run_id: str = "run-fence") -> RunContext:
    return create_run_context(
        admission=build_admission(run_id),
        policy_set=POLICY_SET,
        policy_set_digest=POLICY_SET.digest(),
        asset_manifest_id="manifest-fence",
        asset_manifest_digest="5" * 64,
        task_fence="fence-current",
        lease_ref=f"lease:{run_id}",
        run_control_ref=f"run-control:{run_id}",
    )


@pytest.fixture()
async def control_environment(tmp_path, monkeypatch):
    """One migrated database with one leased run-control row."""
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "controls.db"))
    await db.init_db()
    clock = FakeDatabaseClock()
    run_ledger = RunLedger()
    run = run_ledger.create_run(
        task_id="task-fence", tenant_id="tenant-a", runtime_key=CLASSIC_KEY,
    )
    run_ledger.transition(run.run_id, RunState.QUEUED)
    run_ledger.transition(run.run_id, RunState.RUNNING)
    await db.create_run_control(
        run.run_id, "task-fence", "fence-current", database_time=clock.current,
    )
    registry = ReasonRegistry()
    registry.register_mapping(
        CLASSIC_KEY,
        {
            "task_answer_accepted": ReasonBinding(
                OutcomeClass.SUCCESS, retryable=False,
            ),
            "coordination_stalled": ReasonBinding(
                OutcomeClass.SUBSTANTIVE_FAILURE, retryable=True,
            ),
            "operator_cancelled": ReasonBinding(
                OutcomeClass.CANCELLATION, retryable=False,
            ),
        },
        mapping_version="1",
    )
    outcome_ledger = OutcomeLedger(run_ledger, registry)
    return clock, run_ledger, outcome_ledger, run


def build_services(
    run_id: str,
    clock: FakeDatabaseClock,
    run_ledger: RunLedger,
    outcome_ledger: OutcomeLedger,
    *,
    owner: str = "worker-one",
    fence: str = "lease-fence-one",
    scheduler: bool = True,
):
    controls = HumanControlService(
        run_ledger=run_ledger,
        authorized_actor_ids=frozenset({"operator-lead"}),
        database_time=clock.now,
    )
    return create_runtime_services(
        run_id=run_id,
        lease_owner=owner,
        lease_fence=fence,
        scheduler=scheduler,
        run_ledger=run_ledger,
        outcome_ledger=outcome_ledger,
        invalidations=None,  # type: ignore[arg-type]
        assets=None,  # type: ignore[arg-type]
        artifacts=None,  # type: ignore[arg-type]
        controls=controls,
        database_time=clock.now,
        lease_ttl_seconds=60.0,
    )


async def all_mutations(services, context):
    """Return one callable per fenced mutation boundary."""
    return [
        ("state", lambda: services.mutations.commit_state_change(
            context, {"field": "value"},
        )),
        ("checkpoint", lambda: services.checkpoints.save(
            context, {"progress": 1},
        )),
        ("budget", lambda: services.budgets.reserve(context, 1000)),
        ("effect", lambda: services.effects.approve(
            context, "effect-a", "http-call",
        )),
        ("activation", lambda: services.activations.transition(
            context, "activation-a", "completed",
        )),
    ]


@pytest.mark.asyncio
async def test_the_current_fence_commits_and_stale_fences_cannot(
    control_environment,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    holder = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await holder.task_leases.acquire() is True

    entry = await holder.mutations.commit_state_change(context, {"ok": True})
    assert entry.control_version > 0

    # A different lease owner cannot commit with the same fence.
    intruder = build_services(
        run.run_id, clock, run_ledger, outcome_ledger,
        owner="worker-two", fence="lease-fence-one",
    )
    for name, mutation in await all_mutations(intruder, context):
        with pytest.raises(AuthorityError) as failure:
            await mutation()
        assert failure.value.reason == "lease_owner", name

    # The stale fence cannot mutate any boundary.
    stale = build_services(
        run.run_id, clock, run_ledger, outcome_ledger,
        owner="worker-one", fence="lease-fence-stale",
    )
    for name, mutation in await all_mutations(stale, context):
        with pytest.raises(AuthorityError) as failure:
            await mutation()
        assert failure.value.reason == "stale_fence", name


@pytest.mark.asyncio
async def test_an_expired_lease_cannot_commit_with_the_same_fence(
    control_environment,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    holder = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await holder.task_leases.acquire() is True
    clock.shift("2026-09-01T00:02:00.000Z")
    for name, mutation in await all_mutations(holder, context):
        with pytest.raises(AuthorityError) as failure:
            await mutation()
        assert failure.value.reason == "lease_expired", name


@pytest.mark.asyncio
async def test_cancellation_stops_new_effects_and_reconciles_in_flight(
    control_environment,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    services = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await services.task_leases.acquire() is True

    # New work is possible before cancellation.
    await services.activations.dispatch(context, "activation-a", "planner")
    started = await services.external_effects.execute(
        context, kind="http-call", request={"target": "example"},
    )

    assert await services.cancellation.request() is True
    # Cancellation stops every new mutation at every boundary.
    for name, mutation in await all_mutations(services, context):
        with pytest.raises(AuthorityError) as failure:
            await mutation()
        assert failure.value.reason == "cancelled", name
    with pytest.raises(AuthorityError):
        await services.activations.dispatch(context, "activation-b", "critic")
    with pytest.raises(AuthorityError):
        await services.models.invoke(context, {"prompt": "more work"})
    with pytest.raises(AuthorityError):
        await services.mutations.commit_terminal_outcome(
            context, "task_answer_accepted",
        )

    # The in-flight effect reconciles without a state commit.
    committed_before = services.mutations.committed_changes
    record = await services.effects.reconcile(
        started["effect_id"], {"status": "returned"},
    )
    assert record["state"] == "reconciled"
    assert services.mutations.committed_changes == committed_before

    assert await services.cancellation.finalize() is True
    control = await services.run_controls.read()
    assert control["cancellation_state"] == "terminal"


@pytest.mark.asyncio
async def test_a_deadline_rejects_every_boundary_and_stays_expired(
    control_environment,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    services = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await services.task_leases.acquire() is True
    await services.run_controls.set_deadline(
        "2026-09-01T00:00:30.000Z", "fail_run",
    )

    # The deadline expires through a forward database-time jump.
    clock.shift("2026-09-01T00:00:45.000Z")
    assert await services.task_leases.renew() is True
    for name, mutation in await all_mutations(services, context):
        with pytest.raises(AuthorityError) as failure:
            await mutation()
        assert failure.value.reason == "deadline", name

    # A later backward correction cannot reverse the expiry decision.
    clock.shift("2026-09-01T00:00:20.000Z")
    with pytest.raises(AuthorityError) as failure:
        await services.mutations.commit_state_change(context, {"late": True})
    assert failure.value.reason in ("deadline", "clock_fault")
    control = await services.run_controls.read()
    assert control["deadline_expired"] == 1


@pytest.mark.asyncio
async def test_the_frozen_context_holds_no_live_control_value(
    control_environment,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    services = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await services.task_leases.acquire() is True
    frozen = dataclasses.asdict(context)

    # Renew the lease after the context freezes.
    clock.shift("2026-09-01T00:00:10.000Z")
    assert await services.task_leases.renew() is True

    # The context never carried a live value, so nothing moved.
    assert dataclasses.asdict(context) == frozen
    assert_frozen_contract_holds_no_live_value(RunContext)
    assert_frozen_contract_holds_no_live_value(PolicySet)
    live_tokens = (
        "lease_owner", "lease_expires", "pause", "cancellation", "deadline",
        "access_grant",
    )
    for field_name in frozen:
        for token in live_tokens:
            assert token not in field_name

    # Every mutation reads the renewed owner and expiry from the
    # durable run-control row, not from the context.
    entry = await services.mutations.commit_state_change(
        context, {"after": "renewal"},
    )
    control = await services.run_controls.read()
    assert entry.control_version == control["control_version"]


def test_policy_set_digests_reject_every_mismatch():
    digest = POLICY_SET.digest()
    validate_policy_set_digest(POLICY_SET, digest)
    for spec in dataclasses.fields(PolicySet):
        if spec.name == "schema_version":
            changed = dataclasses.replace(POLICY_SET, schema_version="2")
        else:
            changed = dataclasses.replace(POLICY_SET, **{spec.name: "9" * 64})
        assert changed.digest() != digest
        with pytest.raises(PolicyDigestMismatchError):
            validate_policy_set_digest(changed, digest)


def test_policy_set_schema_rejects_live_values():
    with pytest.raises(TypeError):
        PolicySet(
            schema_version="1",
            access_policy_digest="a" * 64,
            model_policy_digest="b" * 64,
            tool_policy_digest="c" * 64,
            environment_policy_digest="d" * 64,
            source_trust_policy_digest="e" * 64,
            redaction_policy_digest="f" * 64,
            retention_policy_digest="0" * 64,
            lease_owner="worker-one",
        )
    for live_field in (
        "owner", "access_grant", "lease_expires_at", "cancellation_state",
        "pause_state", "deadline_at",
    ):
        bad_contract = dataclasses.make_dataclass(
            "PolicyDraft", [(live_field, str)], frozen=True,
        )
        with pytest.raises(RunContractError):
            assert_frozen_contract_holds_no_live_value(bad_contract)


def test_a_policy_digest_mismatch_blocks_admission_and_context():
    with pytest.raises(PolicyDigestMismatchError):
        compile_fenced_admission(
            policy_set=POLICY_SET,
            policy_set_digest="9" * 64,
            admission_id="admission-blocked",
            task_id="task-fence",
            run_id="run-blocked",
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
    with pytest.raises(PolicyDigestMismatchError):
        create_run_context(
            admission=build_admission("run-blocked"),
            policy_set=POLICY_SET,
            policy_set_digest="9" * 64,
            asset_manifest_id="manifest-fence",
            asset_manifest_digest="5" * 64,
            task_fence="fence-current",
            lease_ref="lease:run-blocked",
            run_control_ref="run-control:run-blocked",
        )


def test_a_context_requires_the_authorized_asset_manifest():
    with pytest.raises(RunContractError):
        create_run_context(
            admission=build_admission("run-no-manifest"),
            policy_set=POLICY_SET,
            policy_set_digest=POLICY_SET.digest(),
            asset_manifest_id="",
            asset_manifest_digest="5" * 64,
            task_fence="fence-current",
            lease_ref="lease:run-no-manifest",
            run_control_ref="run-control:run-no-manifest",
        )
    context = build_context("run-with-manifest")
    assert context.asset_manifest_id == "manifest-fence"
    assert context.seed_policy is SeedPolicy.RECORDED


@pytest.mark.asyncio
async def test_live_revocation_defeats_a_frozen_context(control_environment):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    services = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await services.task_leases.acquire() is True
    await services.mutations.commit_state_change(context, {"before": True})

    # The durable authority revokes access after the context froze.
    assert await services.cancellation.request() is True
    with pytest.raises(AuthorityError):
        await services.activations.dispatch(context, "activation-x", "planner")
    with pytest.raises(AuthorityError):
        await services.mutations.commit_state_change(context, {"after": True})
    inspection = await services.task_leases.inspect()
    assert inspection["valid"] is True  # The lease still exists.
    control = await services.run_controls.read()
    assert control["cancellation_state"] == "requested"


@pytest.mark.asyncio
async def test_pause_state_stops_new_dispatch_only(control_environment):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    services = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await services.task_leases.acquire() is True
    await services.run_controls.set_paused(True)
    with pytest.raises(AuthorityError) as failure:
        await services.activations.dispatch(context, "activation-p", "planner")
    assert failure.value.reason == "paused"
    # A checkpoint of in-flight state still commits while paused.
    assert await services.checkpoints.save(context, {"paused": True})
    await services.run_controls.set_paused(False)
    assert await services.activations.dispatch(
        context, "activation-p", "planner",
    )


@pytest.mark.asyncio
async def test_a_clock_rollback_stops_lease_effect_and_terminal_authority(
    control_environment,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    services = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await services.task_leases.acquire() is True

    # Database UTC moves backward beyond the allowed tolerance.
    clock.shift("2026-08-31T23:59:00.000Z")
    with pytest.raises(AuthorityError) as failure:
        await services.effects.approve(context, "effect-c", "http-call")
    assert failure.value.reason == "clock_fault"
    with pytest.raises(AuthorityError):
        await services.mutations.commit_terminal_outcome(
            context, "task_answer_accepted",
        )
    assert await services.task_leases.acquire() is False
    assert await services.task_leases.renew() is False
    control = await services.run_controls.read()
    assert control["clock_fault"] == 1

    # The operator corrects time and creates a new fence before resume.
    clock.shift("2026-09-01T00:00:05.000Z")
    with pytest.raises(AuthorityError):
        await services.mutations.commit_state_change(context, {"still": True})
    assert await services.run_controls.clear_clock_fault("fence-after-fault")
    control = await services.run_controls.read()
    assert control["clock_fault"] == 0
    assert control["task_fence"] == "fence-after-fault"
    assert await services.mutations.commit_state_change(context, {"ok": True})


@pytest.mark.asyncio
async def test_a_forward_jump_expires_a_lease_durably(control_environment):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    services = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await services.task_leases.acquire() is True

    clock.shift("2026-09-01T00:05:00.000Z")
    with pytest.raises(AuthorityError) as failure:
        await services.mutations.commit_state_change(context, {"x": 1})
    assert failure.value.reason == "lease_expired"

    # Small backward corrections inside the tolerance cannot revive the
    # lease: the expiry decision is durable.
    clock.shift("2026-09-01T00:04:59.500Z")
    with pytest.raises(AuthorityError) as failure:
        await services.mutations.commit_state_change(context, {"x": 2})
    assert failure.value.reason == "lease_expired"
    control = await services.run_controls.read()
    assert control["lease_expired"] == 1


@pytest.mark.asyncio
async def test_recovery_uses_no_persisted_monotonic_value(
    control_environment,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    # The durable schema stores no monotonic value.
    control = await db.get_run_control(run.run_id)
    assert all("monotonic" not in column for column in control)
    # A restart creates a new monotonic origin; deadlines reload from
    # the database instead.
    first_origin = MonotonicClock().elapsed_origin()
    second_origin = MonotonicClock().elapsed_origin()
    assert second_origin >= first_origin
    await db.set_run_deadline(run.run_id, "2026-09-01T01:00:00.000Z", "fail_run")
    reloaded = await db.get_run_control(run.run_id)
    assert reloaded["deadline_at"] == "2026-09-01T01:00:00.000Z"


@pytest.mark.asyncio
async def test_two_workers_share_database_utc_authority(
    control_environment, monkeypatch,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    context = build_context(run.run_id)
    holder = build_services(run.run_id, clock, run_ledger, outcome_ledger)
    assert await holder.task_leases.acquire() is True

    # Two workers with different process wall clocks share one database
    # clock, so the authority decision is identical for both.
    import time as time_module

    real_time = time_module.time
    for wall_offset in (-3600.0, 3600.0):
        monkeypatch.setattr(
            time_module,
            "time",
            lambda offset=wall_offset, base=real_time: base() + offset,
        )
        entry = await holder.mutations.commit_state_change(
            context, {"offset": wall_offset},
        )
        assert entry.database_time == clock.current


@pytest.mark.asyncio
async def test_only_the_scheduler_transfers_lease_authority(
    control_environment,
):
    clock, run_ledger, outcome_ledger, run = control_environment
    runtime_handle = build_services(
        run.run_id, clock, run_ledger, outcome_ledger, scheduler=False,
    )
    with pytest.raises(LeaseAuthorityError):
        await runtime_handle.task_leases.acquire()
    with pytest.raises(LeaseAuthorityError):
        await runtime_handle.task_leases.renew()
    with pytest.raises(LeaseAuthorityError):
        await runtime_handle.task_leases.release()
    # The runtime can still inspect lease validity.
    inspection = await runtime_handle.task_leases.inspect()
    assert inspection["valid"] is False
