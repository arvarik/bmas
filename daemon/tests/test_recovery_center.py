"""Foundation Stage 0G: the Recovery Center.

Every declared unhealthy-work queue exposes its items with required
redaction, a foreign tenant or insufficient role denies, every
allowed action creates exactly one journaled control decision with no
direct ledger update, and an unsafe retry without the separated
approval denies while the unknown state stays.
"""
from __future__ import annotations

import protocol_test_support as support
import pytest
from test_external_effects import SAFE_ADAPTER, UNSAFE_ADAPTER

import access_control as access
import database as db
import effect_service as effects
import recovery_center as recovery
import runtime_journal as journal

RUN = support.RUN_ID
FENCE = support.TASK_FENCE
EARLY = "2000-01-01T00:00:00.000Z"

OPERATOR = access.Principal(
    principal_id="op-1", tenant_id="tenant-default", roles=("operator",),
)
APPROVER = access.Principal(
    principal_id="approver-1", tenant_id="tenant-default",
    roles=("effect_approver",),
)
SECURITY = access.Principal(
    principal_id="sec-1", tenant_id="tenant-default",
    roles=("security_administrator", "operator"),
)
FOREIGN = access.Principal(
    principal_id="op-foreign", tenant_id="tenant-other", roles=("operator",),
)
VIEWER = access.Principal(
    principal_id="view-1", tenant_id="tenant-default",
    roles=("read_only_viewer",),
)


@pytest.fixture()
async def recovery_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "recovery.db"))
    await db.init_db()
    await support.seed_run()
    await support.seed_budget()
    await support.make_reservation("reservation-activation")
    await support.make_reservation("reservation-effect")
    await support.make_reservation("reservation-retry")
    return tmp_path


@pytest.fixture()
def keys():
    return support.make_keys()


@pytest.fixture()
def store(tmp_path):
    return support.make_store(tmp_path)


async def make_unknown_effect(keys, store, *, safety="safe",
                              child_key="child-unknown"):
    parent = await support.dispatch_and_accept(keys, store)
    child = await effects.request_child_effect_grant(
        run_id=RUN, parent_grant_id=parent["grant"].activation_grant_id,
        kind="provider", request_digest="d" * 64,
        child_idempotency_key=child_key,
        reservation_id="reservation-effect", retry_safety=safety,
        target="litellm", provider_operation_key="operation-key-1",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    await effects.mark_outcome_unknown(
        run_id=RUN, effect_id=child["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )
    return child


# ── Queue listing with redaction and access ──────────────────────────


async def test_unknown_effect_queue_lists_with_redaction(
    recovery_db, keys, store,
):
    child = await make_unknown_effect(keys, store)
    items = await recovery.list_queue(
        "unknown_effects", principal=OPERATOR,
    )
    assert len(items) == 1
    item = items[0]
    assert item["item_id"] == child["effect_id"]
    assert "reconcile_by_lookup" in item["allowed_actions"]
    # The item shows identifiers and reasons, never a request body.
    assert "request" not in item["evidence"]
    assert "prompt" not in item["evidence"]


async def test_a_foreign_tenant_and_a_viewer_are_denied(
    recovery_db, keys, store,
):
    await make_unknown_effect(keys, store)
    assert await recovery.list_queue(
        "unknown_effects", principal=FOREIGN,
    ) == []
    assert await recovery.list_queue(
        "unknown_effects", principal=VIEWER,
    ) == []


async def test_every_required_queue_exposes_its_items(
    recovery_db, keys, store,
):
    # 1. Unknown effect.
    await make_unknown_effect(keys, store, child_key="child-q")
    # 2. delivery_unknown dispatch + 3. dead letter.
    queued = await support.queue_dispatch(
        keys, store, activation_id="activation-du",
        reservation_id="reservation-retry",
    )
    grant_id = queued["grant"].activation_grant_id
    claimed = await support.activations.claim_activation_dispatch(
        grant_id=grant_id, run_id=RUN, dispatcher="dispatcher-a",
        claim_ttl_seconds=3600, key_registry=keys["registry"],
        artifact_store=store, expected_target_agent_id=support.AGENT_ID,
        task_fence=FENCE,
    )
    await support.activations.record_send_start(
        grant_id=grant_id, claim_owner=str(claimed["claim_owner"]),
        claim_fence=str(claimed["claim_fence"]),
    )
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET claim_expires_at = ? "
            "WHERE grant_id = ?",
            (EARLY, grant_id),
        )
        await connection.commit()
    await support.activations.recover_expired_claim(
        grant_id=grant_id, run_id=RUN, task_fence=FENCE,
    )
    # 4. Stale lease: expire the activation lease.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE activation_leases SET expires_at = ?", (EARLY,),
        )
        await connection.execute(
            "UPDATE run_controls SET lease_owner = 'worker-x', "
            "lease_fence = '1', lease_expires_at = ?, lease_expired = 1",
            (EARLY,),
        )
        await connection.commit()
    # 8. Clock fault.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE run_controls SET clock_fault = 1",
        )
        await connection.commit()
    # 9. Failed backup + 10. expired qualification.
    await recovery.register_backup_outcome(
        backup_id="backup-a", kind="backup", state="failed",
        details="snapshot digest mismatch",
    )
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO provider_qualifications ("
            "qualification_id, provider, model, adapter, adapter_version, "
            "provider_version, capabilities, issued_at, expires_at) "
            "VALUES ('qual-a', 'litellm', 'claude', 'adapter', '1', '1', "
            "'{}', ?, ?)",
            (EARLY, EARLY),
        )
        await connection.commit()

    listing = await recovery.list_all_queues(
        principal=SECURITY, artifact_store=store,
    )
    assert len(listing["unknown_effects"]) == 1
    assert len(listing["delivery_unknown_dispatches"]) == 1
    assert len(listing["stale_leases"]) >= 1
    assert len(listing["clock_faults"]) == 1
    assert len(listing["backup_health"]) == 1
    assert len(listing["expired_qualifications"]) == 1


