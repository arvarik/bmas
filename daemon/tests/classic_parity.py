"""The parity trace of the classic engine over the deterministic harness.

The Classic policy extraction moves engine methods into policy modules
without a behavior change. This module records what the engine does on
the deterministic lifecycle harness: the roster, every round plan, every
worker call with its prompt digest, every board event, every
checkpoint, the terminal result, and a set of samples that call the
engine API on the final board. The frozen copy lives in
``conformance/runtime_fixtures/classic-parity-trace.json``. Each parity
test recomputes its slice through the engine and through the extracted
policy and compares both with the frozen copy.

Every digest passes through the plain JSON profile, so floats become
text and the trace stays byte-stable. Volatile timestamps and random
turn identifiers never enter a digest.
"""

from __future__ import annotations

import copy
from typing import Any

from classic_harness import (
    OBJECTIVE,
    TASK_ID,
    ClassicLifecycleHarness,
    LifecycleRun,
    WorkerCall,
    harness_engine,
)

from core.digest_profile import digest_hex, plain_json
from core.entry import BoardEntry
from core.variants.traditional import (
    TraditionalVariant,
    sole_evidence_vote,
    sole_majority_vote,
)

PARITY_DOMAIN = "classic-parity"
VOLATILE_KEYS = frozenset({"created_at", "updated_at", "genesis_started_at", "turn_id"})
SAMPLE_ACTORS = ("planner", "expert.alpha", "expert.beta", "critic", "conflict_resolver", "cleaner", "decider")
SAMPLE_PRICING = {
    "priced-model": {"input_cost_per_token": 0.000002, "output_cost_per_token": 0.000004, "source": "fixture"},
}
SAMPLE_ANSWERS = [
    ("expert.alpha", "The final value is 42."),
    ("expert.beta", "The final value is 42."),
    ("critic", "The value is forty-one."),
    ("planner", "The final value equals 42 after the review of every constraint on the board."),
]
SAMPLE_EVIDENCE = [
    ("Alpha requires the final value to equal forty-two.", 0.92, 0.8),
    ("Beta accepts forty-two after reviewing alpha evidence.", 0.94, 0.7),
]


def scrub(value: Any) -> Any:
    """Drop volatile keys from a nested structure."""
    if isinstance(value, dict):
        return {
            str(key): scrub(item)
            for key, item in value.items()
            if str(key) not in VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(scrub(item) for item in value)
    return value


def digest(value: Any) -> str:
    """Digest one structure through the plain JSON profile."""
    return digest_hex(PARITY_DOMAIN, plain_json(scrub(value)))


def prompt_digest(call: WorkerCall) -> str:
    """Digest the prompt inputs of one worker call."""
    return digest({"persona": call.persona, "context": call.context})


def board_rows(snapshot: dict[str, BoardEntry]) -> list[list[Any]]:
    """The board as sorted rows with every field the policies read."""
    return sorted(
        [
            entry.type, entry.author, entry.title, entry.body, list(entry.refs),
            list(entry.sources), entry.status, entry.space, int(entry.round),
            f"{float(entry.salience):.6f}", f"{float(entry.confidence):.6f}",
        ]
        for entry in snapshot.values()
    )


def activation_rows(activations: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "actor": activation.actor,
            "role": activation.role,
            "model": activation.model,
            "node_endpoint": activation.node_endpoint,
            "profile": activation.profile,
            "activation_id": activation.activation_id,
        }
        for activation in activations
    ]


def _entry(entry_id: str, entry_type: str, author: str, body: str, *, round_no: int, refs: list[str] | None = None, sources: list[str] | None = None) -> BoardEntry:
    return BoardEntry(
        id=entry_id, task_id="task-parity-sample", type=entry_type, author=author,
        body=body, refs=list(refs or []), sources=list(sources or []), round=round_no,
        confidence=0.8, salience=0.5,
    )


def stall_sample_rounds() -> list[dict[str, BoardEntry]]:
    """Board snapshots that exercise every stall rule in order."""
    first = _entry("s-1", "finding", "expert.alpha", "The alpha constraint holds tightly.", round_no=1)
    repeat = _entry("s-2", "finding", "expert.beta", "The alpha constraint holds tightly.", round_no=2)
    paraphrase = _entry("s-3", "finding", "expert.beta", "The alpha constraint holds tightly indeed.", round_no=3)
    novel = _entry("s-4", "finding", "expert.beta", "Beta measured a completely different quantity today.", round_no=4)
    return [
        {},
        {"s-1": first},
        {"s-1": first, "s-2": repeat},
        {"s-1": first, "s-2": repeat, "s-3": paraphrase},
        {"s-1": first, "s-2": repeat, "s-3": paraphrase, "s-4": novel},
    ]


