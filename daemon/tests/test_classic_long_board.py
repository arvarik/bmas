"""Long-board retention and resource tests for classic coordination."""

from __future__ import annotations

import json
import tracemalloc
from types import SimpleNamespace

import pytest

from core.entry import BoardEntry, entry_to_dict
from core.variants.traditional import TraditionalVariant

CONSTRAINT = "CONSTRAINT-ALPHA: Never disclose a private customer identifier."
HORIZONS = (50, 200, 500)


def _entry(
    entry_id: str,
    entry_type: str,
    body: str,
    *,
    author: str = "expert.alpha",
    refs: list[str] | None = None,
    round_no: int = 1,
    salience: float = 0.5,
) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        task_id="long-board",
        type=entry_type,
        author=author,
        title=f"{entry_type.title()} {entry_id}",
        body=body,
        refs=refs or [],
        confidence=0.9,
        salience=salience,
        round=round_no,
    )


def _large_board() -> tuple[dict[str, BoardEntry], set[str]]:
    board = {
        "objective": _entry(
            "objective",
            "objective",
            "Analyze the complete evidence set. " * 800,
            author="control_unit",
            round_no=0,
            salience=1.0,
        ),
        "constraint": _entry(
            "constraint",
            "directive",
            CONSTRAINT,
            author="operator",
            round_no=1,
            salience=1.0,
        ),
    }
    important_ids = {f"important-{index}" for index in range(5)}
    for index in range(300):
        entry_id = (
            f"important-{index}"
            if index < len(important_ids)
            else f"finding-{index}"
        )
        board[entry_id] = _entry(
            entry_id,
            "finding",
            f"Evidence marker {entry_id}. " + ("evidence " * 120),
            round_no=(index % 50) + 1,
            salience=1.0 if entry_id in important_ids else index / 1000,
        )
    for index in range(200):
        entry_id = f"artifact-{index}"
        board[entry_id] = _entry(
            entry_id,
            "artifact",
            f"Artifact {index}. " + ("artifact-data " * 60),
            author="daemon",
            round_no=(index % 50) + 1,
            salience=index / 1000,
        )
    board["solution-draft"] = _entry(
        "solution-draft",
        "solution",
        "A draft answer that cites every essential item.",
        author="decider",
        refs=sorted(important_ids),
        round_no=50,
        salience=1.0,
    )
    return board, important_ids


def _variant() -> TraditionalVariant:
    return TraditionalVariant(
        gateway=SimpleNamespace(),
        board_store=SimpleNamespace(),
        event_emitter=None,
        triage=None,
        config={"view_budget_tokens": 2048},
        litellm_url="",
        litellm_key="",
        node_endpoints=["http://node.test"],
        role_registry={},
        model_routing={"medium": "test-model"},
    )


def _visible_ids(view: dict) -> set[str]:
    return {
        str(entry["id"])
        for entry in [*view["entries"], *view["omitted_index"]]
    }


@pytest.mark.asyncio
async def test_long_board_retains_constraints_and_measures_resources():
    board, important_ids = _large_board()
    variant = _variant()
    board_bytes = len(json.dumps([entry_to_dict(entry) for entry in board.values()]))
    peak_bytes = 0

    try:
        assert len(board) == 503
        assert board_bytes > 500_000

        for horizon in HORIZONS:
            partial = dict(list(board.items())[: horizon + 2])
            for actor in (
                "planner",
                "expert.alpha",
                "critic",
                "conflict_resolver",
                "decider",
            ):
                tracemalloc.start()
                view = variant._serialize_board(partial, actor=actor)
                _, current_peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                peak_bytes = max(peak_bytes, current_peak)

                context = json.dumps(view)
                assert CONSTRAINT in context
                assert view["estimated_tokens"] <= view["token_budget"]
                assert len(context) < board_bytes

        final_view = variant._serialize_board(board, actor="decider")
        retrieval_recall = len(_visible_ids(final_view) & important_ids) / len(
            important_ids
        )
        context_bytes = len(json.dumps(final_view))

        assert retrieval_recall == 1.0
        assert context_bytes < board_bytes * 0.05
        assert peak_bytes < 16 * 1024 * 1024

        cu_view = variant._serialize_board_for_cu(board)
        assert CONSTRAINT in cu_view

        cleaner_payload = variant.build_turn_payload(
            {"task_id": "long-board", "query": "Analyze all evidence."},
            "cleaner",
            board,
        )
        assert CONSTRAINT in json.dumps(cleaner_payload["board"])
    finally:
        await variant.close()


@pytest.mark.asyncio
async def test_early_constraint_survives_a_large_objective_in_every_role_view():
    board, _ = _large_board()
    variant = _variant()
    try:
        for actor in (
            "planner",
            "expert.alpha",
            "critic",
            "conflict_resolver",
            "decider",
        ):
            payload = variant.build_turn_payload(
                {"task_id": "long-board", "query": "Full objective"},
                actor,
                board,
            )
            assert CONSTRAINT in json.dumps(payload["board"])
    finally:
        await variant.close()
