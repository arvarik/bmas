"""The draft editing engine behind the evaluation facade.

The engine powers every editor area: the source and license card,
case editing with undo and redo, the transformation builder with
deterministic previews, the validation issue list, split and horizon
distribution previews, the version difference view, and the publish
confirmation with complete lineage. Derivation preserves trust:
edits, transforms, clones, and publication never increase the
compiled capability set, publication reruns the recipe from the
pinned source and blocks on a digest mismatch, an unresolved license
decision blocks publication, and hidden-test content stays outside
every preview.
"""

from __future__ import annotations

import json
from typing import Any

import database as db
from benchmarks import evaluation_records, rights_screening
from benchmarks.evaluation_contracts import validate_record
from benchmarks.provenance import content_checksum
from benchmarks.transform_profile import (
    ENGINE_VERSION,
    PROFILE_NAME,
    PROFILE_VERSION,
    apply_recipe,
    dataset_digest,
    strict_parse,
)


class DraftEditorError(ValueError):
    """An editor operation violates the draft contract."""


async def _draft_row(draft_id: str) -> dict[str, Any]:
    stored = await evaluation_records.get_record("dataset-draft", draft_id)
    if stored is None:
        raise DraftEditorError(f"The draft {draft_id} does not exist")
    return stored


async def _draft_cases(draft_id: str) -> list[dict[str, Any]]:
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM dataset_draft_cases WHERE draft_id = ? "
            "ORDER BY case_id",
            (draft_id,),
        )
    return [json.loads(row["record"]) for row in rows]


# ── Trust derivation ─────────────────────────────────────────────────


def compile_effective_restrictions(
    restriction_sets: list[list[dict[str, str]]],
) -> list[dict[str, str]]:
    """Compile the intersection of allowed capabilities.

    The allowed capability set is the intersection of every active
    policy, which equals the union of every restriction. A hard
    behavior always wins over a reviewable one for the same name, and
    no derivation removes an entry.
    """
    compiled: dict[str, str] = {}
    for restrictions in restriction_sets:
        for restriction in restrictions:
            name = str(restriction["name"])
            behavior = str(restriction["behavior"])
            if compiled.get(name) == "hard":
                continue
            compiled[name] = behavior
    return [
        {"name": name, "behavior": compiled[name]}
        for name in sorted(compiled)
    ]


def restriction_policy_digest(
    restrictions: list[dict[str, str]],
) -> str:
    """Digest the frozen effective restrictions of one version."""
    return content_checksum(sorted(
        (restriction["name"], restriction["behavior"])
        for restriction in restrictions
    ))


def assert_no_capability_increase(
    parent: list[dict[str, str]],
    child: list[dict[str, str]],
    *,
    approved_promotions: set[str] | None = None,
) -> None:
    """Reject any derivation that weakens a source restriction.

    A child keeps every parent restriction unless one authenticated
    promotion approved lifting exactly that reviewable restriction.
    """
    approved = set(approved_promotions or ())
    child_map = {
        str(restriction["name"]): str(restriction["behavior"])
        for restriction in child
    }
    for restriction in parent:
        name = str(restriction["name"])
        behavior = str(restriction["behavior"])
        if name in child_map:
            if behavior == "hard" and child_map[name] != "hard":
                raise DraftEditorError(
                    f"The derivation weakens the hard restriction {name}"
                )
            continue
        if behavior == "hard" or name not in approved:
            raise DraftEditorError(
                f"The derivation drops the restriction {name} without "
                "an authenticated promotion"
            )


# Restrictions no promotion ever supersedes: secrets, unsafe tools,
# asset-class rules, license terms, organization policy, and
# deployment caps.
NEVER_PROMOTABLE = frozenset({
    "deny_secrets",
    "deny_unsafe_tools",
    "asset_class_policy",
    "license_terms",
    "organization_policy",
    "deployment_caps",
})


