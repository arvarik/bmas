"""Judge calibration against pinned human labels.

Every judge version calibrates against one pinned human label set
before its scores count. The calibration records raw agreement, kappa
only when its calculation defines it, a Wilson uncertainty interval,
the disagreement set, invalid-output and abstention rates, judge
independence from every candidate model, and drift against the
previous calibration version. Human review panels assign reviewers
blind to runtime, model, and candidate order, keep every individual
judgment, send ties to one adjudicator, and never convert a tie into
a pass. A judge result exposes its calibration, disagreement,
abstention, and complete scorer provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any

import database as db
from benchmarks.evaluation_contracts import validate_record
from benchmarks.provenance import content_checksum

CALIBRATION_METHOD = "raw-agreement-cohen-kappa"
DEFAULT_AGREEMENT_THRESHOLD = 0.7
DEFAULT_DRIFT_TOLERANCE = 0.1
ABSTAIN = "abstain"


class JudgeCalibrationError(ValueError):
    """The calibration request violates the calibration contract."""


# ── Pinned labels and independence ───────────────────────────────────


def pinned_label_set(
    dataset_id: str, version: str, items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pin one human label set with its content digest."""
    if not items:
        raise JudgeCalibrationError("A label set holds at least one item")
    pinned = sorted(
        (
            {
                "item_id": str(item["item_id"]),
                "label": str(item["label"]),
                "reviewers": sorted(str(r) for r in item.get("reviewers") or []),
            }
            for item in items
        ),
        key=lambda item: str(item["item_id"]),
    )
    return {
        "dataset_id": dataset_id,
        "version": version,
        "items": pinned,
        "label_digest": content_checksum(pinned),
    }


def judge_independence(
    judge_model: str,
    candidate_models: list[str],
    *,
    prompt_derived_from_candidates: bool = False,
) -> dict[str, Any]:
    """Record whether the judge is independent of every candidate."""
    shared = sorted(
        model for model in candidate_models if model == judge_model
    )
    if shared:
        return {
            "independent": False,
            "candidate_models": sorted(candidate_models),
            "reason": (
                "the judge model also produced candidate output: "
                + ", ".join(shared)
            ),
        }
    if prompt_derived_from_candidates:
        return {
            "independent": False,
            "candidate_models": sorted(candidate_models),
            "reason": "candidate content shaped the judge prompt",
        }
    return {
        "independent": True,
        "candidate_models": sorted(candidate_models),
        "reason": "the judge model differs from every candidate model",
    }


# ── Agreement statistics ─────────────────────────────────────────────


def cohen_kappa(pairs: list[tuple[str, str]]) -> tuple[float | None, bool]:
    """Compute Cohen's kappa, or report that it is undefined.

    Kappa is undefined when expected agreement equals one, which
    happens with a single label category or an empty set.
    """
    if not pairs:
        return None, False
    total = len(pairs)
    observed = sum(1 for left, right in pairs if left == right) / total
    categories = {label for pair in pairs for label in pair}
    expected = 0.0
    for category in categories:
        left_rate = sum(1 for left, _ in pairs if left == category) / total
        right_rate = sum(1 for _, right in pairs if right == category) / total
        expected += left_rate * right_rate
    if math.isclose(expected, 1.0):
        return None, False
    return (observed - expected) / (1.0 - expected), True


def wilson_interval(successes: int, total: int) -> dict[str, Any]:
    """Return one Wilson score interval for a proportion."""
    if total <= 0:
        return {"low": 0.0, "high": 1.0, "method": "wilson"}
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total),
    ) / denominator
    return {
        "low": round(max(0.0, centre - spread), 6),
        "high": round(min(1.0, centre + spread), 6),
        "method": "wilson",
    }


# ── Calibration ──────────────────────────────────────────────────────


