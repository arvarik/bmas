"""Parity tests for the Classic policy extraction.

Each extracted policy has one test. The test runs the deterministic
lifecycle harness through the engine, which now delegates to the
policy, and compares the policy's slice of the parity trace with the
frozen trace in ``conformance/runtime_fixtures/classic-parity-trace.json``.
It then calls the extracted policy directly with the same inputs and
asserts that the direct path equals the delegated path. Any difference
fails the test, so an extraction never changes behavior unnoticed.
"""

from __future__ import annotations

import json

import pytest
from classic_harness import OBJECTIVE, TASK_ID, ClassicLifecycleHarness
from classic_parity import (
    SAMPLE_ACTORS,
    activation_rows,
    digest,
    parity_trace,
)
from runtime_fixture_capture import fixture_path

from core.variants import traditional
from core.variants.classic.control import (
    ControlLimits,
    ControlPolicy,
    ControlProgress,
    parse_cu_output,
)
from core.variants.classic.roster import (
    CONSTANT_ROLE_DESCRIPTIONS,
    FALLBACK_EXPERTS,
    RosterPolicy,
)
from core.variants.classic.scheduling import (
    SchedulingPolicy,
    activation_identity,
)


def frozen_trace() -> dict:
    return json.loads(fixture_path("classic-parity-trace").read_bytes())["record"]


@pytest.fixture(scope="module")
def lifecycle():
    """One harness run and its parity trace, shared by every parity test."""
    import asyncio

    async def run():
        harness = ClassicLifecycleHarness("sequential")
        run = await harness.run()
        trace = await parity_trace(harness, run)
        return harness, run, trace

    harness, run, trace = asyncio.run(run())
    yield harness, run, trace
    asyncio.run(harness.variant.close())


def assert_slice_equal(trace: dict, frozen: dict, *keys: str) -> None:
    for key in keys:
        assert trace[key] == frozen[key], f"parity slice {key!r} differs from the frozen trace"


# ── Pull request 1: roster, control, scheduling ──────────────────────


def test_roster_policy_parity(lifecycle):
    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert_slice_equal(trace, frozen, "roster")
    assert trace["samples"]["roster"] == frozen["samples"]["roster"]
    assert [call["model"] for call in trace["worker_calls"]] == [
        call["model"] for call in frozen["worker_calls"]
    ]
    # The direct path equals the delegated path.
    variant = harness.variant
    policy = variant.roster_policy
    assert isinstance(policy, RosterPolicy)
    assert policy.default_experts(12) == variant._default_experts(12)
    assert [expert["slug"] for expert in policy.default_experts(12)] == [
        expert["slug"] for expert in FALLBACK_EXPERTS
    ]
    assert policy.roster_to_metadata(variant.roster) == run.meta["roster"]
    restored = policy.roster_from_metadata(run.meta["roster"])
    assert restored.actor_names() == variant.roster.actor_names()
    assert restored.constants == CONSTANT_ROLE_DESCRIPTIONS
    fresh = RosterPolicy(model_routing={"medium": "local"}, edge_models=["edge-node-1", "edge-node-2"])
    assert [fresh.resolve_model("local") for _ in range(3)] == frozen["samples"]["roster"]["edge_sequence"][:3]
    assert fresh.resolve_model("fixed-role-model") == "fixed-role-model"
    assert fresh.edge_rotation == frozen["samples"]["roster"]["edge_rotation"]


def test_control_policy_parity(lifecycle):
    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert [round_["selected"] for round_ in trace["rounds"]] == [
        round_["selected"] for round_ in frozen["rounds"]
    ]
    assert [(round_["rationale"], round_["selection_source"]) for round_ in trace["rounds"]] == [
        (round_["rationale"], round_["selection_source"]) for round_ in frozen["rounds"]
    ]
    assert trace["samples"]["control"] == frozen["samples"]["control"]
    # The direct path equals the delegated path.
    variant = harness.variant
    policy = variant.control_policy
    assert isinstance(policy, ControlPolicy)
    snapshot = run.snapshot
    for round_no in (1, 2, 5):
        direct = policy.deterministic_fallback(
            snapshot, round_no, variant.roster, cleaner_threshold=variant.cleaner_threshold,
        )
        assert direct == variant._deterministic_fallback(snapshot, round_no)
        assert direct == frozen["samples"]["control"]["fallback"][str(round_no)]
    selection = ["decider", "planner", "expert.alpha", "ghost", "planner", "critic", "expert.zeta"]
    assert policy.normalize_selection(selection, variant.roster, variant.role_registry) == (
        variant._normalize_selection(selection)
    ) == frozen["samples"]["control"]["normalized"]
    roster_text = "\n".join(f"- {actor}: {desc}" for actor, desc in variant.roster.all_actors())
    board_text = variant._serialize_board_for_cu(snapshot)
    limits = ControlLimits(
        max_rounds=variant.max_rounds, max_concurrent=variant.max_concurrent,
        stall_rounds=variant.stall_rounds, max_replans=variant.max_replans,
        budget_ceiling=variant.budget_ceiling, require_evidence=variant.require_evidence,
        cleaner_threshold=variant.cleaner_threshold,
    )
    progress = ControlProgress(
        budget_spent=variant.budget_spent, stall_counter=variant._stall_counter,
        replan_count=variant._replan_count,
    )
    direct_prompt = policy.cu_prompt(
        OBJECTIVE, board_text, roster_text, 9, snapshot, run.meta,
        limits=limits, progress=progress,
    )
    assert direct_prompt == variant._cu_prompt(OBJECTIVE, board_text, roster_text, 9, snapshot, run.meta)
    assert digest(direct_prompt) == frozen["samples"]["control"]["cu_prompt"]
    assert policy.fallback_rationale(snapshot, 3, ["critic"]) == variant._fallback_rationale(snapshot, 3, ["critic"])
    assert parse_cu_output('{"selected": ["critic", "ghost"], "rationale": "review"}', variant.roster.actor_names()) == (
        ["critic"], "review",
    )
    assert traditional.parse_cu_output is parse_cu_output


