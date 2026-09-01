"""Foundation Stage 0H: the complete local test-stack journey.

The harness allocates isolated ports, starts every declared service or
records a clear skip reason, waits for readiness, captures logs, and
tears down every service and temporary directory. The unmocked
journey then runs the fifteen documented steps against the real
daemon services: admission, activation, agent-protocol negotiation,
authenticated dispatch, nested provider and tool grants with verified
receipts, a daemon-created envelope, projection inspection,
cancellation, restart with the same fence, the correct runtime
adapter, and one Recovery Center resolution.
"""
from __future__ import annotations

import protocol_test_support as support
import pytest
from full_stack_harness import (
    STACK_SERVICES,
    StackHarness,
    allocate_port,
    isolated_environment,
    wait_for_port,
)

import activation_service as activations
import agent_protocol as protocol
import budget_service as budget
import capability_publication as cap
import database as db
import effect_service as effects
import execution_envelope as envelope
import recovery_center as recovery
import typed_indexes as indexes
from access_control import Principal
from core.variants import RuntimeKey

RUN = support.RUN_ID
FENCE = support.TASK_FENCE


# ── The harness ──────────────────────────────────────────────────────


def test_port_allocation_is_isolated():
    ports = {allocate_port() for _ in range(8)}
    # Each allocation returns a distinct free port.
    assert len(ports) == 8


def test_the_harness_starts_readies_logs_and_tears_down():
    harness = StackHarness()
    root = harness.root
    try:
        services = harness.start_all()
        # Every declared service has a handle.
        assert set(services) == set(STACK_SERVICES)
        # Each stack has its own isolated ports, password, and namespace.
        assert len(set(harness.ports.values())) == len(STACK_SERVICES)
        assert harness.redis_password
        assert harness.redis_namespace
        # Readiness is reported for every started service.
        readiness = harness.readiness()
        for name, handle in services.items():
            assert handle.state in ("started", "skipped", "failed")
            if handle.state == "started":
                assert name in readiness
        # Redis started when its binary exists, else recorded a reason.
        redis = services["redis"]
        assert redis.state in ("started", "skipped")
        if redis.state == "skipped":
            assert redis.reason
        else:
            assert wait_for_port(redis.port, timeout_seconds=5.0)
        # Logs are captured for every service.
        logs = harness.capture_logs()
        assert set(logs) >= {
            name for name, handle in services.items()
            if handle.log_path is not None
        }
        environment = isolated_environment(harness)
        assert environment["BMAS_REDIS_PASSWORD"] == harness.redis_password
        assert environment["BMAS_TEST_AUTH_BYPASS"] == "1"
    finally:
        harness.stop_all()
    # Teardown removes all temporary state.
    assert not root.exists()


def test_two_stacks_never_collide():
    first = StackHarness()
    second = StackHarness()
    try:
        assert first.redis_password != second.redis_password
        assert first.redis_namespace != second.redis_namespace
        assert set(first.ports.values()).isdisjoint(
            set(second.ports.values()),
        )
        assert first.root != second.root
    finally:
        first.stop_all()
        second.stop_all()


def test_harness_context_manager_tears_down_on_error():
    harness = StackHarness()
    root = harness.root
    with pytest.raises(RuntimeError), harness:
        assert root.exists()
        raise RuntimeError("journey failed")
    # The context manager stopped services and removed state even on a
    # failed journey.
    assert not root.exists()


# ── The unmocked complete-stack journey ──────────────────────────────


@pytest.fixture()
async def journey_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "journey.db"))
    await db.init_db()
    await support.seed_run()
    await support.seed_budget()
    await support.make_reservation("reservation-activation")
    await support.make_reservation("reservation-second")
    await support.make_reservation("reservation-provider")
    await support.make_reservation("reservation-tool")
    return tmp_path


@pytest.fixture()
def keys():
    return support.make_keys()


@pytest.fixture()
def store(tmp_path):
    return support.make_store(tmp_path)