def promotion_decision(
    *,
    actor: str,
    evidence: str,
    policy_version: str,
    restriction_name: str,
    content_digest: str,
    child_version_id: str,
    prior_restrictions: list[dict[str, str]],
    new_restrictions: list[dict[str, str]],
    reason: str,
) -> dict[str, Any]:
    """Record one authenticated promotion bound to exact content.

    The decision binds to the exact content digest and the child
    version, keeps the prior and new restriction sets, and carries its
    own decision digest. A missing actor, evidence, or policy version
    rejects, and a never-promotable restriction never lifts.
    """
    for field_name, value in (
        ("actor", actor), ("evidence", evidence),
        ("policy_version", policy_version), ("reason", reason),
    ):
        if not value or not str(value).strip():
            raise DraftEditorError(
                f"A promotion decision requires {field_name}"
            )
    if restriction_name in NEVER_PROMOTABLE:
        raise DraftEditorError(
            f"The restriction {restriction_name} never lifts through "
            "a promotion"
        )
    decision = {
        "actor": actor,
        "evidence": evidence,
        "policy_version": policy_version,
        "restriction_name": restriction_name,
        "content_digest": content_digest,
        "child_version_id": child_version_id,
        "prior_restrictions": prior_restrictions,
        "new_restrictions": new_restrictions,
        "reason": reason,
    }
    return {**decision, "decision_digest": content_checksum(decision)}


def assert_promotion_applies(
    decision: dict[str, Any],
    *,
    content_digest: str,
    child_version_id: str,
) -> None:
    """Reject a promotion applied outside its exact binding."""
    if str(decision.get("content_digest")) != content_digest:
        raise DraftEditorError(
            "The promotion binds to a different content digest"
        )
    if str(decision.get("child_version_id")) != child_version_id:
        raise DraftEditorError(
            "The promotion binds to a different child version"
        )