def fresh_engine(**config: Any) -> TraditionalVariant:
    """An engine with the harness configuration and no host services."""
    engine = harness_engine("sequential", None, None, None)
    for name, value in config.items():
        setattr(engine, name, value)
    return engine


async def engine_samples(harness: ClassicLifecycleHarness, run: LifecycleRun) -> dict[str, Any]:
    """Call the engine API on the final board and record every result."""
    variant = harness.variant
    snapshot = run.snapshot
    meta = run.meta
    task = {"task_id": TASK_ID, "query": OBJECTIVE}
    roster = variant.roster
    assert roster is not None
    roster_text = "\n".join(f"- {actor}: {desc}" for actor, desc in roster.all_actors())
    cu_board = variant._serialize_board_for_cu(snapshot)
    prompts: dict[str, str] = {}
    views: dict[str, str] = {}
    for actor in SAMPLE_ACTORS:
        prompts[actor] = digest(variant.build_turn_payload(task, actor, snapshot))
        views[actor] = digest(variant._serialize_board(snapshot, actor=actor))
    pinned_before = dict(variant._actor_nodes)
    activations = activation_rows(variant._to_activations(
        ["planner", "expert.alpha", "expert.beta", "critic", "decider"],
    ))
    assert dict(variant._actor_nodes) == pinned_before

    stall_engine = fresh_engine()
    stall_engine.stall_rounds = 2
    stall_states = []
    for index, board in enumerate(stall_sample_rounds()[1:], start=2):
        stalled = stall_engine._is_stalled(board, index)
        stall_states.append({
            "round": index,
            "stalled": stalled,
            "counter": stall_engine._stall_counter,
            "hashes": list(stall_engine._round_hashes),
            "token_sets": [sorted(tokens) for tokens in stall_engine._round_token_sets],
        })

    budget_engine = fresh_engine(model_pricing=dict(SAMPLE_PRICING))
    budget_engine.track_cost(0.25)
    reserved = budget_engine.reserve_activation_budgets(3)
    await budget_engine._record_llm_cost(
        "task-parity-sample", {"prompt_tokens": 1000, "completion_tokens": 500},
        "priced-model", "control_plane:cu", round_no=2,
    )
    await budget_engine._record_llm_cost(
        "task-parity-sample", {"prompt_tokens": 10, "completion_tokens": 5},
        "unpriced-model", "control_plane:ag", round_no=0,
    )
    edge_engine = fresh_engine()
    edge_engine._edge_models = ["edge-node-1", "edge-node-2"]
    edge_sequence = [edge_engine._resolve_model("local") for _ in range(3)]
    edge_sequence.append(edge_engine._resolve_model("fixed-role-model"))

    rounds_by_number: dict[int, list[BoardEntry]] = {}
    for entry in snapshot.values():
        rounds_by_number.setdefault(int(entry.round), []).append(entry)
    from core.gateway import _normalize_sources
    from core.variants.traditional import _round_lacks_evidence

    for engine in (stall_engine, budget_engine, edge_engine):
        await engine.close()

    return {
        "roster": {
            "default_experts": [expert["slug"] for expert in variant._default_experts(12)],
            "edge_sequence": edge_sequence,
            "edge_rotation": edge_engine._edge_rr_counter,
        },
        "control": {
            "fallback": {str(round_no): variant._deterministic_fallback(snapshot, round_no) for round_no in (1, 2, 5)},
            "fallback_rationale": {
                str(round_no): variant._fallback_rationale(snapshot, round_no, selected)
                for round_no, selected in ((1, ["planner"]), (3, ["critic"]), (4, ["decider"]))
            },
            "normalized": variant._normalize_selection(
                ["decider", "planner", "expert.alpha", "ghost", "planner", "critic", "expert.zeta"],
            ),
            "cu_prompt": digest(variant._cu_prompt(OBJECTIVE, cu_board, roster_text, 9, snapshot, meta)),
        },
        "scheduling": {
            "activations": activations,
            "phase": {str(round_no): variant._infer_phase(snapshot, round_no) for round_no in (1, 3, 9)},
            "activation_id": variant._activation_id(TASK_ID, 3, "critic", 0),
        },
        "board_views": {"views": views, "cu_view": digest(cu_board)},
        "memory": {
            "response_ids": dict(variant._response_ids),
            "session": {
                actor: {
                    "session_id": variant.build_turn_payload(task, actor, snapshot)["session_id"],
                    "previous_response_id": variant.build_turn_payload(task, actor, snapshot)["previous_response_id"],
                }
                for actor in ("expert.alpha", "decider")
            },
        },
        "prompts": {"payloads": prompts},
        "cleaner": {
            "eviction_candidates": [entry.id for entry in variant._get_eviction_candidates(snapshot)],
            "condense_entries": [
                item["id"] for item in variant.build_turn_payload(task, "cleaner", snapshot)["board"]["entries"]
            ],
        },
        "evidence": {
            "round_lacks_evidence": {
                str(round_no): _round_lacks_evidence(entries)
                for round_no, entries in sorted(rounds_by_number.items())
            },
            "normalized_sources": _normalize_sources([" https://a.example ", "", 7, "b" * 600]),
        },
        "verification": {
            "accepted": getattr(variant._accepted_solution(
                snapshot, reviewed_solution_id=meta.get("solution_reviewed_id"), require_review=True,
            ), "id", None),
            "accepted_unreviewed": getattr(variant._accepted_solution(snapshot), "id", None),
            "reviewed_id": meta.get("solution_reviewed_id"),
            "verification_status": meta.get("verification_status"),
        },
        "consensus": {
            "majority": {
                strategy: sole_majority_vote(SAMPLE_ANSWERS, strategy)
                for strategy in ("token_similarity", "exact")
            },
            "evidence": {
                strategy: sole_evidence_vote(SAMPLE_ANSWERS, SAMPLE_EVIDENCE, strategy)
                for strategy in ("token_similarity", "exact")
            },
        },
        "budget": {
            "reserved": [f"{share:.6f}" for share in reserved],
            "spent_after_calls": f"{budget_engine.budget_spent:.8f}",
            "control_turn_ids": [
                budget_engine._control_turn_id("task-parity-sample", 3),
                budget_engine._control_turn_id("task-parity-sample", 3),
                budget_engine._control_turn_id(None, 3),
            ],
        },
        "termination": {
            "terminal": list(variant.is_terminal(snapshot)),
            "best_finding": variant._best_finding(snapshot),
            "stall": stall_states,
            "revision_headroom": [
                fresh_engine()._revision_headroom({"budget_spent": 0.1}),
                fresh_engine()._revision_headroom({"budget_spent": 1.0}),
            ],
        },
    }


