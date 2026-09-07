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


# ── Pull request 2: board views, sessions, prompts ───────────────────


def test_board_view_policy_parity(lifecycle):
    from core.variants.classic.board_views import BoardViewPolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["board_views"] == frozen["samples"]["board_views"]
    variant = harness.variant
    policy = variant.board_view_policy
    assert isinstance(policy, BoardViewPolicy)
    for actor in SAMPLE_ACTORS:
        direct = policy.serialize_board(run.snapshot, actor, view_budget_tokens=variant.view_budget_tokens)
        assert direct == variant._serialize_board(run.snapshot, actor=actor)
        assert digest(direct) == frozen["samples"]["board_views"]["views"][actor]
    cu_view = policy.serialize_for_cu(run.snapshot, view_budget_tokens=variant.view_budget_tokens)
    assert cu_view == variant._serialize_board_for_cu(run.snapshot)
    assert digest(cu_view) == frozen["samples"]["board_views"]["cu_view"]
    # A budget changed after construction reaches the next view.
    narrow = policy.serialize_board(run.snapshot, "decider", view_budget_tokens=600)
    assert narrow["token_budget"] == 600
    assert narrow["estimated_tokens"] <= 600


def test_context_session_policy_parity(lifecycle):
    from core.variants.classic.memory import ContextSessionPolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["memory"] == frozen["samples"]["memory"]
    assert [(call["session_id"], call["previous_response_id"]) for call in trace["worker_calls"]] == [
        (call["session_id"], call["previous_response_id"]) for call in frozen["worker_calls"]
    ]
    variant = harness.variant
    policy = variant.session_policy
    assert isinstance(policy, ContextSessionPolicy)
    assert policy.response_ids is variant._response_ids
    assert dict(policy.response_ids) == run.meta["response_ids"]
    for actor in ("expert.alpha", "decider"):
        chained = policy.session_fields(TASK_ID, actor, actor_context="chained")
        assert chained == {
            "session_id": f"{TASK_ID}:{actor}",
            "previous_response_id": variant.get_response_id(actor),
        }
        assert chained == frozen["samples"]["memory"]["session"][actor]
        fresh = policy.session_fields(TASK_ID, actor, actor_context="fresh")
        assert fresh["previous_response_id"] is None
    # The mutation path writes through both ways. Restore the recorded
    # identity afterwards, because later parity tests share this engine.
    original = policy.get_response_id("critic")
    assert original == run.meta["response_ids"]["critic"]
    policy.set_response_id("critic", "response-parity")
    assert variant.get_response_id("critic") == "response-parity"
    variant.clear_response_id("critic")
    assert policy.get_response_id("critic") is None
    variant.set_response_id("critic", original)
    assert policy.get_response_id("critic") == original


def test_prompt_policy_parity(lifecycle):
    from core.variants.classic.prompts import PromptPolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["prompts"] == frozen["samples"]["prompts"]
    assert [call["prompt_digest"] for call in trace["worker_calls"]] == [
        call["prompt_digest"] for call in frozen["worker_calls"]
    ]
    variant = harness.variant
    policy = variant.prompt_policy
    assert isinstance(policy, PromptPolicy)
    task = {"task_id": TASK_ID, "query": OBJECTIVE}
    for actor in SAMPLE_ACTORS:
        delegated = variant.build_turn_payload(task, actor, run.snapshot)
        role_prompt = policy.role_prompt(actor, variant.roster, OBJECTIVE)
        assert role_prompt == delegated["role_prompt"]
        direct = policy.render(
            task_id=TASK_ID, query=OBJECTIVE, actor=actor, role_prompt=role_prompt,
            board_data=delegated["board"], round_no=0,
            session=variant.session_policy.session_fields(TASK_ID, actor, actor_context=variant.actor_context),
            budget_remaining_usd=max(0, variant.budget_ceiling - variant.budget_spent),
            budget_ceiling=variant.budget_ceiling, budget_spent=variant.budget_spent,
            require_evidence=variant.require_evidence,
        )
        assert digest(direct) == digest(delegated) == frozen["samples"]["prompts"]["payloads"][actor]
        assert direct["turn_id"] != delegated["turn_id"]  # each render names a fresh turn
    assert policy.role_prompt("expert.unknown", variant.roster, OBJECTIVE) == policy.role_prompt("expert", None, OBJECTIVE)