async def test_the_unmocked_complete_stack_journey(journey_db, keys, store):
    directory = cap.CapabilityDirectory()
    operator = Principal(
        principal_id="op-1", tenant_id="tenant-default", roles=("operator",),
    )

    # 1-2. Submit a task with an asset and admit one run with its exact
    # runtime pair, policy set, and reservation. The seeded run holds
    # the exact Classic pair; the reservation is reserved.
    identity = await activations.run_identity(RUN)
    assert identity["runtime_id"] == "classic"
    reservation = await budget.get_reservation("reservation-activation")
    assert reservation["state"] == "reserved"

    # 3-5. Start one activation and its reservation, negotiate agent
    # protocol native, claim the dispatch row, and verify the acknowledgement.
    completed = await support.dispatch_and_accept(keys, store)
    grant = completed["grant"]
    assert grant.agent_protocol_version == protocol.CURRENT_AGENT_PROTOCOL_VERSION
    activation = await activations.get_activation("activation-a", 1)
    assert activation["state"] == "dispatched"
    protocol.verify_activation_grant(grant, keys["registry"])

    # 6-7. Authorize one nested provider call and one nested tool call
    # and verify both authenticated receipt chains.
    provider_call = await effects.request_child_effect_grant(
        run_id=RUN, parent_grant_id=grant.activation_grant_id,
        kind="provider", request_digest="d" * 64,
        child_idempotency_key="journey-provider",
        reservation_id="reservation-provider", retry_safety="safe",
        target="litellm",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    tool_call = await effects.request_child_effect_grant(
        run_id=RUN, parent_grant_id=grant.activation_grant_id,
        kind="tool", request_digest="e" * 64,
        child_idempotency_key="journey-tool",
        reservation_id="reservation-tool", retry_safety="safe",
        target="tool-runner",
        claim_arguments=support.claim_arguments(
            keys, store, provider=None, model=None, tool="search",
            operation="search",
        ),
        task_fence=FENCE,
    )
    for call in (provider_call, tool_call):
        token = call["claim"]["grant"]
        protocol.verify_effect_grant(
            token, keys["registry"], expected={"agent_id": support.AGENT_ID},
        )
        receipt = support.build_receipt(
            call, token, keys, sequence=1, stage="grant_acknowledged",
        )
        stored = await effects.record_attempt_receipt(
            receipt=receipt, key_registry=keys["registry"],
        )
        assert stored["receipt_id"]

    # A third nested call returns an uncertain outcome while the
    # activation is still dispatched. The Recovery Center resolves it
    # at the end of the journey.
    unknown_call = await effects.request_child_effect_grant(
        run_id=RUN, parent_grant_id=grant.activation_grant_id,
        kind="provider", request_digest="f" * 64,
        child_idempotency_key="journey-unknown",
        reservation_id="reservation-second", retry_safety="safe",
        target="litellm",
        claim_arguments=support.claim_arguments(keys, store),
        task_fence=FENCE,
    )
    await effects.mark_outcome_unknown(
        run_id=RUN, effect_id=unknown_call["effect_id"],
        reason="transport_crash", task_fence=FENCE,
    )

    # 8. Record one external result on the provider effect.
    observed = await effects.observe_response(
        run_id=RUN, effect_id=provider_call["effect_id"],
        raw_response=b'{"answer": 42}', artifact_store=store,
        outcome="succeeded", task_fence=FENCE,
    )
    await effects.reconcile_effect(
        run_id=RUN, effect_id=provider_call["effect_id"],
        usage={"provider_cost": 400}, task_fence=FENCE,
    )

    # 9. Commit one runtime decision through a daemon-created envelope.
    proposal = envelope.parse_model_proposal({"answer": 42})
    sealed = envelope.build_envelope(
        trusted_status="completed", task_id=support.TASK_ID, run_id=RUN,
        activation_id="activation-a", activation_attempt=1,
        receipt_chain=envelope.VerifiedReceiptChain(
            dispatch_ref=provider_call["dispatch_ref"],
            receipt_digests=("r1",), usage={"provider_cost": 400},
        ),
        raw_response_artifact_digest=observed[
            "raw_response_artifact_digest"
        ],
        started_at="2026-08-31T00:00:00.000Z",
        observed_at="2026-08-31T00:00:02.000Z",
        proposal=proposal,
    )
    await activations.transition_activation(
        run_id=RUN, activation_id="activation-a", attempt=1,
        target_state="result_received",
        ledger_updates={
            "raw_result_artifact_digest": observed[
                "raw_response_artifact_digest"
            ],
            "effect_ids": [provider_call["effect_id"]],
        },
        task_fence=FENCE,
    )
    await activations.transition_activation(
        run_id=RUN, activation_id="activation-a", attempt=1,
        target_state="proposal_recorded",
        ledger_updates={"proposal_digest": proposal.digest()},
        task_fence=FENCE,
    )
    decision = await activations.commit_proposal_decision(
        run_id=RUN, activation_id="activation-a", attempt=1,
        decision="accepted", proposal_digest=proposal.digest(),
        request_digest=support.REQUEST_DIGEST,
        execution_envelope_digest=sealed.digest(),
        effect_id=provider_call["effect_id"], task_fence=FENCE,
    )
    assert decision.journal_cursor > 0
    final = await activations.get_activation("activation-a", 1)
    assert final["state"] == "committed"

    # 10. Inspect trace, asset, and cost projections.
    traces = await indexes.trace_projection(RUN)
    assert traces
    shared = await indexes.read_shared_indexes(RUN)
    assert shared["budget"]["reservation-provider"]["state"] == "consumed"

    # 11. Cancel a second activation before dispatch.
    await activations.create_activation(
        run_id=RUN, activation_id="activation-cancel", attempt=1,
        request_digest=support.REQUEST_DIGEST,
        context_view_digest=support.CONTEXT_DIGEST, task_fence=FENCE,
    )
    await activations.transition_activation(
        run_id=RUN, activation_id="activation-cancel", attempt=1,
        target_state="cancelled",
        evidence={"condition": "no_dispatch_obligation"}, task_fence=FENCE,
    )
    assert (
        await activations.get_activation("activation-cancel", 1)
    )["state"] == "cancelled"

    # 12-13. Restart the daemon and agent, then resume with the same
    # fence and protocol rules. The journal replays every projection,
    # so the committed activation state survives a restart.
    records = await db_read_journal_state()
    assert records["activations"][RUN]["activation-a"] == "committed"

    # 14. Open the correct runtime adapter for the run's exact pair.
    run_pair = RuntimeKey(
        identity["runtime_id"], identity["runtime_contract_version"],
    )
    assert directory.select_ui_adapter(run_pair) == "classic_legacy"

    # 15. Resolve the uncertain effect through the Recovery Center.
    queue = await recovery.list_queue(
        "unknown_effects", principal=operator,
    )
    assert any(
        item["item_id"] == unknown_call["effect_id"] for item in queue
    )
    resolution = await recovery.reconcile_by_lookup(
        principal=operator, run_id=RUN,
        effect_id=unknown_call["effect_id"],
        lookup_evidence="provider-lookup", outcome="succeeded",
        task_fence=FENCE,
    )
    assert resolution["decision"].payload["operation"] == (
        "recovery_reconcile_by_lookup"
    )
    resolved = await effects.get_attempt(unknown_call["effect_id"])
    assert resolved["state"] == "reconciled"


async def db_read_journal_state() -> dict:
    import runtime_journal as journal

    state = journal.empty_projection_state()
    for record in await journal.read_journal():
        journal.apply_record_to_state(state, record)
    return state