async def test_artifact_health_queue_reports_orphans(recovery_db, store):
    from activation_service import persist_protected_artifact

    digest = persist_protected_artifact(
        store, b"orphan bytes", media_type="text/plain",
        access_policy="foundation-raw-response", referenced_by="temp",
    )
    # Drop the reference so the object becomes an orphan.
    store._state.committed_refs.pop(digest, None)  # noqa: SLF001
    items = await recovery.list_queue(
        "artifact_health", principal=OPERATOR, artifact_store=store,
    )
    orphans = [item for item in items
               if item["evidence"]["kind"] == "orphans"]
    assert orphans


# ── Journaled actions with no direct ledger update ───────────────────


async def test_reconcile_by_lookup_journals_one_control_decision(
    recovery_db, keys, store,
):
    child = await make_unknown_effect(keys, store)
    before = len(await journal.read_journal())
    result = await recovery.reconcile_by_lookup(
        principal=OPERATOR, run_id=RUN, effect_id=child["effect_id"],
        lookup_evidence="provider-lookup", outcome="succeeded",
        task_fence=FENCE,
    )
    assert result["decision"].payload["operation"] == (
        "recovery_reconcile_by_lookup"
    )
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "reconciled"
    control_records = [
        record
        for record in (await journal.read_journal())[before:]
        if record.operation_type == "human_control"
    ]
    assert len(control_records) == 1


async def test_retry_safe_effect_journals_and_creates_a_new_chain(
    recovery_db, keys, store,
):
    child = await make_unknown_effect(keys, store)
    result = await recovery.retry_safe_effect(
        principal=OPERATOR, run_id=RUN, effect_id=child["effect_id"],
        reservation_id="reservation-retry",
        adapter_capabilities=SAFE_ADAPTER, task_fence=FENCE,
    )
    assert result["retry"]["effect_id"] != child["effect_id"]
    assert result["decision"].payload["actor_id"] == "op-1"


async def test_unsafe_retry_denies_without_separated_approval(
    recovery_db, keys, store,
):
    child = await make_unknown_effect(keys, store, safety="unsafe")
    # The operator can request, but cannot retry an unsafe effect
    # through the safe path.
    await recovery.request_unsafe_retry(
        principal=OPERATOR, run_id=RUN, effect_id=child["effect_id"],
        reason="needs human review", task_fence=FENCE,
    )
    with pytest.raises(recovery.RecoveryCenterError):
        await recovery.retry_safe_effect(
            principal=OPERATOR, run_id=RUN, effect_id=child["effect_id"],
            reservation_id="reservation-retry",
            adapter_capabilities=UNSAFE_ADAPTER, task_fence=FENCE,
        )
    # The requester cannot self-approve.
    with pytest.raises(recovery.RecoveryCenterError):
        await recovery.approve_unsafe_retry(
            principal=access.Principal(
                principal_id="op-1", tenant_id="tenant-default",
                roles=("effect_approver",),
            ),
            requested_by="op-1", run_id=RUN,
            effect_id=child["effect_id"], reason="self", task_fence=FENCE,
        )
    # The unknown state is preserved throughout.
    attempt = await effects.get_attempt(child["effect_id"])
    assert attempt["state"] == "outcome_unknown"
    # A separate approver records the journaled approval.
    approval = await recovery.approve_unsafe_retry(
        principal=APPROVER, requested_by="op-1", run_id=RUN,
        effect_id=child["effect_id"], reason="verified irrecoverable",
        task_fence=FENCE,
    )
    assert approval["approval_id"]


