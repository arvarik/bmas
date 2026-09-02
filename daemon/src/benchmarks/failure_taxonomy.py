"""The documented failure taxonomy with multi-label classification.

The taxonomy starts from the long-horizon and multi-agent categories
in the research record: the HORIZON long-trajectory families
(planning, memory, false assumptions, history errors, environment
errors, and instruction errors) and the MAST multi-agent families
(specification and design, inter-agent misalignment, and task
verification). Injected infrastructure faults stay in their own
family, separate from model reasoning faults. One attempt carries
several classes at once, every classification is immutable, and a
human correction supersedes the prior record through a new record
that keeps the complete history readable.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import database as db
from benchmarks.evaluation_contracts import validate_record

TAXONOMY_VERSION = "1"

FAILURE_TAXONOMY: dict[str, dict[str, str]] = {
    "long_horizon": {
        "planning": "The plan omits, misorders, or abandons required steps.",
        "memory": "Earlier facts, goals, or constraints drop out of use.",
        "false_assumption": "The agent acts on an unverified belief.",
        "history_error": "The agent misreads its own action history.",
        "environment_error": "The agent misreads the environment state.",
        "instruction_error": "The agent violates an explicit instruction.",
    },
    "multi_agent_specification": {
        "task_misinterpretation": "Agents solve a different task.",
        "role_ambiguity": "Agent roles overlap or stay undefined.",
        "poor_decomposition": "Subtasks split badly or lose coverage.",
        "duplicate_roles": "Two agents perform the same responsibility.",
        "missing_termination_condition": "No agent knows when to stop.",
    },
    "multi_agent_misalignment": {
        "communication_breakdown": "Messages fail to transfer state.",
        "coordination_failure": "Agents act on conflicting state.",
        "step_repetition": "Agents repeat completed steps.",
        "information_withholding": "One agent keeps needed information.",
        "task_derailment": "The conversation leaves the task.",
    },
    "multi_agent_verification": {
        "inadequate_validation": "Outputs pass without a real check.",
        "false_completion": "A completion claim has no verified success.",
        "premature_termination": "Work stops before the goal is met.",
    },
    # Infrastructure faults stay separate from reasoning faults so a
    # fault schedule never inflates a model failure rate.
    "infrastructure": {
        "injected_fault": "A declared fault schedule caused the failure.",
        "provider_failure": "A provider or transport failed.",
        "sandbox_wall_time_kill": "The safety deadline killed the work.",
    },
}

REASONING_FAMILIES = tuple(
    family for family in FAILURE_TAXONOMY if family != "infrastructure"
)


class FailureTaxonomyError(ValueError):
    """The classification violates the documented taxonomy."""


def taxonomy_document() -> dict[str, Any]:
    """Return the complete taxonomy for the interface and exports."""
    return {
        "version": TAXONOMY_VERSION,
        "families": [
            {
                "family": family,
                "reasoning": family in REASONING_FAMILIES,
                "classes": [
                    {"name": name, "description": description}
                    for name, description in sorted(classes.items())
                ],
            }
            for family, classes in FAILURE_TAXONOMY.items()
        ],
    }


def validate_classes(classes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate one multi-label class list against the taxonomy."""
    if not classes:
        raise FailureTaxonomyError(
            "A classification names at least one failure class"
        )
    validated = []
    seen: set[tuple[str, str]] = set()
    for entry in classes:
        family = str(entry.get("family") or "")
        name = str(entry.get("name") or "")
        if family not in FAILURE_TAXONOMY:
            raise FailureTaxonomyError(f"Unknown failure family: {family!r}")
        if name not in FAILURE_TAXONOMY[family]:
            raise FailureTaxonomyError(
                f"Unknown failure class {name!r} in family {family!r}"
            )
        if (family, name) in seen:
            raise FailureTaxonomyError(
                f"The class {family}/{name} repeats in one classification"
            )
        seen.add((family, name))
        confidence = entry.get("confidence")
        validated.append({
            "family": family,
            "name": name,
            "confidence": (
                float(confidence) if confidence is not None else None
            ),
        })
    return validated


def classes_from_trajectory(
    trajectory_result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Derive automatic classes from one trajectory scorer result."""
    values = {
        str(dimension["name"]): dimension.get("value")
        for dimension in trajectory_result.get("dimensions") or []
    }
    classes = []
    if values.get("loop_free") == 0.0:
        classes.append({"family": "multi_agent_misalignment",
                        "name": "step_repetition"})
    if values.get("no_false_completion") == 0.0:
        classes.append({"family": "multi_agent_verification",
                        "name": "false_completion"})
    if values.get("constraints_kept") == 0.0:
        classes.append({"family": "long_horizon",
                        "name": "instruction_error"})
    return classes


def classification_record(
    *,
    attempt_id: str,
    classes: list[dict[str, Any]],
    source: str,
    classifier: str,
    evidence_references: list[str],
    supersedes: str | None = None,
    correction: dict[str, str] | None = None,
    now: str = "1970-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Build one validating classification record."""
    if source not in ("automatic", "human"):
        raise FailureTaxonomyError(
            f"Unknown classification source: {source!r}"
        )
    if source == "human" and not correction:
        raise FailureTaxonomyError(
            "A human classification records its reviewer and reason"
        )
    record: dict[str, Any] = {
        "schema_id": "failure-classification-record",
        "schema_version": 2,
        "classification_id": f"classification-{uuid.uuid4().hex}",
        "attempt_id": attempt_id,
        "classes": validate_classes(classes),
        "source": source,
        "classifier": classifier,
        "evidence_references": list(evidence_references),
        "supersedes": supersedes,
        "classified_at": now,
    }
    if correction:
        record["correction"] = {
            "reviewer": str(correction["reviewer"]),
            "reason": str(correction["reason"]),
        }
    validate_record(record)
    return record


async def record_classification(record: dict[str, Any]) -> dict[str, Any]:
    """Store one classification through the one facade."""
    from benchmarks import facade

    saved = await facade.execute(
        "record_failure_classification",
        {
            "record": record,
            "attempt_id": record["attempt_id"],
            "supersedes": record.get("supersedes"),
        },
    )
    return {"classification_id": saved["id"], "record": record}


async def classification_history(attempt_id: str) -> dict[str, Any]:
    """Read the complete classification chain for one attempt.

    The current classification is the latest record that no later
    record supersedes. Every earlier record stays readable.
    """
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM failure_classification_records "
            "WHERE attempt_id = ? ORDER BY created_at, id",
            (attempt_id,),
        )
    records = [json.loads(row["record"]) for row in rows]
    superseded = {
        record["supersedes"] for record in records if record["supersedes"]
    }
    current = [
        record for record in records
        if record["classification_id"] not in superseded
    ]
    return {
        "attempt_id": attempt_id,
        "history": records,
        "current": current[-1] if current else None,
    }


async def correct_classification(
    prior_id: str,
    *,
    classes: list[dict[str, Any]],
    reviewer: str,
    reason: str,
    now: str = "1970-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Apply one human correction as a new superseding record."""
    from benchmarks import evaluation_records

    prior = await evaluation_records.get_record(
        "failure-classification-record", prior_id,
    )
    if prior is None:
        raise FailureTaxonomyError(
            f"The classification {prior_id} does not exist"
        )
    record = classification_record(
        attempt_id=str(prior["attempt_id"]),
        classes=classes,
        source="human",
        classifier=f"human:{reviewer}",
        evidence_references=list(
            prior["record"].get("evidence_references") or [],
        ),
        supersedes=prior_id,
        correction={"reviewer": reviewer, "reason": reason},
        now=now,
    )
    return await record_classification(record)
