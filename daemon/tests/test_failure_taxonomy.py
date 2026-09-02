"""The documented failure taxonomy with multi-label classifications.

The taxonomy carries the long-horizon and multi-agent families from
the research record with infrastructure faults kept separate. One
attempt takes several classes at once, every record is immutable,
and a human correction supersedes the prior record while the
complete history stays readable.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from test_evidence_capture import make_attempts

import database as db
from benchmarks import failure_taxonomy
from benchmarks.failure_taxonomy import (
    FAILURE_TAXONOMY,
    REASONING_FAMILIES,
    FailureTaxonomyError,
    classes_from_trajectory,
    classification_record,
    taxonomy_document,
    validate_classes,
)


def test_taxonomy_documents_the_research_record_families():
    document = taxonomy_document()
    families = {entry["family"]: entry for entry in document["families"]}
    assert set(families) == {
        "long_horizon", "multi_agent_specification",
        "multi_agent_misalignment", "multi_agent_verification",
        "infrastructure",
    }
    long_horizon = {cls["name"] for cls in families["long_horizon"]["classes"]}
    assert long_horizon == {
        "planning", "memory", "false_assumption", "history_error",
        "environment_error", "instruction_error",
    }
    assert families["infrastructure"]["reasoning"] is False
    assert "infrastructure" not in REASONING_FAMILIES
    for family in FAILURE_TAXONOMY.values():
        assert all(description for description in family.values())


def test_multiple_classes_apply_at_once():
    validated = validate_classes([
        {"family": "long_horizon", "name": "planning", "confidence": 0.9},
        {"family": "multi_agent_misalignment", "name": "step_repetition"},
    ])
    assert [entry["name"] for entry in validated] == [
        "planning", "step_repetition",
    ]
    assert validated[1]["confidence"] is None


def test_unknown_or_repeated_classes_reject():
    with pytest.raises(FailureTaxonomyError, match="Unknown failure family"):
        validate_classes([{"family": "vibes", "name": "bad"}])
    with pytest.raises(FailureTaxonomyError, match="Unknown failure class"):
        validate_classes([{"family": "long_horizon", "name": "laziness"}])
    with pytest.raises(FailureTaxonomyError, match="repeats"):
        validate_classes([
            {"family": "long_horizon", "name": "memory"},
            {"family": "long_horizon", "name": "memory"},
        ])
    with pytest.raises(FailureTaxonomyError, match="at least one"):
        validate_classes([])


def test_trajectory_results_map_to_classes():
    classes = classes_from_trajectory({"dimensions": [
        {"name": "loop_free", "value": 0.0},
        {"name": "no_false_completion", "value": 0.0},
        {"name": "constraints_kept", "value": 1.0},
    ]})
    assert classes == [
        {"family": "multi_agent_misalignment", "name": "step_repetition"},
        {"family": "multi_agent_verification", "name": "false_completion"},
    ]


def test_human_classification_requires_a_correction():
    with pytest.raises(FailureTaxonomyError, match="reviewer and reason"):
        classification_record(
            attempt_id="attempt-a",
            classes=[{"family": "long_horizon", "name": "memory"}],
            source="human",
            classifier="human:reviewer-a",
            evidence_references=[],
        )
    with pytest.raises(FailureTaxonomyError, match="Unknown classification"):
        classification_record(
            attempt_id="attempt-a",
            classes=[{"family": "long_horizon", "name": "memory"}],
            source="oracle",
            classifier="x",
            evidence_references=[],
        )


@pytest_asyncio.fixture
async def taxonomy_db(tmp_path, monkeypatch):
    path = str(tmp_path / "taxonomy.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    return await make_attempts(1)


@pytest.mark.asyncio
async def test_correction_supersedes_and_keeps_history(taxonomy_db):
    import aiosqlite

    attempt_id = taxonomy_db[0]
    automatic = classification_record(
        attempt_id=attempt_id,
        classes=[{"family": "long_horizon", "name": "planning"}],
        source="automatic",
        classifier="trajectory-classifier",
        evidence_references=[attempt_id],
        now="2026-09-01T00:00:00Z",
    )
    stored = await failure_taxonomy.record_classification(automatic)
    corrected = await failure_taxonomy.correct_classification(
        stored["classification_id"],
        classes=[
            {"family": "long_horizon", "name": "memory"},
            {"family": "infrastructure", "name": "injected_fault"},
        ],
        reviewer="reviewer-a",
        reason="the trace shows a dropped goal after a fault",
        now="2026-09-02T00:00:00Z",
    )
    history = await failure_taxonomy.classification_history(attempt_id)
    assert len(history["history"]) == 2
    assert history["current"]["classification_id"] == (
        corrected["classification_id"]
    )
    assert history["current"]["supersedes"] == stored["classification_id"]
    assert history["current"]["correction"]["reviewer"] == "reviewer-a"
    assert [cls["name"] for cls in history["current"]["classes"]] == [
        "memory", "injected_fault",
    ]
    # The prior record stays readable and immutable.
    assert history["history"][0]["classes"][0]["name"] == "planning"
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "DELETE FROM failure_classification_records WHERE id = ?",
                (stored["classification_id"],),
            )


@pytest.mark.asyncio
async def test_correcting_a_missing_classification_rejects(taxonomy_db):
    with pytest.raises(FailureTaxonomyError, match="does not exist"):
        await failure_taxonomy.correct_classification(
            "classification-missing",
            classes=[{"family": "long_horizon", "name": "memory"}],
            reviewer="reviewer-a",
            reason="none",
        )
