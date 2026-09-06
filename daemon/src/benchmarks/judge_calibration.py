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
# The anchor schedule: every release and weekly during active use.
DEFAULT_CALIBRATION_INTERVAL_DAYS = 7
CALIBRATION_LOOP_SECONDS = 3600.0


class JudgeCalibrationError(ValueError):
    """The calibration request violates the calibration contract."""


# ── Pinned labels and independence ───────────────────────────────────


def pinned_label_set(
    dataset_id: str, version: str, items: list[dict[str, Any]],
    *, dataset_version_id: str | None = None,
) -> dict[str, Any]:
    """Pin one human label set with its content digest.

    ``dataset_version_id`` names the exact version the items come from;
    ``dataset_id`` and ``version`` stay for readers of older records,
    which may carry a version id in ``dataset_id``.
    """
    if not items:
        raise JudgeCalibrationError("A label set holds at least one item")
    pinned = sorted(
        (
            _pinned_item(item)
            for item in items
        ),
        key=lambda item: str(item["item_id"]),
    )
    label_set: dict[str, Any] = {
        "dataset_id": dataset_id,
        "version": version,
        "items": pinned,
        "label_digest": content_checksum(pinned),
    }
    if dataset_version_id:
        label_set["dataset_version_id"] = str(dataset_version_id)
    return label_set


# The content fields one anchor item can carry beside its label, so
# the judge reads the same material the human reviewer labelled even
# when the dataset version is not available at calibration time.
ANCHOR_CONTENT_FIELDS = ("input", "expected_output", "candidate")


def _pinned_item(item: dict[str, Any]) -> dict[str, Any]:
    pinned = {
        "item_id": str(item["item_id"]),
        "label": str(item["label"]),
        "reviewers": sorted(str(r) for r in item.get("reviewers") or []),
    }
    for field in ANCHOR_CONTENT_FIELDS:
        value = item.get(field)
        if value is not None and str(value) != "":
            pinned[field] = str(value)
    return pinned


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


# ── Anchor sets and the weekly calibration schedule ──────────────────


def next_due(after: str, interval_days: int) -> str:
    """The next due timestamp one interval after ``after``."""
    from datetime import UTC, datetime, timedelta

    text = str(after)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    due = moment.astimezone(UTC) + timedelta(days=int(interval_days))
    return due.strftime("%Y-%m-%dT%H:%M:%SZ")


def anchor_set_record(
    *,
    anchor_id: str,
    judge_id: str,
    judge_version: str,
    judge_model: str,
    prompt_digest: str,
    scorer_id: str,
    scorer_version: str,
    label_set: dict[str, Any],
    candidate_models: list[str],
    now: str,
    interval_days: int = DEFAULT_CALIBRATION_INTERVAL_DAYS,
    threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
    drift_tolerance: float = DEFAULT_DRIFT_TOLERANCE,
) -> dict[str, Any]:
    """Build one validating anchor set with its calibration schedule.

    The first calibration is due immediately, so a new anchor set
    calibrates on the next scheduler pass and then every interval.
    """
    record = {
        "schema_id": "judge-anchor-set",
        "schema_version": 2,
        "anchor_id": anchor_id,
        "judge": {
            "judge_id": judge_id,
            "version": judge_version,
            "model": judge_model,
            "prompt_digest": prompt_digest,
        },
        "scorer": {"scorer_id": scorer_id, "version": scorer_version},
        "label_set": label_set,
        "candidate_models": list(candidate_models),
        "schedule": {
            "interval_days": int(interval_days),
            "next_due_at": now,
            "created_at": now,
        },
        "threshold": float(threshold),
        "drift_tolerance": float(drift_tolerance),
        "state": "active",
    }
    validate_record(record)
    return record


async def register_anchor_set(record: dict[str, Any]) -> dict[str, Any]:
    """Store one anchor set through the one facade."""
    from benchmarks import facade

    saved = await facade.execute("record_judge_anchor_set", {"record": record})
    return {"anchor_id": saved["id"], "record": record}


async def list_anchor_sets(*, now: str | None = None) -> list[dict[str, Any]]:
    """List every anchor set with its schedule state."""
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM judge_anchor_sets ORDER BY next_due_at, id",
        )
    listed = []
    for row in rows:
        entry = {**dict(row), "record": json.loads(row["record"])}
        entry["due"] = bool(
            now is not None and str(row["state"]) == "active"
            and str(row["next_due_at"]) <= now
        )
        listed.append(entry)
    return listed


async def _resolve_version_id(
    dataset_id: str, version: str, dataset_version_id: str | None = None,
) -> str | None:
    """Find the dataset version one label set names.

    An explicit ``dataset_version_id`` wins. Otherwise the label set's
    ``dataset_id`` may already be a version id, or a dataset id whose
    ``version`` names a version id, a version number, or nothing, in
    which case the newest version applies.
    """
    if dataset_version_id:
        return str(dataset_version_id)
    if not dataset_id:
        return None
    probe, _total = await db.list_dataset_items(dataset_id, limit=1, offset=0)
    if probe:
        return dataset_id
    dataset = await db.get_dataset(dataset_id)
    if not dataset:
        return None
    versions = list(dataset.get("versions") or [])
    for candidate in versions:
        if str(candidate.get("id")) == version or str(candidate.get("version")) == version:
            return str(candidate["id"])
    if versions:
        newest = max(versions, key=lambda entry: int(entry.get("version") or 0))
        return str(newest["id"])
    return None