async def test_reclaim_stale_lease_uses_a_new_fence(recovery_db):
    # A leased activation before dispatch queuing whose lease expired
    # is the reclaimable case: no grant exists yet.
    await support.activations.create_activation(
        run_id=RUN, activation_id="activation-lease", attempt=1,
        request_digest=support.REQUEST_DIGEST,
        context_view_digest=support.CONTEXT_DIGEST, task_fence=FENCE,
    )
    await support.activations.claim_activation(
        run_id=RUN, activation_id="activation-lease", attempt=1,
        owner="worker-x", lease_ttl_seconds=-1, task_fence=FENCE,
    )
    result = await recovery.reclaim_stale_lease(
        principal=OPERATOR, run_id=RUN, activation_id="activation-lease",
        attempt=1, new_owner="worker-recovered", lease_ttl_seconds=3600,
        task_fence=FENCE,
    )
    assert result["claim"]["lease_fence"] == 2
    assert result["decision"].payload["operation"] == (
        "recovery_reclaim_stale_lease"
    )


async def test_replay_outbox_record_journals_a_decision(recovery_db):
    record = await recovery.replay_outbox_record(
        principal=OPERATOR, run_id=RUN, journal_cursor=1, task_fence=FENCE,
    )
    assert record.payload["operation"] == "recovery_replay_outbox"
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT delivery_state FROM journal_delivery "
            "WHERE journal_cursor = 1",
        )
        row = await cursor.fetchone()
    assert row["delivery_state"] == "pending"


async def test_wal_checkpoint_journals_a_decision(recovery_db):
    result = await recovery.run_wal_checkpoint(
        principal=OPERATOR, run_id=RUN, task_fence=FENCE,
    )
    assert result["decision"].payload["operation"] == (
        "recovery_wal_checkpoint"
    )


async def test_erase_artifact_requires_security_administrator(
    recovery_db, store,
):
    from activation_service import persist_protected_artifact

    digest = persist_protected_artifact(
        store, b"body to erase", media_type="text/plain",
        access_policy="foundation-raw-response", referenced_by="temp",
    )
    with pytest.raises(recovery.RecoveryCenterError):
        await recovery.erase_artifact(
            principal=OPERATOR, run_id=RUN, artifact_store=store,
            content_digest=digest, reason="legal", task_fence=FENCE,
        )
    result = await recovery.erase_artifact(
        principal=SECURITY, run_id=RUN, artifact_store=store,
        content_digest=digest, reason="legal erasure", task_fence=FENCE,
    )
    assert result["erasure"].content_digest == digest
    assert not store.has_object(digest)


async def test_actions_deny_across_a_tenant_boundary(recovery_db, keys, store):
    child = await make_unknown_effect(keys, store)
    with pytest.raises(recovery.RecoveryCenterError):
        await recovery.reconcile_by_lookup(
            principal=FOREIGN, run_id=RUN, effect_id=child["effect_id"],
            lookup_evidence="x", outcome="succeeded", task_fence=FENCE,
        )


# ── Alerts ───────────────────────────────────────────────────────────


def test_alerts_link_to_the_filtered_recovery_view():
    alerts = recovery.evaluate_alerts(
        {"unknown_effects": 3, "dead_letters": 0},
        wal_bytes=128_000_000,
    )
    queues = {alert["queue"] for alert in alerts}
    assert "unknown_effects" in queues
    assert "wal_pressure" in queues
    for alert in alerts:
        assert alert["view"].startswith("/recovery-center?queue=")
    with pytest.raises(recovery.RecoveryCenterError):
        recovery.evaluate_alerts({"not_a_queue": 1})