async def parity_trace(harness: ClassicLifecycleHarness, run: LifecycleRun) -> dict[str, Any]:
    """The complete parity trace of one harness run."""
    roster = run.meta.get("roster")
    return {
        "runtime_id": "classic",
        "contract_version": "1",
        "mode": run.mode,
        "roster": copy.deepcopy(roster),
        "rounds": [
            {
                "selected": plan["selected"],
                "rationale": plan["rationale"],
                "selection_source": plan["selection_source"],
                "phase": plan["phase"],
                "round_state": scrub(plan["round_state"]),
                "completed": plan.get("completed", {}),
            }
            for plan in run.round_plans
        ],
        "worker_calls": [
            {
                "actor": call.actor,
                "role": call.role,
                "model": call.model,
                "endpoint": call.endpoint,
                "profile": call.profile,
                "turn_id": call.turn_id,
                "activation_id": call.activation_id,
                "session_id": call.session_id,
                "previous_response_id": call.context.get("previous_response_id"),
                "round_no": call.round_no,
                "private": call.private,
                "prompt_digest": prompt_digest(call),
            }
            for call in run.calls
        ],
        "events": [
            {
                "seq": event["seq"],
                "event_type": event["event_type"],
                "actor": event["actor"],
                "entry_id": event.get("entry_id"),
                "turn_id": event.get("turn_id"),
                "round": event.get("round"),
                "mutation_id": (event.get("payload") or {}).get("_mutation_id"),
            }
            for event in run.events
        ],
        "board": board_rows(run.snapshot),
        "checkpoints": [digest(meta) for meta in run.checkpoint_metas],
        "final_meta_digest": digest(run.meta),
        "result": run.result,
        "samples": await engine_samples(harness, run),
    }


async def capture_parity_trace(mode: str = "sequential") -> dict[str, Any]:
    """Run the harness once and return its parity trace."""
    harness = ClassicLifecycleHarness(mode)
    run = await harness.run()
    try:
        return await parity_trace(harness, run)
    finally:
        await harness.variant.close()