@pytest.mark.asyncio
async def test_control_policy_reads_live_limits(lifecycle):
    """A limit changed after construction reaches the next decision."""
    harness, run, trace = lifecycle
    variant = harness.variant
    before = variant._cu_prompt(OBJECTIVE, "(board)", "(roster)", 2, run.snapshot, run.meta)
    original = variant.max_rounds
    variant.max_rounds = original + 5
    try:
        after = variant._cu_prompt(OBJECTIVE, "(board)", "(roster)", 2, run.snapshot, run.meta)
    finally:
        variant.max_rounds = original
    assert f"Round: 2/{original}" in before
    assert f"Round: 2/{original + 5}" in after


def test_scheduling_policy_parity(lifecycle):
    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert [round_["round_state"] for round_ in trace["rounds"]] == [
        round_["round_state"] for round_ in frozen["rounds"]
    ]
    assert [round_["phase"] for round_ in trace["rounds"]] == [round_["phase"] for round_ in frozen["rounds"]]
    assert [round_["completed"] for round_ in trace["rounds"]] == [
        round_["completed"] for round_ in frozen["rounds"]
    ]
    assert [(call["endpoint"], call["activation_id"]) for call in trace["worker_calls"]] == [
        (call["endpoint"], call["activation_id"]) for call in frozen["worker_calls"]
    ]
    assert trace["samples"]["scheduling"] == frozen["samples"]["scheduling"]
    # The direct path equals the delegated path.
    variant = harness.variant
    policy = variant.scheduling_policy
    assert isinstance(policy, SchedulingPolicy)
    selected = ["planner", "expert.alpha", "expert.beta", "critic", "decider"]
    pins = dict(policy.actor_nodes)
    direct = activation_rows(policy.to_activations(
        selected, roster=variant.roster, tier=variant._tier,
        role_registry=variant.role_registry, node_endpoints=variant.node_endpoints,
        model_routing=variant.model_routing, resolve_model=variant._resolve_model,
    ))
    assert direct == activation_rows(variant._to_activations(selected))
    assert direct == frozen["samples"]["scheduling"]["activations"]
    assert dict(policy.actor_nodes) == pins == dict(variant._actor_nodes)
    for round_no in (1, 3, 9):
        assert policy.infer_phase(run.snapshot, round_no) == variant._infer_phase(run.snapshot, round_no)
    assert policy.isolate_decider(["expert.alpha", "decider", "critic"]) == (["expert.alpha", "critic"], True)
    assert policy.isolate_decider(["decider"]) == (["decider"], False)
    assert policy.clamp(["a", "b", "c", "d"], 3) == ["a", "b", "c"]
    assert activation_identity(TASK_ID, 3, "critic", 0) == variant._activation_id(TASK_ID, 3, "critic", 0)
    assert activation_identity(TASK_ID, 3, "critic", 0) == frozen["samples"]["scheduling"]["activation_id"]
    # The recorded active state rebuilds the same plan the engine ran,
    # and a state with every activation completed rebuilds nothing.
    active_state = run.round_plans[0]["round_state"]
    plan = SchedulingPolicy.plan_from_state(active_state, run.meta)
    assert plan is not None
    assert activation_rows(plan.activations) == active_state["activations"]
    assert plan.selected == run.round_plans[0]["selected"]
    closed_state = {**active_state, "completed": dict(run.round_plans[0]["completed"])}
    closed = SchedulingPolicy.plan_from_state(closed_state, run.meta)
    assert closed is not None and closed.activations == []
    assert SchedulingPolicy.plan_from_state({**active_state, "status": "completed"}, run.meta) is None
    assert [call["session_id"] for call in trace["worker_calls"]] == [
        call["session_id"] for call in frozen["worker_calls"]
    ]
    assert trace["events"] == frozen["events"]
    assert trace["board"] == frozen["board"]
    assert trace["checkpoints"] == frozen["checkpoints"]
    assert trace["result"] == frozen["result"]
    assert [call["prompt_digest"] for call in trace["worker_calls"]] == [
        call["prompt_digest"] for call in frozen["worker_calls"]
    ]


def test_every_sample_actor_has_a_prompt(lifecycle):
    _harness, _run, trace = lifecycle
    assert set(trace["samples"]["prompts"]["payloads"]) == set(SAMPLE_ACTORS)