def calibrate(
    *,
    judge_id: str,
    judge_version: str,
    judge_model: str,
    prompt_digest: str,
    scorer_id: str,
    scorer_version: str,
    label_set: dict[str, Any],
    judge_outputs: dict[str, Any],
    candidate_models: list[str],
    previous: dict[str, Any] | None = None,
    threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
    drift_tolerance: float = DEFAULT_DRIFT_TOLERANCE,
    now: str = "1970-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Calibrate one judge version against one pinned label set.

    Agreement counts only the items the judge decided with a valid
    label. Abstentions and invalid outputs stay visible as separate
    rates and never count as agreement.
    """
    labels = {item["item_id"]: item["label"] for item in label_set["items"]}
    vocabulary = set(labels.values())
    pairs: list[tuple[str, str]] = []
    disagreements: list[str] = []
    invalid = 0
    abstained = 0
    for item_id, human in sorted(labels.items()):
        output = judge_outputs.get(item_id)
        if output == ABSTAIN:
            abstained += 1
            continue
        if not isinstance(output, str) or output not in vocabulary:
            invalid += 1
            continue
        pairs.append((human, output))
        if human != output:
            disagreements.append(item_id)
    decided = len(pairs)
    agreed = decided - len(disagreements)
    raw = agreed / decided if decided else 0.0
    kappa, defined = cohen_kappa(pairs)
    total = len(labels)
    previous_raw = None
    previous_version = None
    if previous is not None:
        previous_raw = float(previous["agreement"]["raw"])
        previous_version = str(previous["judge"]["version"])
    delta = round(raw - previous_raw, 6) if previous_raw is not None else None
    exceeds = delta is not None and abs(delta) > drift_tolerance
    record = {
        "schema_id": "judge-calibration-record",
        "schema_version": 2,
        "calibration_id": f"calibration-{uuid.uuid4().hex}",
        "judge": {
            "judge_id": judge_id,
            "version": judge_version,
            "model": judge_model,
            "prompt_digest": prompt_digest,
        },
        "scorer": {"scorer_id": scorer_id, "version": scorer_version},
        "dataset": {
            "dataset_id": str(label_set["dataset_id"]),
            "version": str(label_set["version"]),
            "label_digest": str(label_set["label_digest"]),
            "item_count": total,
        },
        "independence": judge_independence(judge_model, candidate_models),
        "agreement": {
            "raw": round(raw, 6),
            "kappa": round(kappa, 6) if kappa is not None else None,
            "kappa_defined": defined,
            "interval": wilson_interval(agreed, decided),
        },
        "disagreement": {
            "count": len(disagreements),
            "item_ids": disagreements,
        },
        "invalid_output": {
            "count": invalid,
            "rate": round(invalid / total, 6) if total else 0.0,
        },
        "abstention": {
            "count": abstained,
            "rate": round(abstained / total, 6) if total else 0.0,
        },
        "drift": {
            "previous_version": previous_version,
            "raw_agreement_delta": delta,
            "exceeds_policy": exceeds,
        },
        "state": (
            "current"
            if decided and raw >= threshold and not exceeds
            else "failed"
        ),
        "threshold": threshold,
        "calibrated_at": now,
    }
    validate_record(record)
    return record


async def store_calibration(record: dict[str, Any]) -> dict[str, Any]:
    """Store one calibration through the one facade."""
    from benchmarks import facade

    saved = await facade.execute(
        "record_judge_calibration", {"record": record},
    )
    return {"calibration_id": saved["id"], "record": record}


async def latest_calibration(
    judge_id: str, judge_version: str,
) -> dict[str, Any] | None:
    """Read the latest stored calibration for one judge version."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT record FROM judge_calibration_records "
            "WHERE judge_id = ? AND judge_version = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (judge_id, judge_version),
        )
        row = await cursor.fetchone()
    return json.loads(row["record"]) if row else None


