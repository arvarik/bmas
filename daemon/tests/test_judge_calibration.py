"""Judge calibration, independence, abstention, and human review panels.

A judge version calibrates against one pinned human label set. The
record exposes raw agreement, kappa only when defined, a Wilson
interval, the disagreement set, invalid-output and abstention rates,
independence from every candidate model, drift against the previous
version, and complete scorer provenance. Review panels assign blind,
keep every judgment, adjudicate ties, and never turn a tie into a
pass.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_score_record

import database as db
from benchmarks import judge_calibration
from benchmarks.judge_calibration import (
    ABSTAIN,
    JudgeCalibrationError,
    adjudicate,
    assign_reviewers,
    calibrate,
    cohen_kappa,
    judge_independence,
    judge_result_view,
    pinned_label_set,
    wilson_interval,
)

DIGEST = "b" * 64


def _labels(count: int = 12) -> dict:
    return pinned_label_set("labels-alpha", "1", [
        {"item_id": f"item-{index}",
         "label": "pass" if index % 3 else "fail",
         "reviewers": ["reviewer-a", "reviewer-b"]}
        for index in range(count)
    ])


def _calibrate(outputs: dict, **overrides) -> dict:
    arguments = {
        "judge_id": "judge-rubric",
        "judge_version": "3",
        "judge_model": "judge-model",
        "prompt_digest": DIGEST,
        "scorer_id": "scorer-rubric",
        "scorer_version": "2",
        "label_set": _labels(),
        "judge_outputs": outputs,
        "candidate_models": ["model-a", "model-b"],
        "now": "2026-09-01T00:00:00Z",
    }
    arguments.update(overrides)
    return calibrate(**arguments)


def _perfect_outputs() -> dict:
    return {
        item["item_id"]: item["label"] for item in _labels()["items"]
    }


# ── Pinned labels and statistics ─────────────────────────────────────


def test_label_set_pins_a_content_digest():
    first = _labels()
    second = _labels()
    assert first["label_digest"] == second["label_digest"]
    assert first["items"][0]["reviewers"] == ["reviewer-a", "reviewer-b"]
    with pytest.raises(JudgeCalibrationError, match="at least one"):
        pinned_label_set("labels-empty", "1", [])


def test_kappa_reports_only_when_defined():
    kappa, defined = cohen_kappa([
        ("pass", "pass"), ("fail", "fail"), ("pass", "fail"),
        ("fail", "fail"),
    ])
    assert defined is True
    assert -1.0 <= kappa <= 1.0
    # One category only: expected agreement is one, kappa undefined.
    undefined, defined = cohen_kappa([("pass", "pass"), ("pass", "pass")])
    assert undefined is None
    assert defined is False
    assert cohen_kappa([]) == (None, False)


def test_wilson_interval_bounds():
    interval = wilson_interval(9, 10)
    assert 0.0 <= interval["low"] < 0.9 < interval["high"] <= 1.0
    assert wilson_interval(0, 0) == {
        "low": 0.0, "high": 1.0, "method": "wilson",
    }


# ── Calibration records ──────────────────────────────────────────────


def test_perfect_agreement_calibrates_current():
    record = _calibrate(_perfect_outputs())
    assert record["state"] == "current"
    assert record["agreement"]["raw"] == 1.0
    assert record["agreement"]["kappa"] == 1.0
    assert record["agreement"]["kappa_defined"] is True
    assert record["disagreement"] == {"count": 0, "item_ids": []}
    assert record["independence"]["independent"] is True
    assert record["dataset"]["label_digest"] == _labels()["label_digest"]


def test_disagreement_invalid_output_and_abstention_record_separately():
    outputs = _perfect_outputs()
    outputs["item-1"] = "fail"          # disagreement
    outputs["item-2"] = ABSTAIN         # abstention
    outputs["item-4"] = "maybe"         # invalid output
    outputs["item-5"] = None            # invalid output
    record = _calibrate(outputs)
    assert record["disagreement"] == {"count": 1, "item_ids": ["item-1"]}
    assert record["abstention"] == {"count": 1, "rate": round(1 / 12, 6)}
    assert record["invalid_output"] == {"count": 2, "rate": round(2 / 12, 6)}
    # Agreement counts decided items only: 9 decided, 8 agreed.
    assert record["agreement"]["raw"] == round(8 / 9, 6)


def test_low_agreement_fails_calibration():
    outputs = {
        item["item_id"]: ("fail" if item["label"] == "pass" else "pass")
        for item in _labels()["items"]
    }
    record = _calibrate(outputs)
    assert record["state"] == "failed"
    assert record["agreement"]["raw"] == 0.0


def test_judge_independence_records_shared_models():
    dependent = judge_independence("model-a", ["model-a", "model-b"])
    assert dependent["independent"] is False
    assert "model-a" in dependent["reason"]
    derived = judge_independence(
        "judge-model", ["model-a"], prompt_derived_from_candidates=True,
    )
    assert derived["independent"] is False
    record = _calibrate(
        _perfect_outputs(), candidate_models=["judge-model", "model-b"],
    )
    assert record["independence"]["independent"] is False


def test_drift_against_the_previous_version():
    previous = _calibrate(_perfect_outputs(), judge_version="2")
    outputs = _perfect_outputs()
    for item_id in ("item-1", "item-2", "item-4"):
        outputs[item_id] = "fail" if outputs[item_id] == "pass" else "pass"
    record = _calibrate(outputs, previous=previous)
    assert record["drift"]["previous_version"] == "2"
    assert record["drift"]["raw_agreement_delta"] == round(9 / 12 - 1.0, 6)
    assert record["drift"]["exceeds_policy"] is True
    assert record["state"] == "failed"


# ── Persistence and the judge result view ────────────────────────────


@pytest_asyncio.fixture
async def calibration_db(tmp_path, monkeypatch):
    path = str(tmp_path / "calibration.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    return path


@pytest.mark.asyncio
async def test_calibration_persists_and_the_latest_reads(calibration_db):
    import aiosqlite

    first = _calibrate(_perfect_outputs(), now="2026-09-01T00:00:00Z")
    await judge_calibration.store_calibration(first)
    outputs = _perfect_outputs()
    outputs["item-1"] = "fail"
    second = _calibrate(outputs, now="2026-09-02T00:00:00Z")
    await judge_calibration.store_calibration(second)
    latest = await judge_calibration.latest_calibration("judge-rubric", "3")
    assert latest["calibration_id"] == second["calibration_id"]
    assert await judge_calibration.latest_calibration(
        "judge-rubric", "99",
    ) is None
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE judge_calibration_records SET state = 'failed' "
                "WHERE id = ?",
                (first["calibration_id"],),
            )


def test_judge_result_view_exposes_everything():
    calibration = _calibrate(_perfect_outputs())
    score = valid_score_record()
    score["judge"] = {"request_digest": DIGEST, "response_digest": DIGEST}
    score["calibration_version"] = "3"
    score["sandbox"] = {
        "boundary": "wasi_component",
        "policy_digest": DIGEST,
        "runtime_digest": DIGEST,
    }
    view = judge_result_view(score, calibration)
    assert view["calibration"]["state"] == "current"
    assert view["calibration"]["kappa_defined"] is True
    assert view["calibration"]["independent"] is True
    assert view["disagreement"] == {"count": 0, "item_ids": []}
    assert view["abstained"] is False
    provenance = view["provenance"]
    assert provenance["scorer_id"] == "scorer-exact-match"
    assert provenance["calibration_version"] == "3"
    assert provenance["judge_model"] == "judge-model"
    assert provenance["judge_request_digest"] == DIGEST
    assert provenance["sandbox_policy_digest"] == DIGEST


def test_judge_result_view_marks_abstention():
    score = valid_score_record()
    score["status"] = "error"
    score["error"] = "judge abstained: insufficient evidence"
    view = judge_result_view(score, None)
    assert view["abstained"] is True
    assert view["calibration"] is None


# ── Human review panels ──────────────────────────────────────────────


CANDIDATES = [
    {"candidate_id": "run-classic", "content": "Alpha", "runtime": "classic"},
    {"candidate_id": "run-patchboard", "content": "Beta",
     "runtime": "patchboard"},
]


def test_reviewers_receive_blind_packets_in_private_orders():
    assignment = assign_reviewers(
        candidates=CANDIDATES, reviewers=["reviewer-a", "reviewer-b"],
        seed=4,
    )
    for packet in assignment["packets"]:
        rendered = str(packet["items"])
        for hidden in ("classic", "patchboard", "run-", "runtime"):
            assert hidden not in rendered
    assert sorted(assignment["mapping"].values()) == [
        "run-classic", "run-patchboard",
    ]
    orders = [
        [item["label"] for item in packet["items"]]
        for packet in assignment["packets"]
    ]
    assert all(sorted(order) == ["candidate-1", "candidate-2"]
               for order in orders)
    with pytest.raises(JudgeCalibrationError, match="reviewers"):
        assign_reviewers(candidates=CANDIDATES, reviewers=[], seed=1)


def test_panel_keeps_every_judgment_and_reports_agreement():
    judgments = [
        {"reviewer": "reviewer-a", "passed": True},
        {"reviewer": "reviewer-b", "passed": True},
        {"reviewer": "reviewer-c", "passed": False},
    ]
    outcome = adjudicate(judgments)
    assert outcome["individual_judgments"] == judgments
    assert outcome["decision"] == "passed"
    assert outcome["resolved_by"] == "majority"
    assert outcome["raw_agreement"] == round(2 / 3, 6) or (
        abs(outcome["raw_agreement"] - 2 / 3) < 1e-9
    )
    # Kappa reports only for the defined two-rater case.
    assert outcome["kappa_defined"] is False


def test_ties_go_to_the_adjudicator_and_never_become_a_pass():
    judgments = [
        {"reviewer": "reviewer-a", "passed": True},
        {"reviewer": "reviewer-b", "passed": False},
    ]
    unresolved = adjudicate(judgments)
    assert unresolved["tie"] is True
    assert unresolved["decision"] == "tie_unresolved"
    assert unresolved["decision"] != "passed"
    resolved = adjudicate(
        judgments, adjudicator={"reviewer": "reviewer-z", "passed": False},
    )
    assert resolved["decision"] == "failed"
    assert resolved["resolved_by"] == "adjudicator:reviewer-z"
    assert resolved["kappa_defined"] is True
    with pytest.raises(JudgeCalibrationError, match="at least one"):
        adjudicate([])