# ── Pull request 3: cleaner, evidence, verification ──────────────────


def test_cleaner_policy_parity(lifecycle):
    from core.variants.classic.cleaner import CleanerPolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["cleaner"] == frozen["samples"]["cleaner"]
    cleaner_calls = [call for call in trace["worker_calls"] if call["actor"] == "cleaner"]
    frozen_cleaner_calls = [call for call in frozen["worker_calls"] if call["actor"] == "cleaner"]
    assert cleaner_calls and [call["prompt_digest"] for call in cleaner_calls] == [
        call["prompt_digest"] for call in frozen_cleaner_calls
    ]
    variant = harness.variant
    policy = variant.cleaner_policy
    assert isinstance(policy, CleanerPolicy)
    direct = policy.eviction_candidates(run.snapshot, retention_weights=variant.cleaner_retention_weights)
    assert [entry.id for entry in direct] == [entry.id for entry in variant._get_eviction_candidates(run.snapshot)]
    assert [entry.id for entry in direct] == frozen["samples"]["cleaner"]["eviction_candidates"]
    view = policy.condense_view(run.snapshot, direct)
    task = {"task_id": TASK_ID, "query": OBJECTIVE}
    assert view == variant.build_turn_payload(task, "cleaner", run.snapshot)["board"]
    assert [item["id"] for item in view["entries"]] == frozen["samples"]["cleaner"]["condense_entries"]
    open_entries = [entry for entry in run.snapshot.values() if entry.status == "open"]
    assert policy.board_tokens(open_entries) == sum(len(entry.body) // 4 for entry in open_entries)
    assert "forced cleaner invocation" in policy.pressure_rationale(9000, 8000)


def test_evidence_policy_parity(lifecycle):
    from core.gateway import _normalize_sources
    from core.variants.classic.evidence import EvidencePolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["evidence"] == frozen["samples"]["evidence"]
    assert [row[5] for row in trace["board"]] == [row[5] for row in frozen["board"]]  # sources
    variant = harness.variant
    policy = variant.evidence_policy
    assert isinstance(policy, EvidencePolicy)
    rounds: dict[int, list] = {}
    for entry in run.snapshot.values():
        rounds.setdefault(int(entry.round), []).append(entry)
    for round_no, entries in sorted(rounds.items()):
        assert policy.round_lacks_evidence(entries) == traditional._round_lacks_evidence(entries)
        assert policy.round_lacks_evidence(entries) == frozen["samples"]["evidence"]["round_lacks_evidence"][str(round_no)]
    sample = [" https://a.example ", "", 7, "b" * 600]
    assert policy.normalize_sources(sample) == _normalize_sources(sample)
    assert policy.normalize_sources(sample) == frozen["samples"]["evidence"]["normalized_sources"]
    assert policy.normalize_sources("single") == ["single"]
    assert policy.normalize_sources(None) == []


def test_verification_policy_parity(lifecycle):
    from core.variants.classic.verification import VerificationPolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["verification"] == frozen["samples"]["verification"]
    assert trace["result"] == frozen["result"]
    assert [row[6] for row in trace["board"]] == [row[6] for row in frozen["board"]]  # statuses
    variant = harness.variant
    policy = variant.verification_policy
    assert isinstance(policy, VerificationPolicy)
    reviewed_id = run.meta.get("solution_reviewed_id")
    direct = policy.accepted_solution(run.snapshot, reviewed_solution_id=reviewed_id, require_review=True)
    delegated = variant._accepted_solution(run.snapshot, reviewed_solution_id=reviewed_id, require_review=True)
    assert direct is delegated
    assert getattr(direct, "id", None) == frozen["samples"]["verification"]["accepted"]
    assert getattr(policy.accepted_solution(run.snapshot), "id", None) == frozen["samples"]["verification"]["accepted_unreviewed"]
    resolved = policy.resolve_answer(run.snapshot, reviewed_id)
    assert resolved is not None
    assert (resolved.answer, resolved.answer_source, resolved.verification_status) == (
        run.result["answer"], run.result["answer_source"], run.result["verification_status"],
    )
    assert policy.resolve_answer({}, None) is None
    approval = policy.approval_entry("e-9", "mutation-1")
    assert approval["refs"] == ["e-9"] and approval["_mutation_id"] == "mutation-1:approval"
    # The grace plan of a finished run owes nothing.
    plan = policy.grace_plan(
        run.snapshot, run.meta, grace_verification=True, critic_enabled=True,
        within_overrun=lambda: True, revision_headroom=lambda: True,
    )
    assert plan.candidate is None and plan.revision is False
    forced = {**run.meta, "decider_forced": True, "solution_reviewed_id": None, "solution_candidate_id": None}
    plan = policy.grace_plan(
        run.snapshot, forced, grace_verification=True, critic_enabled=True,
        within_overrun=lambda: True, revision_headroom=lambda: True,
    )
    assert plan.candidate is not None and plan.candidate.type == "solution"


# ── Pull request 4: consensus, budget, termination ───────────────────


def test_consensus_policy_parity(lifecycle):
    from classic_parity import SAMPLE_ANSWERS, SAMPLE_EVIDENCE

    from core.variants.classic.consensus import ConsensusPolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["consensus"] == frozen["samples"]["consensus"]
    policy = harness.variant.consensus_policy
    assert isinstance(policy, ConsensusPolicy)
    for strategy in ("token_similarity", "exact", "auto"):
        canonical = "token_similarity" if strategy == "auto" else strategy
        assert policy.majority_vote(SAMPLE_ANSWERS, strategy) == traditional.sole_majority_vote(SAMPLE_ANSWERS, strategy)
        assert policy.majority_vote(SAMPLE_ANSWERS, strategy) == frozen["samples"]["consensus"]["majority"][canonical]
        assert policy.evidence_vote(SAMPLE_ANSWERS, SAMPLE_EVIDENCE, strategy) == (
            traditional.sole_evidence_vote(SAMPLE_ANSWERS, SAMPLE_EVIDENCE, strategy)
        ) == frozen["samples"]["consensus"]["evidence"][canonical]
    assert traditional.sole_majority_vote is ConsensusPolicy.majority_vote
    assert traditional._fuzzy_similarity("the final value", "the final answer") == pytest.approx(2 / 4)
    assert traditional._exact_similarity("Value: 42!", "value 42") == 1.0
    with pytest.raises(ValueError):
        policy.majority_vote(SAMPLE_ANSWERS, "judge")


@pytest.mark.asyncio
async def test_budget_policy_parity(lifecycle):
    from classic_parity import SAMPLE_PRICING, fresh_engine

    from core.variants.classic.budget import BudgetPolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["budget"] == frozen["samples"]["budget"]
    assert trace["result"]["budget_spent"] == frozen["result"]["budget_spent"]
    assert [call["cost_usd"] for call in [c.__dict__ for c in run.calls]] == [
        call.cost_usd for call in run.calls
    ]
    variant = harness.variant
    policy = variant.budget_policy
    assert isinstance(policy, BudgetPolicy)
    assert policy.spent == variant.budget_spent == run.result["budget_spent"]
    assert policy.ceiling == variant.budget_ceiling
    # The direct path equals the delegated path on a fresh engine.
    engine = fresh_engine(model_pricing=dict(SAMPLE_PRICING))
    direct = BudgetPolicy(ceiling=engine.budget_ceiling)
    engine.track_cost(0.25)
    direct.track_cost(0.25)
    assert engine.reserve_activation_budgets(3) == direct.reserve_activation_budgets(3)
    assert [f"{share:.6f}" for share in direct.reserve_activation_budgets(3)] == frozen["samples"]["budget"]["reserved"]
    priced = direct.price_usage({"prompt_tokens": 1000, "completion_tokens": 500}, "priced-model", SAMPLE_PRICING)
    assert priced is not None and priced.price_source == "fixture"
    unpriced = direct.price_usage({"prompt_tokens": 10, "completion_tokens": 5}, "unpriced-model", SAMPLE_PRICING)
    assert unpriced is not None and unpriced.cost_usd == 0.0 and unpriced.price_source == "missing"
    assert direct.price_usage({"prompt_tokens": 0, "completion_tokens": 0}, "priced-model", SAMPLE_PRICING) is None
    await engine._record_llm_cost(
        "task-parity-sample", {"prompt_tokens": 1000, "completion_tokens": 500},
        "priced-model", "control_plane:cu", round_no=2,
    )
    await engine._record_llm_cost(
        "task-parity-sample", {"prompt_tokens": 10, "completion_tokens": 5},
        "unpriced-model", "control_plane:ag", round_no=0,
    )
    direct.track_cost(priced.cost_usd)
    direct.track_cost(unpriced.cost_usd)
    assert f"{engine.budget_spent:.8f}" == f"{direct.spent:.8f}" == frozen["samples"]["budget"]["spent_after_calls"]
    assert [
        engine._control_turn_id("task-parity-sample", 3),
        engine._control_turn_id("task-parity-sample", 3),
        engine._control_turn_id(None, 3),
    ] == frozen["samples"]["budget"]["control_turn_ids"]
    assert engine.budget_policy.budget_event()["spent"] == round(engine.budget_spent, 6)
    await engine.close()


@pytest.mark.asyncio
async def test_termination_policy_parity(lifecycle):
    from classic_parity import fresh_engine, stall_sample_rounds

    from core.variants.classic.termination import TerminationPolicy

    harness, run, trace = lifecycle
    frozen = frozen_trace()
    assert trace["samples"]["termination"] == frozen["samples"]["termination"]
    assert trace["result"]["terminated_by"] == frozen["result"]["terminated_by"]
    assert trace["checkpoints"] == frozen["checkpoints"]
    variant = harness.variant
    policy = variant.termination_policy
    assert isinstance(policy, TerminationPolicy)
    assert list(policy.is_terminal(run.snapshot, variant._accepted_solution)) == list(variant.is_terminal(run.snapshot))
    assert list(variant.is_terminal(run.snapshot)) == frozen["samples"]["termination"]["terminal"]
    assert policy.best_finding(run.snapshot) == variant._best_finding(run.snapshot) == frozen["samples"]["termination"]["best_finding"]
    # The stall detector: the delegated engine and a direct policy agree.
    engine = fresh_engine()
    engine.stall_rounds = 2
    direct = TerminationPolicy()
    observed = []
    for index, board in enumerate(stall_sample_rounds()[1:], start=2):
        delegated = engine._is_stalled(board, index)
        straight = direct.is_stalled(
            board, index, stall_rounds=2, require_evidence=False,
            round_lacks_evidence=engine.evidence_policy.round_lacks_evidence,
        )
        assert delegated == straight
        assert (engine._stall_counter, engine._round_hashes, engine._round_token_sets) == (
            direct.stall_counter, direct.round_hashes, direct.round_token_sets,
        )
        observed.append({
            "round": index, "stalled": delegated, "counter": engine._stall_counter,
            "hashes": list(engine._round_hashes),
            "token_sets": [sorted(tokens) for tokens in engine._round_token_sets],
        })
    assert observed == frozen["samples"]["termination"]["stall"]
    # The state properties write through to the policy.
    engine._stall_counter = 7
    engine._round_hashes = ["abc"]
    assert engine.termination_policy.stall_counter == 7 and engine.termination_policy.round_hashes == ["abc"]
    assert [
        fresh_engine()._revision_headroom({"budget_spent": 0.1}),
        fresh_engine()._revision_headroom({"budget_spent": 1.0}),
    ] == frozen["samples"]["termination"]["revision_headroom"]
    assert policy.revision_headroom({"budget_spent": 1.0}, budget_ceiling=1.0, elapsed_s=0.0, max_duration_s=600) is False
    assert policy.revision_headroom({"budget_spent": 0.1}, budget_ceiling=1.0, elapsed_s=0.0, max_duration_s=600) is True
    verdict = policy.limit_verdict(
        current_round=11, max_rounds=10, budget_spent=0.0, budget_ceiling=1.0,
        elapsed_s=0.0, duration_limit_s=600.0, stalled=lambda: False,
        stall_counter=lambda: 0, stall_rounds=2, replan_count=0, max_replans=2,
    )
    assert (verdict.force_decider, verdict.term_reason, verdict.force_replan) == (True, "max_rounds", False)
    stalled = policy.limit_verdict(
        current_round=3, max_rounds=10, budget_spent=0.0, budget_ceiling=1.0,
        elapsed_s=0.0, duration_limit_s=600.0, stalled=lambda: True,
        stall_counter=lambda: 2, stall_rounds=2, replan_count=0, max_replans=2,
    )
    assert (stalled.force_replan, stalled.force_decider) == (True, False)
    engine.note_turn_duration(5)
    assert engine._turn_durations == engine.termination_policy.turn_durations == [0.005]
    assert engine.closing_turn_timeout_s() == 120
    await engine.close()