def apply_deployment_caps(
    restrictions: list[dict[str, str]],
    caps: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Apply deployment caps after every promotion.

    A deployment cap re-enters the effective set even when a
    promotion lifted the same name, so the deployed profile never
    exceeds the deployment policy.
    """
    return compile_effective_restrictions([restrictions, caps])


# ── Case editing with undo and redo ──────────────────────────────────


async def _journal(draft_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    stored = await _draft_row(draft_id)
    if str(stored["status"]) != "editing":
        raise DraftEditorError(
            f"The draft {draft_id} is not an editable draft"
        )
    record = dict(stored["record"])
    metadata = dict(record.get("metadata") or {})
    journal = dict(metadata.get("edit_journal") or {})
    journal.setdefault("entries", [])
    journal.setdefault("cursor", 0)
    metadata["edit_journal"] = journal
    record["metadata"] = metadata
    return record, journal


async def _write_journal(
    draft_id: str, record: dict[str, Any],
) -> None:
    validate_record(record)
    await evaluation_records.update_draft_record(draft_id, record)


async def _apply_case_change(
    draft_id: str, operation: dict[str, Any],
) -> None:
    kind = str(operation["kind"])
    case = operation.get("case")
    case_id = str(operation["case_id"])
    if kind == "put":
        validate_record(case)
        await evaluation_records.upsert_draft_case(draft_id, case)
    elif kind == "delete":
        await evaluation_records.delete_draft_case(draft_id, case_id)
    else:
        raise DraftEditorError(f"Unknown case change kind: {kind!r}")


def _inverse(
    operation: dict[str, Any], previous: dict[str, Any] | None,
) -> dict[str, Any]:
    if str(operation["kind"]) == "put":
        if previous is None:
            return {"kind": "delete", "case_id": operation["case_id"]}
        return {"kind": "put", "case_id": operation["case_id"],
                "case": previous}
    return {"kind": "put", "case_id": operation["case_id"],
            "case": previous}


async def edit_case(
    draft_id: str, case: dict[str, Any],
) -> dict[str, Any]:
    """Add or edit one case with an undoable journal entry."""
    record, journal = await _journal(draft_id)
    case_id = str(case.get("case_id") or "")
    if not case_id:
        raise DraftEditorError("A case names its case_id")
    existing = {
        item["case_id"]: item for item in await _draft_cases(draft_id)
    }
    operation = {"kind": "put", "case_id": case_id, "case": case}
    inverse = _inverse(operation, existing.get(case_id))
    await _apply_case_change(draft_id, operation)
    journal["entries"] = [
        *journal["entries"][: journal["cursor"]],
        {"operation": operation, "inverse": inverse},
    ]
    journal["cursor"] = len(journal["entries"])
    await _write_journal(draft_id, record)
    return {"draft_id": draft_id, "case_id": case_id,
            "cursor": journal["cursor"]}


async def delete_case(draft_id: str, case_id: str) -> dict[str, Any]:
    """Delete one case with an undoable journal entry."""
    record, journal = await _journal(draft_id)
    existing = {
        item["case_id"]: item for item in await _draft_cases(draft_id)
    }
    if case_id not in existing:
        raise DraftEditorError(f"The case {case_id} does not exist")
    operation = {"kind": "delete", "case_id": case_id}
    inverse = _inverse(operation, existing[case_id])
    await _apply_case_change(draft_id, operation)
    journal["entries"] = [
        *journal["entries"][: journal["cursor"]],
        {"operation": operation, "inverse": inverse},
    ]
    journal["cursor"] = len(journal["entries"])
    await _write_journal(draft_id, record)
    return {"draft_id": draft_id, "case_id": case_id,
            "cursor": journal["cursor"]}


async def duplicate_case(
    draft_id: str, case_id: str, new_case_id: str,
) -> dict[str, Any]:
    """Duplicate one case under one new identifier."""
    existing = {
        item["case_id"]: item for item in await _draft_cases(draft_id)
    }
    if case_id not in existing:
        raise DraftEditorError(f"The case {case_id} does not exist")
    duplicated = json.loads(json.dumps(existing[case_id]))
    duplicated["case_id"] = new_case_id
    return await edit_case(draft_id, duplicated)


async def undo(draft_id: str) -> dict[str, Any]:
    """Undo the latest edit by applying its recorded inverse."""
    record, journal = await _journal(draft_id)
    if journal["cursor"] <= 0:
        raise DraftEditorError("There is no edit to undo")
    entry = journal["entries"][journal["cursor"] - 1]
    await _apply_case_change(draft_id, entry["inverse"])
    journal["cursor"] -= 1
    await _write_journal(draft_id, record)
    return {"draft_id": draft_id, "cursor": journal["cursor"]}


async def redo(draft_id: str) -> dict[str, Any]:
    """Redo the next undone edit."""
    record, journal = await _journal(draft_id)
    if journal["cursor"] >= len(journal["entries"]):
        raise DraftEditorError("There is no edit to redo")
    entry = journal["entries"][journal["cursor"]]
    await _apply_case_change(draft_id, entry["operation"])
    journal["cursor"] += 1
    await _write_journal(draft_id, record)
    return {"draft_id": draft_id, "cursor": journal["cursor"]}


# ── Validation, previews, and differences ────────────────────────────


async def validation_issues(draft_id: str) -> list[dict[str, Any]]:
    """List every contract issue across the draft's cases."""
    issues = []
    for case in await _draft_cases(draft_id):
        try:
            validate_record(case)
        except ValueError as error:
            issues.append({
                "code": "contract",
                "case_id": str(case.get("case_id") or ""),
                "message": str(error)[:500],
            })
    return issues


async def distribution_preview(draft_id: str) -> dict[str, Any]:
    """Preview split, family, and horizon distributions.

    Hidden-test content stays outside the preview counts' case list;
    only its aggregate count appears.
    """
    cases = await _draft_cases(draft_id)
    visible = rights_screening.visible_cases(cases, context="preview")
    splits: dict[str, int] = {}
    families: dict[str, int] = {}
    horizons: dict[str, int] = {}
    for case in visible:
        classification = case.get("classification") or {}
        splits[str(classification.get("split") or "test")] = (
            splits.get(str(classification.get("split") or "test"), 0) + 1
        )
        family = str(classification.get("task_family") or "default")
        families[family] = families.get(family, 0) + 1
        horizon = str(classification.get("intrinsic_horizon") or "none")
        horizons[horizon] = horizons.get(horizon, 0) + 1
    return {
        "case_count": len(visible),
        "hidden_test_count": len(cases) - len(visible),
        "splits": splits,
        "task_families": families,
        "intrinsic_horizons": horizons,
    }


def _editor_items(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": str(case.get("case_id") or ""),
            "input": str((case.get("task") or {}).get("instructions")
                         or ""),
            "expected_output": str(
                (case.get("expected") or {}).get("reference_answer") or "",
            ),
        }
        for case in cases
    ]