async def _anchor_items(label_set: dict[str, Any]) -> list[dict[str, Any]]:
    """Join the pinned labels with the content the judge reads.

    Inline content on a pinned item wins; otherwise the dataset item
    with the same key supplies the input and the reference answer.
    """
    items_by_id: dict[str, dict[str, Any]] = {}
    version_id = await _resolve_version_id(
        str(label_set.get("dataset_id") or ""),
        str(label_set.get("version") or ""),
        label_set.get("dataset_version_id"),
    )
    if version_id:
        offset = 0
        while True:
            page, total = await db.list_dataset_items(
                version_id, limit=200, offset=offset,
            )
            for item in page:
                items_by_id[str(item.get("item_key") or item.get("id"))] = item
                items_by_id.setdefault(str(item.get("id")), item)
            offset += len(page)
            if not page or offset >= int(total):
                break
    joined = []
    for pinned in label_set["items"]:
        item = items_by_id.get(str(pinned["item_id"])) or {}
        joined.append({
            "item_id": str(pinned["item_id"]),
            "label": str(pinned["label"]),
            "input": pinned.get("input") or item.get("input"),
            "expected_output": (
                pinned.get("expected_output") or item.get("expected_output")
            ),
            "candidate": pinned.get("candidate"),
        })
    return joined


async def calibrate_anchor_set(
    anchor: dict[str, Any], *, judge: Any, now: str,
) -> dict[str, Any]:
    """Run the judge over one anchor set and store the calibration.

    The judge labels every anchor item inside the pinned vocabulary,
    the calibration compares the labels with the pinned human labels,
    and the schedule advances one interval whatever the outcome, so a
    failing judge stays visible instead of silently retried.
    """
    record = anchor.get("record", anchor)
    label_set = record["label_set"]
    vocabulary = sorted({str(item["label"]) for item in label_set["items"]})
    outputs: dict[str, str] = {}
    for item in await _anchor_items(label_set):
        outputs[item["item_id"]] = judge.label(item, vocabulary)
    judge_info = record["judge"]
    previous = await latest_calibration(
        judge_info["judge_id"], judge_info["version"],
    )
    calibration = calibrate(
        judge_id=judge_info["judge_id"],
        judge_version=judge_info["version"],
        judge_model=judge_info["model"],
        prompt_digest=judge_info["prompt_digest"],
        scorer_id=record["scorer"]["scorer_id"],
        scorer_version=record["scorer"]["version"],
        label_set=label_set,
        judge_outputs=outputs,
        candidate_models=list(record["candidate_models"]),
        previous=previous,
        threshold=float(record["threshold"]),
        drift_tolerance=float(record["drift_tolerance"]),
        now=now,
    )
    stored = await store_calibration(calibration)
    from benchmarks import facade

    due = next_due(now, int(record["schedule"]["interval_days"]))
    await facade.execute("advance_anchor_schedule", {
        "anchor_id": record["anchor_id"],
        "last_calibrated_at": now,
        "next_due_at": due,
        "state": "active",
    })
    return {
        "anchor_id": record["anchor_id"],
        "calibration_id": stored["calibration_id"],
        "state": calibration["state"],
        "raw_agreement": calibration["agreement"]["raw"],
        "next_due_at": due,
        "judge_outputs": outputs,
    }


async def run_due_calibrations(
    *, now: str, judge_factory: Any,
) -> list[dict[str, Any]]:
    """Calibrate every anchor set whose schedule is due."""
    from benchmarks import evaluation_records

    outcomes = []
    for anchor in await evaluation_records.due_anchor_sets(now):
        judge = judge_factory(anchor["record"])
        if judge is None:
            outcomes.append({
                "anchor_id": anchor["record"]["anchor_id"],
                "state": "skipped",
                "reason": "no judge transport is configured",
            })
            continue
        outcomes.append(await calibrate_anchor_set(anchor, judge=judge, now=now))
    return outcomes


def default_judge_factory(record: dict[str, Any]) -> Any:
    """Build the model-backed judge one anchor set pins, when possible."""
    from benchmarks import model_backed

    settings = model_backed.gateway_settings_from_environment()
    if settings is None:
        return None
    judge = record["judge"]
    transport = model_backed.ModelTransport(settings, model=judge["model"])
    return model_backed.ModelBackedJudge(
        transport, judge_id=judge["judge_id"], version=judge["version"],
    )


async def calibration_loop(
    *,
    interval_seconds: float = CALIBRATION_LOOP_SECONDS,
    judge_factory: Any = None,
    iterations: int | None = None,
) -> None:
    """Run due calibrations on a fixed cadence until cancelled."""
    import asyncio
    import logging

    logger = logging.getLogger("bmas.daemon.calibration")
    factory = judge_factory or default_judge_factory
    completed = 0
    while iterations is None or completed < iterations:
        try:
            now = await db.database_utc_now()
            outcomes = await run_due_calibrations(now=now, judge_factory=factory)
            if outcomes:
                logger.info("Anchor calibrations: %s", outcomes)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the loop survives one failure
            logger.warning("Anchor calibration pass failed: %s", error)
        completed += 1
        if iterations is not None and completed >= iterations:
            break
        await asyncio.sleep(interval_seconds)


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
