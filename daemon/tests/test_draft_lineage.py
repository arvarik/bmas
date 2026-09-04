"""Draft editing, lineage, publication, and trust inheritance tests.

The scenarios follow the verification plan: import a pinned source
into a draft, edit cases with undo and redo, apply a recipe, publish
an immutable version, rebuild it from the pinned source with an equal
digest, clone a child version with complete lineage, and reject any
published-version edit. The trust scenarios prove that derivation
preserves or strengthens every source restriction and that only one
authenticated promotion bound to exact content lifts one reviewable
restriction.
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
)

import database as db
from benchmarks import draft_editor, evaluation_records, facade
from benchmarks.draft_editor import (
    DraftEditorError,
    apply_deployment_caps,
    assert_no_capability_increase,
    assert_promotion_applies,
    compile_effective_restrictions,
    promotion_decision,
)
from benchmarks.source_adapters import (
    TRUST_CAPABILITY_PROFILES,
    TrustPolicyError,
    authorize_capability_increase,
)
from benchmarks.transform_profile import (
    PROFILE_NAME,
    PROFILE_VERSION,
    apply_recipe,
    strict_parse,
)
from routes import evaluation as evaluation_routes

SOURCE_ID = "source-gsm8k"

NO_REQUEST = cast("Request", None)


@pytest_asyncio.fixture
async def draft_db(tmp_path, monkeypatch):
    path = str(tmp_path / "draft.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    await facade.execute(
        "import_source", {"record": valid_benchmark_source()},
    )
    return path


def _case(case_id: str, instructions: str = "Add 20 and 22.") -> dict:
    case = valid_evaluation_case()
    case["case_id"] = case_id
    case["task"]["instructions"] = instructions
    return case


async def _draft(
    draft_id: str = "draft-alpha",
    *,
    metadata: dict | None = None,
    parent_version_id: str | None = None,
) -> str:
    record = valid_dataset_draft()
    record["draft_id"] = draft_id
    record["parent_version_id"] = parent_version_id
    if metadata is not None:
        record["metadata"] = metadata
    await facade.execute(
        "create_draft",
        {
            "record": record,
            "source_id": SOURCE_ID,
            "parent_version_id": parent_version_id,
        },
    )
    return draft_id


def _recipe(operations: list[dict], seed: int = 3) -> dict:
    return {
        "profile": PROFILE_NAME,
        "profile_version": PROFILE_VERSION,
        "seed": seed,
        "operations": operations,
    }


# ── Scenario 1: import one pinned source into one draft ──────────────


@pytest.mark.asyncio
async def test_pinned_source_links_into_the_draft(draft_db):
    draft_id = await _draft()
    stored = await evaluation_records.get_record(
        "dataset-draft", draft_id,
    )
    assert stored["source_id"] == SOURCE_ID
    source = await evaluation_records.get_record(
        "benchmark-source", SOURCE_ID,
    )
    assert source["record"]["pinned_revision"] == (
        "e53f048856ff4f594e959d75785d2c2d37b678ee"
    )


# ── Scenarios 2 and 4: edits with undo and redo ──────────────────────


@pytest.mark.asyncio
async def test_edit_add_delete_and_duplicate_cases(draft_db):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one"))
    await draft_editor.edit_case(draft_id, _case("case-two", "Second."))
    await draft_editor.duplicate_case(draft_id, "case-one", "case-three")
    await draft_editor.delete_case(draft_id, "case-two")
    preview = await draft_editor.distribution_preview(draft_id)
    assert preview["case_count"] == 2
    with pytest.raises(DraftEditorError, match="does not exist"):
        await draft_editor.delete_case(draft_id, "case-missing")


@pytest.mark.asyncio
async def test_undo_and_redo_restore_exact_case_content(draft_db):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one", "First."))
    await draft_editor.edit_case(draft_id, _case("case-one", "Edited."))

    await draft_editor.undo(draft_id)
    stored = await evaluation_records.get_record(
        "evaluation-case", f"{draft_id}:case-one",
    )
    assert stored["record"]["task"]["instructions"] == "First."

    await draft_editor.redo(draft_id)
    stored = await evaluation_records.get_record(
        "evaluation-case", f"{draft_id}:case-one",
    )
    assert stored["record"]["task"]["instructions"] == "Edited."

    # A second undo removes the edit; a third undo removes the case.
    await draft_editor.undo(draft_id)
    await draft_editor.undo(draft_id)
    assert await evaluation_records.get_record(
        "evaluation-case", f"{draft_id}:case-one",
    ) is None
    with pytest.raises(DraftEditorError, match="no edit to undo"):
        await draft_editor.undo(draft_id)


@pytest.mark.asyncio
async def test_new_edit_truncates_the_redo_branch(draft_db):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one", "First."))
    await draft_editor.edit_case(draft_id, _case("case-one", "Second."))
    await draft_editor.undo(draft_id)
    await draft_editor.edit_case(draft_id, _case("case-one", "Third."))
    with pytest.raises(DraftEditorError, match="no edit to redo"):
        await draft_editor.redo(draft_id)


# ── Scenario 3: ordered transformation recipes and previews ──────────


@pytest.mark.asyncio
async def test_deterministic_transform_preview(draft_db):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one", "Alpha."))
    await draft_editor.edit_case(draft_id, _case("case-two", "Beta."))
    recipe = _recipe([
        {"operation": "map_template",
         "parameters": {
             "target": "prompt",
             "template": "Task: ${input}",
             "bindings": {"n": {"pointer": "/n"},
                          "input": {"pointer": "/input"}},
         }},
    ])
    first = await draft_editor.transform_preview(draft_id, recipe)
    second = await draft_editor.transform_preview(draft_id, recipe)
    assert first["dataset_digest"] == second["dataset_digest"]
    assert first["recipe_digest"] == second["recipe_digest"]
    assert first["preview"][0]["prompt"] == "Task: Alpha."
    assert first["engine"]["profile_version"] == PROFILE_VERSION


@pytest.mark.asyncio
async def test_validation_issue_list_recomputes(draft_db):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one"))
    issues = await draft_editor.validation_issues(draft_id)
    assert issues == []


@pytest.mark.asyncio
async def test_hidden_test_cases_stay_outside_previews(draft_db):
    draft_id = await _draft()
    hidden = _case("case-hidden", "Secret holdout question.")
    hidden["classification"]["split"] = "hidden_test"
    await draft_editor.edit_case(draft_id, hidden)
    await draft_editor.edit_case(draft_id, _case("case-open"))

    preview = await draft_editor.distribution_preview(draft_id)
    assert preview["case_count"] == 1
    assert preview["hidden_test_count"] == 1
    assert "hidden_test" not in preview["splits"]

    transformed = await draft_editor.transform_preview(
        draft_id,
        _recipe([{"operation": "normalize", "parameters": {}}]),
    )
    rendered = str(transformed["preview"])
    assert "Secret holdout" not in rendered


# ── Scenarios 5, 6, 7, and 10: publication and rebuilds ──────────────


async def _pin_rebuild_expectation(
    draft_id: str, rows: list[dict], recipe: dict,
) -> str:
    """Persist source bytes and pin the expected transform digest."""
    from activation_service import persist_protected_artifact
    from benchmarks.source_adapters import _source_store
    from core.asset_store import DataClass

    payload = "\n".join(
        __import__("json").dumps(row, sort_keys=True) for row in rows
    ).encode("utf-8")
    artifact_digest = persist_protected_artifact(
        _source_store(),
        payload,
        media_type="application/x-ndjson",
        access_policy="benchmark-source-bytes",
        data_class=DataClass.INTERNAL,
        referenced_by=draft_id,
    )
    expected = apply_recipe(
        [strict_parse(line.encode()) for line in
         payload.decode().splitlines()],
        recipe,
    )["dataset_digest"]
    stored = await evaluation_records.get_record(
        "dataset-draft", draft_id,
    )
    record = stored["record"]
    record["metadata"] = {
        **record.get("metadata", {}),
        "source_artifact_digest": artifact_digest,
        "expected_transform_digest": expected,
    }
    await evaluation_records.update_draft_record(draft_id, record)
    return expected


@pytest.mark.asyncio
async def test_publish_rebuilds_recipe_and_freezes_the_version(draft_db):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one"))
    rows = [{"case_id": "case-one", "input": "Add 20 and 22."}]
    recipe = _recipe([{"operation": "normalize", "parameters": {}}])
    await _pin_rebuild_expectation(draft_id, rows, recipe)

    published = await draft_editor.publish_governed(
        draft_id,
        dataset_id="dataset-alpha",
        version_id="version-one",
        name="Alpha",
        recipe=recipe,
    )
    assert published["rebuild_verification"]["verified"] is True
    assert published["contamination_record_id"]
    assert published["policy_digest"]
    names = {
        restriction["name"]
        for restriction in published["effective_restrictions"]
    }
    # Derivation preserves every source restriction.
    assert {"deny_network", "deny_secrets"} <= names

    # Scenario 10: a published draft rejects every edit.
    with pytest.raises(DraftEditorError, match="not an editable draft"):
        await draft_editor.edit_case(draft_id, _case("case-two"))


@pytest.mark.asyncio
async def test_publish_blocks_on_rebuild_digest_mismatch(draft_db):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one"))
    rows = [{"case_id": "case-one", "input": "Add 20 and 22."}]
    recipe = _recipe([{"operation": "normalize", "parameters": {}}])
    await _pin_rebuild_expectation(draft_id, rows, recipe)

    stored = await evaluation_records.get_record(
        "dataset-draft", draft_id,
    )
    record = stored["record"]
    record["metadata"]["expected_transform_digest"] = "0" * 64
    await evaluation_records.update_draft_record(draft_id, record)

    with pytest.raises(DraftEditorError, match="does not match"):
        await draft_editor.publish_governed(
            draft_id,
            dataset_id="dataset-alpha",
            version_id="version-blocked",
            name="Blocked",
            recipe=recipe,
        )
    stored = await evaluation_records.get_record(
        "dataset-draft", draft_id,
    )
    assert stored["status"] == "editing"


@pytest.mark.asyncio
async def test_rebuild_from_pinned_source_gives_equal_digest(draft_db):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one"))
    rows = [
        {"case_id": "case-one", "input": "Add 20 and 22."},
        {"case_id": "case-two", "input": "Add 1 and 2."},
    ]
    recipe = _recipe([
        {"operation": "split",
         "parameters": {"weights": {"train": 2, "test": 1}}},
    ])
    expected = await _pin_rebuild_expectation(draft_id, rows, recipe)
    verification = await draft_editor.rebuild_verification(
        draft_id, recipe,
    )
    assert verification["verified"] is True
    assert verification["rebuilt_digest"] == expected


# ── Scenarios 8 and 9: cloning and lineage ───────────────────────────


@pytest.mark.asyncio
async def test_clone_publishes_a_child_with_complete_lineage(draft_db):
    parent_draft = await _draft("draft-parent")
    await draft_editor.edit_case(parent_draft, _case("case-one"))
    await draft_editor.publish_governed(
        parent_draft,
        dataset_id="dataset-alpha",
        version_id="version-parent",
        name="Parent",
    )

    child_draft = await _draft(
        "draft-child", parent_version_id="version-parent",
    )
    await draft_editor.edit_case(child_draft, _case("case-one"))
    await draft_editor.edit_case(
        child_draft, _case("case-two", "Child addition."),
    )
    confirmation = await draft_editor.publish_confirmation(child_draft)
    assert confirmation["parent_version_id"] == "version-parent"
    assert confirmation["source_ids"] == [SOURCE_ID]
    assert confirmation["case_count"] == 2
    assert len(confirmation["content_digest"]) == 64

    published = await draft_editor.publish_governed(
        child_draft,
        dataset_id="dataset-alpha",
        version_id="version-child",
        name="Child",
    )
    stored = await evaluation_records.get_record(
        "dataset-draft", child_draft,
    )
    assert stored["parent_version_id"] == "version-parent"
    assert stored["status"] == "published"
    assert published["version_id"] == "version-child"


@pytest.mark.asyncio
async def test_version_difference_view(draft_db):
    parent_draft = await _draft("draft-parent")
    await draft_editor.edit_case(parent_draft, _case("case-one"))
    await draft_editor.edit_case(parent_draft, _case("case-gone"))
    await draft_editor.publish_governed(
        parent_draft,
        dataset_id="dataset-alpha",
        version_id="version-parent",
        name="Parent",
    )
    child_draft = await _draft("draft-child")
    await draft_editor.edit_case(
        child_draft, _case("case-one", "Changed content."),
    )
    await draft_editor.edit_case(child_draft, _case("case-new"))
    difference = await draft_editor.version_difference(
        child_draft, "version-parent",
    )
    assert difference["added"] == ["case-new"]
    assert difference["removed"] == ["case-gone"]
    assert difference["changed"] == ["case-one"]


# ── Trust inheritance scenarios ──────────────────────────────────────


def test_clone_preserves_untrusted_denials():
    parent = TRUST_CAPABILITY_PROFILES["public_untrusted"]
    child = compile_effective_restrictions([parent])
    assert_no_capability_increase(parent, child)
    names = {restriction["name"] for restriction in child}
    assert {"deny_network", "deny_secrets", "deny_unsafe_tools"} <= names
    with pytest.raises(DraftEditorError, match="drops the restriction"):
        assert_no_capability_increase(parent, [])


def test_combined_sources_use_the_most_restrictive_set():
    combined = compile_effective_restrictions([
        TRUST_CAPABILITY_PROFILES["built_in_verified"],
        TRUST_CAPABILITY_PROFILES["public_untrusted"],
    ])
    names = {restriction["name"] for restriction in combined}
    # The union of restrictions is the intersection of capabilities.
    assert "benchmark_capability_profile_only" in names
    assert "deny_network" in names
    for parent in (
        TRUST_CAPABILITY_PROFILES["built_in_verified"],
        TRUST_CAPABILITY_PROFILES["public_untrusted"],
    ):
        assert_no_capability_increase(parent, combined)


def test_hard_behavior_wins_over_reviewable():
    combined = compile_effective_restrictions([
        [{"name": "deny_network", "behavior": "reviewable"}],
        [{"name": "deny_network", "behavior": "hard"}],
        [{"name": "deny_network", "behavior": "reviewable"}],
    ])
    assert combined == [{"name": "deny_network", "behavior": "hard"}]


def test_weakening_a_hard_restriction_rejects():
    parent = [{"name": "deny_network", "behavior": "hard"}]
    child = [{"name": "deny_network", "behavior": "reviewable"}]
    with pytest.raises(DraftEditorError, match="weakens the hard"):
        assert_no_capability_increase(parent, child)


@pytest.mark.asyncio
async def test_transform_and_republish_never_increases_capabilities(
    draft_db,
):
    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one"))
    published = await draft_editor.publish_governed(
        draft_id,
        dataset_id="dataset-alpha",
        version_id="version-one",
        name="Alpha",
    )
    source = await evaluation_records.get_record(
        "benchmark-source", SOURCE_ID,
    )
    assert_no_capability_increase(
        source["record"]["execution_restrictions"],
        published["effective_restrictions"],
    )


def test_promotion_requires_actor_evidence_and_policy_version():
    profile = TRUST_CAPABILITY_PROFILES["public_untrusted"]
    with pytest.raises(TrustPolicyError, match="authenticated operator"):
        authorize_capability_increase(
            profile, "bounded_network_destinations",
            operator_id="", evidence="review",
        )
    with pytest.raises(TrustPolicyError, match="evidence"):
        authorize_capability_increase(
            profile, "bounded_network_destinations",
            operator_id="operator-a", evidence=" ",
        )
    for missing in ("actor", "evidence", "policy_version", "reason"):
        values = {
            "actor": "operator-a",
            "evidence": "review-123",
            "policy_version": "1",
            "reason": "bounded egress for scoring",
        }
        values[missing] = ""
        with pytest.raises(DraftEditorError, match=missing):
            promotion_decision(
                **values,
                restriction_name="bounded_network_destinations",
                content_digest="a" * 64,
                child_version_id="version-child",
                prior_restrictions=profile,
                new_restrictions=[],
            )


def test_promotion_binds_to_exact_content_and_child_version():
    decision = promotion_decision(
        actor="operator-a",
        evidence="review-123",
        policy_version="1",
        reason="bounded egress for scoring",
        restriction_name="bounded_network_destinations",
        content_digest="a" * 64,
        child_version_id="version-child",
        prior_restrictions=[
            {"name": "bounded_network_destinations",
             "behavior": "reviewable"},
        ],
        new_restrictions=[],
    )
    assert len(decision["decision_digest"]) == 64
    assert_promotion_applies(
        decision, content_digest="a" * 64,
        child_version_id="version-child",
    )
    with pytest.raises(DraftEditorError, match="content digest"):
        assert_promotion_applies(
            decision, content_digest="b" * 64,
            child_version_id="version-child",
        )
    with pytest.raises(DraftEditorError, match="child version"):
        assert_promotion_applies(
            decision, content_digest="a" * 64,
            child_version_id="version-other",
        )


def test_approved_promotion_lifts_one_reviewable_restriction_only():
    parent = TRUST_CAPABILITY_PROFILES["public_untrusted"]
    child = [
        restriction for restriction in parent
        if restriction["name"] != "bounded_network_destinations"
    ]
    # The one approved reviewable restriction lifts.
    assert_no_capability_increase(
        parent, child,
        approved_promotions={"bounded_network_destinations"},
    )
    # Every other restriction stays active after that promotion.
    with pytest.raises(DraftEditorError):
        assert_no_capability_increase(
            parent,
            [restriction for restriction in child
             if restriction["name"] != "deny_network"],
            approved_promotions={"bounded_network_destinations"},
        )


def test_never_promotable_restrictions_reject_every_override():
    for name in sorted(draft_editor.NEVER_PROMOTABLE):
        with pytest.raises(DraftEditorError, match="never lifts"):
            promotion_decision(
                actor="operator-a",
                evidence="review-123",
                policy_version="1",
                reason="attempted override",
                restriction_name=name,
                content_digest="a" * 64,
                child_version_id="version-child",
                prior_restrictions=[],
                new_restrictions=[],
            )
    with pytest.raises(TrustPolicyError, match="hard"):
        authorize_capability_increase(
            TRUST_CAPABILITY_PROFILES["public_untrusted"],
            "deny_secrets",
            operator_id="operator-a",
            evidence="review-123",
        )


def test_deployment_caps_apply_after_the_promotion():
    parent = TRUST_CAPABILITY_PROFILES["public_untrusted"]
    promoted = authorize_capability_increase(
        parent, "bounded_network_destinations",
        operator_id="operator-a", evidence="review-123",
    )
    assert "bounded_network_destinations" not in {
        restriction["name"] for restriction in promoted
    }
    capped = apply_deployment_caps(
        promoted,
        [{"name": "bounded_network_destinations", "behavior": "hard"}],
    )
    by_name = {
        restriction["name"]: restriction["behavior"]
        for restriction in capped
    }
    # The deployment cap re-enters as hard even after the promotion.
    assert by_name["bounded_network_destinations"] == "hard"


# ── The editor resources ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_editor_endpoints_edit_undo_and_publish(draft_db):
    no_request = NO_REQUEST
    draft_id = await _draft()
    await evaluation_routes.edit_case_endpoint(
        no_request, draft_id,
        evaluation_routes.CaseEditInput(case=_case("case-one", "First.")),
    )
    await evaluation_routes.edit_case_endpoint(
        no_request, draft_id,
        evaluation_routes.CaseEditInput(case=_case("case-one", "Edited.")),
    )
    await evaluation_routes.undo_endpoint(no_request, draft_id)
    stored = await evaluation_records.get_record(
        "evaluation-case", f"{draft_id}:case-one",
    )
    assert stored["record"]["task"]["instructions"] == "First."
    await evaluation_routes.redo_endpoint(no_request, draft_id)

    validation = await evaluation_routes.validation_endpoint(draft_id)
    assert validation["issues"] == []
    preview = await evaluation_routes.transform_preview_endpoint(
        no_request, draft_id,
        evaluation_routes.TransformPreviewInput(
            recipe=_recipe([{"operation": "normalize", "parameters": {}}]),
        ),
    )
    assert preview["case_count"] == 1
    distributions = (
        await evaluation_routes.distribution_preview_endpoint(draft_id)
    )
    assert distributions["case_count"] == 1

    confirmation = (
        await evaluation_routes.publish_confirmation_endpoint(draft_id)
    )
    assert confirmation["case_count"] == 1
    published = await evaluation_routes.publish_governed_endpoint(
        no_request, draft_id,
        evaluation_routes.GovernedPublishInput(
            dataset_id="dataset-route",
            version_id="version-route",
            name="Route publish",
        ),
    )
    assert published["contamination_record_id"]

    with pytest.raises(HTTPException) as blocked:
        await evaluation_routes.edit_case_endpoint(
            no_request, draft_id,
            evaluation_routes.CaseEditInput(case=_case("case-two")),
        )
    assert blocked.value.status_code == 409


@pytest.mark.asyncio
async def test_asset_and_screening_endpoints(draft_db):
    import base64

    no_request = NO_REQUEST
    stored = await evaluation_routes.ingest_asset_endpoint(
        no_request,
        evaluation_routes.AssetUploadInput(
            original_name="diagram.png",
            declared_media_type="image/png",
            content_base64=base64.b64encode(
                b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
            ).decode("ascii"),
        ),
    )
    assert stored["state"] == "accepted"
    fetched = await evaluation_routes.get_asset_endpoint(
        stored["ingestion_id"],
    )
    assert fetched["state"] == "accepted"

    screening = await evaluation_routes.screening_endpoint(
        no_request,
        evaluation_routes.ScreeningInput(
            cases=[{"case_id": "case-one", "input": "Question text"}],
            corpus={"id": "corpus-x", "entries": []},
        ),
    )
    assert screening["result"] == "screened"
    assert "cannot prove" in screening["proof_disclaimer"]


@pytest.mark.asyncio
async def test_publish_screens_case_content_against_the_corpus(draft_db):
    draft_id = await _draft()
    text = "A well known benchmark question about the quick brown fox."
    await draft_editor.edit_case(draft_id, _case("case-one", text))
    published = await draft_editor.publish_governed(
        draft_id,
        dataset_id="dataset-alpha",
        version_id="version-screened",
        name="Screened",
        screening_corpus={"id": "corpus-pinned", "entries": [text]},
    )
    assert published["screening_result"] == "suspected"
    record = await evaluation_records.get_record(
        "contamination-rights-record",
        published["contamination_record_id"],
    )
    assert record["record"]["matches"][0]["kind"] == "content_hash"


@pytest.mark.asyncio
async def test_publication_stores_the_dataset_version_record(draft_db):
    """Publication freezes one dataset-version record beside the projection."""
    from benchmarks import evaluation_records
    from benchmarks.provenance import content_checksum

    draft_id = await _draft()
    await draft_editor.edit_case(draft_id, _case("case-one"))
    rows = [{"case_id": "case-one", "input": "Add 20 and 22."}]
    recipe = _recipe([{"operation": "normalize", "parameters": {}}])
    await _pin_rebuild_expectation(draft_id, rows, recipe)

    published = await draft_editor.publish_governed(
        draft_id,
        dataset_id="dataset-alpha",
        version_id="version-record",
        name="Alpha",
        recipe=recipe,
    )
    record = published["dataset_version_record"]
    assert published["dataset_version_record_id"] == "version-record"
    assert record["schema_id"] == "dataset-version"
    assert record["content_digest"] == published["content_digest"]
    assert record["policy_digest"] == published["policy_digest"]
    assert record["split_manifest"] == {"test": ["case-one"]}
    assert record["source_lineage"]
    assert record["transformation_recipe_digest"] == content_checksum(recipe)
    assert len(record["contamination_record_digest"]) == 64
    assert record["attribution_bundle_digest"] == published["attribution_digest"]

    stored = await evaluation_records.get_record(
        "dataset-version", "version-record",
    )
    assert stored is not None
    assert stored["dataset_id"] == "dataset-alpha"
    assert stored["content_digest"] == published["content_digest"]
    assert stored["policy_digest"] == published["policy_digest"]
    assert stored["record"] == record