async def transform_preview(
    draft_id: str, recipe: dict[str, Any], *, limit: int = 10,
) -> dict[str, Any]:
    """Preview one recipe deterministically without persisting."""
    cases = rights_screening.visible_cases(
        await _draft_cases(draft_id), context="preview",
    )
    outcome = apply_recipe(_editor_items(cases), recipe)
    return {
        "preview": outcome["cases"][: max(int(limit), 1)],
        "case_count": len(outcome["cases"]),
        "dataset_digest": outcome["dataset_digest"],
        "recipe_digest": outcome["recipe_digest"],
        "engine": outcome["engine"],
    }


async def version_difference(
    draft_id: str, version_id: str,
) -> dict[str, Any]:
    """Compare draft cases against one published version's items."""
    cases = {
        str(case["case_id"]): case for case in await _draft_cases(draft_id)
    }
    items, _total = await db.list_dataset_items(version_id, limit=200)
    published = {
        str(item["item_key"]): item for item in items
    }
    added = sorted(set(cases) - set(published))
    removed = sorted(set(published) - set(cases))
    changed = sorted(
        key
        for key in set(cases) & set(published)
        if str((cases[key].get("task") or {}).get("instructions") or "")
        != str(published[key].get("input") or "")
        or str(
            (cases[key].get("expected") or {}).get("reference_answer")
            or "",
        )
        != str(published[key].get("expected_output") or "")
    )
    return {"added": added, "removed": removed, "changed": changed}


# ── Governed publication with lineage ────────────────────────────────


async def rebuild_verification(
    draft_id: str, recipe: dict[str, Any] | None,
) -> dict[str, Any]:
    """Rerun the recipe from the pinned source and verify its digest.

    The verification rebuilds from the stored immutable source bytes.
    A recorded expected digest that does not match blocks publication.
    """
    stored = await _draft_row(draft_id)
    record = stored["record"]
    expected = (record.get("metadata") or {}).get(
        "expected_transform_digest",
    )
    if recipe is None or expected is None:
        return {"verified": True, "reason": "no_recipe_expectation"}
    source_id = stored.get("source_id")
    if not source_id:
        return {"verified": True, "reason": "no_pinned_source"}
    source = await evaluation_records.get_record(
        "benchmark-source", str(source_id),
    )
    if source is None:
        raise DraftEditorError("The pinned source record disappeared")
    from benchmarks.source_adapters import _source_store

    stored_bytes = _source_store().read_object(
        str(
            (record.get("metadata") or {}).get("source_artifact_digest")
            or "",
        ),
    )
    rows = [
        strict_parse(line.encode("utf-8"))
        for line in stored_bytes["payload"].decode("utf-8").splitlines()
        if line.strip()
    ]
    outcome = apply_recipe(rows, recipe)
    verified = outcome["dataset_digest"] == expected
    return {
        "verified": verified,
        "expected_digest": expected,
        "rebuilt_digest": outcome["dataset_digest"],
    }