def judge_result_view(
    score_record: dict[str, Any],
    calibration: dict[str, Any] | None,
) -> dict[str, Any]:
    """Expose calibration, disagreement, abstention, and provenance."""
    scorer = score_record.get("scorer") or {}
    sandbox = score_record.get("sandbox") or {}
    judge = score_record.get("judge") or {}
    abstained = (
        score_record.get("status") == "error"
        and "abstain" in str(score_record.get("error") or "").lower()
    )
    return {
        "score_id": score_record.get("score_id"),
        "status": score_record.get("status"),
        "abstained": abstained,
        "calibration": (
            None if calibration is None else {
                "calibration_id": calibration["calibration_id"],
                "state": calibration["state"],
                "raw_agreement": calibration["agreement"]["raw"],
                "kappa": calibration["agreement"]["kappa"],
                "kappa_defined": calibration["agreement"]["kappa_defined"],
                "interval": calibration["agreement"]["interval"],
                "abstention_rate": calibration["abstention"]["rate"],
                "invalid_output_rate": calibration["invalid_output"]["rate"],
                "independent": calibration["independence"]["independent"],
                "drift": calibration["drift"],
            }
        ),
        "disagreement": (
            None if calibration is None else calibration["disagreement"]
        ),
        "provenance": {
            "scorer_id": scorer.get("scorer_id"),
            "scorer_version": scorer.get("version"),
            "configuration_digest": scorer.get("configuration_digest"),
            "calibration_version": score_record.get("calibration_version"),
            "judge_request_digest": judge.get("request_digest"),
            "judge_response_digest": judge.get("response_digest"),
            "judge_model": (
                None if calibration is None
                else calibration["judge"]["model"]
            ),
            "judge_prompt_digest": (
                None if calibration is None
                else calibration["judge"]["prompt_digest"]
            ),
            "sandbox_policy_digest": sandbox.get("policy_digest"),
            "sandbox_runtime_digest": sandbox.get("runtime_digest"),
        },
    }


# ── Human review panels ──────────────────────────────────────────────


def assign_reviewers(
    *,
    candidates: list[dict[str, Any]],
    reviewers: list[str],
    seed: int,
) -> dict[str, Any]:
    """Assign reviewers blind to runtime, model, and candidate order.

    Every reviewer sees neutral labels in a private permutation, and
    the mapping back to real candidates stays outside the packets.
    """
    if not reviewers:
        raise JudgeCalibrationError("A review panel names its reviewers")
    packets = []
    mapping: dict[str, str] = {}
    for reviewer in reviewers:
        order = sorted(
            range(len(candidates)),
            key=lambda index: hashlib.sha256(
                f"review-order:{seed}:{reviewer}:{index}".encode(),
            ).digest(),
        )
        items = []
        for index in order:
            label = f"candidate-{index + 1}"
            mapping[label] = str(candidates[index].get("candidate_id") or "")
            items.append({
                "label": label,
                "content": str(candidates[index].get("content") or ""),
            })
        packets.append({"reviewer": reviewer, "items": items})
    return {"packets": packets, "mapping": mapping}


def adjudicate(
    judgments: list[dict[str, Any]],
    *,
    adjudicator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one panel with every judgment kept and no tie-to-pass.

    A tie goes to the adjudicator. Without an adjudicator the tie
    stays unresolved, and an unresolved tie is never a pass.
    """
    if not judgments:
        raise JudgeCalibrationError("A panel records at least one judgment")
    passes = sum(1 for judgment in judgments if judgment.get("passed"))
    fails = len(judgments) - passes
    tie = passes == fails
    if not tie:
        decision = "passed" if passes > fails else "failed"
        resolved_by = "majority"
    elif adjudicator is not None:
        decision = "passed" if adjudicator.get("passed") else "failed"
        resolved_by = f"adjudicator:{adjudicator.get('reviewer')}"
    else:
        decision = "tie_unresolved"
        resolved_by = "none"
    majority = passes > fails if not tie else (
        bool(adjudicator.get("passed")) if adjudicator else None
    )
    agreements = sum(
        1 for judgment in judgments if bool(judgment.get("passed")) == majority
    ) if majority is not None else 0
    raw_agreement = agreements / len(judgments) if majority is not None else None
    kappa = None
    kappa_defined = False
    if len(judgments) == 2:
        pairs = [(
            "pass" if judgments[0].get("passed") else "fail",
            "pass" if judgments[1].get("passed") else "fail",
        )]
        kappa, kappa_defined = cohen_kappa(pairs)
    return {
        "individual_judgments": list(judgments),
        "passes": passes,
        "fails": fails,
        "tie": tie,
        "decision": decision,
        "resolved_by": resolved_by,
        "raw_agreement": raw_agreement,
        "kappa": kappa,
        "kappa_defined": kappa_defined,
    }
