"""Evaluation API resources behind the one facade.

The resource handlers route every mutation through the facade, the
draft publication creates its legacy compatibility projection in one
transaction, dual-read resources select one complete source, and the
authority resource reports the migration phase with the facade
counters.
"""

from __future__ import annotations

from typing import cast

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from test_evaluation_contracts import (
    valid_benchmark_source,
    valid_dataset_draft,
    valid_evaluation_case,
    valid_scorer_spec,
)

import database as db
from benchmarks import facade
from routes import evaluation as evaluation_routes

NO_REQUEST = cast("Request", None)


@pytest_asyncio.fixture
async def resources_db(tmp_path, monkeypatch):
    path = str(tmp_path / "resources.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    facade.reset_metrics()
    await db.init_db()
    return path


@pytest.mark.asyncio
async def test_the_draft_lifecycle_publishes_one_projection(resources_db):
    source = await evaluation_routes.create_source_endpoint(
        NO_REQUEST,
        evaluation_routes.RecordEnvelope(record=valid_benchmark_source()),
    )
    draft = await evaluation_routes.create_draft_endpoint(
        NO_REQUEST,
        evaluation_routes.RecordEnvelope(
            record=valid_dataset_draft(),
            links={"source_id": source["id"]},
        ),
    )
    case = valid_evaluation_case()
    await evaluation_routes.add_draft_case_endpoint(
        NO_REQUEST,
        draft["id"],
        evaluation_routes.RecordEnvelope(record=case),
    )
    await evaluation_routes.add_transformation_endpoint(
        NO_REQUEST,
        draft["id"],
        evaluation_routes.TransformRecipeInput(
            position=0, recipe={"operation": "shuffle", "seed": 7},
        ),
    )
    published = await evaluation_routes.publish_draft_endpoint(
        NO_REQUEST,
        draft["id"],
        evaluation_routes.DraftPublishInput(
            dataset_id="dataset-projected",
            version_id="version-projected",
            name="Projected dataset",
        ),
    )
    assert published["item_count"] == 1
    # One transaction created the compatible legacy projection: the
    # published version and its projected item exist, and the draft
    # froze.
    dataset = await db.get_dataset("dataset-projected")
    assert dataset is not None
    assert dataset["versions"][0]["id"] == "version-projected"
    assert dataset["versions"][0]["status"] == "published"
    items, total = await db.list_dataset_items("version-projected")
    assert total == 1
    assert items[0]["item_key"] == case["case_id"]
    assert items[0]["input"] == case["task"]["instructions"]
    stored = await evaluation_routes.get_draft_endpoint(draft["id"])
    assert stored["status"] == "published"
    # A frozen draft rejects another case through the same facade.
    with pytest.raises(HTTPException) as conflict:
        second = valid_evaluation_case()
        second["case_id"] = "example-002"
        await evaluation_routes.add_draft_case_endpoint(
            NO_REQUEST,
            draft["id"],
            evaluation_routes.RecordEnvelope(record=second),
        )
    assert conflict.value.status_code in {409, 422, 500}


@pytest.mark.asyncio
async def test_scorer_resources_publish_and_dual_read(resources_db):
    record = valid_scorer_spec()
    saved = await evaluation_routes.create_scorer_endpoint(
        NO_REQUEST, evaluation_routes.RecordEnvelope(record=record),
    )
    await evaluation_routes.publish_scorer_endpoint(
        NO_REQUEST, saved["id"],
    )
    read = await evaluation_routes.read_scorer_endpoint(
        record["scorer_id"], record["version"],
    )
    assert read["source"] == "current"
    assert read["record"]["scorer_id"] == record["scorer_id"]
    # A legacy seeded scorer reads through the fallback adapter.
    fallback = await evaluation_routes.read_scorer_endpoint(
        "scorer-exact-match-v1", "1",
    )
    assert fallback["source"] == "legacy"
    with pytest.raises(HTTPException) as missing:
        await evaluation_routes.read_scorer_endpoint("scorer-none", "9")
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_invalid_records_reject_at_the_resource_boundary(
    resources_db,
):
    record = valid_benchmark_source()
    record["surprise_field"] = "unexpected"
    with pytest.raises(HTTPException) as rejected:
        await evaluation_routes.create_source_endpoint(
            NO_REQUEST, evaluation_routes.RecordEnvelope(record=record),
        )
    assert rejected.value.status_code == 422


@pytest.mark.asyncio
async def test_the_authority_resource_reports_facade_counters(
    resources_db,
):
    await evaluation_routes.create_source_endpoint(
        NO_REQUEST,
        evaluation_routes.RecordEnvelope(record=valid_benchmark_source()),
    )
    snapshot = await evaluation_routes.authority_endpoint()
    assert snapshot["phase"] == "expand"
    assert snapshot["facade"]["generations"]["current"] >= 1
    assert snapshot["facade"]["commands"]["import_source"] == 1
    assert snapshot["direct_legacy_call_events"] == 0