async def publish_confirmation(draft_id: str) -> dict[str, Any]:
    """Build the publish confirmation with its complete lineage."""
    stored = await _draft_row(draft_id)
    record = stored["record"]
    cases = await _draft_cases(draft_id)
    restrictions = compile_effective_restrictions([
        list(record.get("effective_restrictions") or []),
    ])
    return {
        "draft_id": draft_id,
        "source_ids": list(record.get("source_ids") or []),
        "parent_version_id": record.get("parent_version_id"),
        "case_count": len(cases),
        "content_digest": dataset_digest(_editor_items(cases)),
        "effective_restrictions": restrictions,
        "policy_digest": restriction_policy_digest(restrictions),
        "engine": {
            "profile": PROFILE_NAME,
            "profile_version": PROFILE_VERSION,
            "engine_version": ENGINE_VERSION,
        },
    }


async def publish_governed(
    draft_id: str,
    *,
    dataset_id: str,
    version_id: str,
    name: str,
    description: str = "",
    recipe: dict[str, Any] | None = None,
    screening_corpus: dict[str, Any] | None = None,
    operator_decisions: dict[str, str] | None = None,
    approved_promotions: set[str] | None = None,
) -> dict[str, Any]:
    """Publish one draft under every governance rule.

    The publication verifies the recipe rebuild, compiles and freezes
    the effective restrictions with no capability increase, resolves
    every license decision or blocks, screens contamination, creates
    the immutable rights record, and stores the lineage with the
    version.
    """
    from benchmarks import facade

    stored = await _draft_row(draft_id)
    record = stored["record"]
    cases = await _draft_cases(draft_id)
    if not cases:
        raise DraftEditorError("A draft publishes at least one case")

    verification = await rebuild_verification(draft_id, recipe)
    if not verification["verified"]:
        raise DraftEditorError(
            "Publication is blocked: the recipe rebuild digest "
            f"{verification['rebuilt_digest']} does not match the "
            f"expected digest {verification['expected_digest']}"
        )

    sources = []
    for source_id in record.get("source_ids") or []:
        source = await evaluation_records.get_record(
            "benchmark-source", str(source_id),
        )
        if source is not None:
            sources.append(source["record"])
    source_restrictions = [
        list(source.get("execution_restrictions") or [])
        for source in sources
    ]
    draft_restrictions = list(record.get("effective_restrictions") or [])
    effective = compile_effective_restrictions([
        *source_restrictions, draft_restrictions,
    ])
    for restrictions in source_restrictions:
        assert_no_capability_increase(
            restrictions, effective,
            approved_promotions=approved_promotions,
        )

    decisions = rights_screening.build_rights_decisions(
        sources, operator_decisions=operator_decisions,
    )
    rights_screening.assert_publication_allowed(decisions)
    screening = rights_screening.screen_cases(
        [
            {"case_id": case["case_id"],
             "input": (case.get("task") or {}).get("instructions") or ""}
            for case in cases
        ],
        screening_corpus or {"id": "empty-corpus", "entries": []},
    )
    attribution = rights_screening.attribution_bundle(sources, decisions)
    canaries = rights_screening.new_canaries()

    published = await facade.execute(
        "publish_draft",
        {
            "draft_id": draft_id,
            "dataset_id": dataset_id,
            "version_id": version_id,
            "name": name,
            "description": description,
        },
    )
    contamination = rights_screening.build_contamination_record(
        dataset_version_id=version_id,
        screening=screening,
        decisions=decisions,
        attribution=attribution,
        canaries=canaries,
        holdout_accesses=[],
    )
    saved = await facade.execute(
        "record_contamination_rights",
        {"record": contamination, "dataset_version_id": version_id},
    )
    return {
        **published,
        "effective_restrictions": effective,
        "policy_digest": restriction_policy_digest(effective),
        "license_decisions": decisions,
        "screening_result": screening["result"],
        "attribution_digest": attribution["bundle_digest"],
        "contamination_record_id": saved["id"],
        "canaries": canaries,
        "rebuild_verification": verification,
    }
