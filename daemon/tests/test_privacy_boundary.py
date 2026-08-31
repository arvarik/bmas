"""Foundation Stage 0G: privacy classification and object access.

Every field classifies before persistence, prohibited data rejects or
irreversibly redacts, secrets exist as references only, direct
artifact, evidence, and trace lookups repeat the object access check,
and no sensitive value enters an unauthorized trace, export,
artifact, or evidence response.
"""
from __future__ import annotations

import protocol_test_support as support
import pytest

import access_control as access
import database as db
import evidence_service as evidence
import typed_indexes as indexes
from activation_service import persist_protected_artifact
from core.asset_store import (
    AssetBoundaryError,
    DataClass,
    ProhibitedContentError,
    export_view,
    sanitize_record_for_persistence,
)

RUN = support.RUN_ID
FENCE = support.TASK_FENCE

OWNER = access.Principal(
    principal_id="owner-1",
    tenant_id="tenant-default",
    roles=("task_owner",),
)
VIEWER = access.Principal(
    principal_id="viewer-1",
    tenant_id="tenant-default",
    roles=("read_only_viewer",),
)
FOREIGN_OPERATOR = access.Principal(
    principal_id="op-foreign",
    tenant_id="tenant-other",
    roles=("operator",),
)


@pytest.fixture()
async def privacy_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "privacy.db"))
    await db.init_db()
    await support.seed_run()
    return tmp_path


RECORD = {
    "summary": "task summary",
    "customer_name": "Ada Lovelace",
    "api_token": "token-value-123",
    "raw_health_note": "prohibited detail",
}

CLASSES = {
    "summary": DataClass.INTERNAL,
    "customer_name": DataClass.SENSITIVE,
    "api_token": DataClass.SECRET,
    "raw_health_note": DataClass.PROHIBITED,
}


# ── Classification before persistence ────────────────────────────────


def test_every_field_classifies_before_persistence():
    with pytest.raises(AssetBoundaryError):
        sanitize_record_for_persistence(
            {"summary": "x", "unclassified": "y"},
            {"summary": DataClass.INTERNAL},
        )


def test_prohibited_data_never_persists_and_secrets_become_references():
    persistable, protected, secret_refs = sanitize_record_for_persistence(
        RECORD, CLASSES,
    )
    assert "raw_health_note" not in persistable
    assert "raw_health_note" not in protected
    token = persistable["api_token"]
    assert isinstance(token, dict)
    assert "token-value-123" not in str(persistable)
    assert secret_refs
    # The sensitive value moves to the protected store, and the
    # persistable record keeps only a marker.
    assert persistable["customer_name"] == {"protected": True}
    assert protected["customer_name"] == "Ada Lovelace"


def test_exports_contain_no_sensitive_or_secret_values():
    for view_name in ("public", "advanced"):
        view = export_view(RECORD, CLASSES, view=view_name)
        text = str(view)
        assert "Ada Lovelace" not in text
        assert "token-value-123" not in text
        assert "prohibited detail" not in text
    # The advanced view adds internal fields without bypassing policy.
    advanced = export_view(RECORD, CLASSES, view="advanced")
    assert advanced["summary"] == "task summary"
    assert "summary" not in export_view(RECORD, CLASSES, view="public")


async def test_prohibited_content_never_reaches_an_artifact(privacy_db,
                                                            tmp_path):
    store = support.make_store(tmp_path)
    with pytest.raises(ProhibitedContentError):
        persist_protected_artifact(
            store,
            b"prohibited bytes",
            media_type="text/plain",
            access_policy="foundation-raw-response",
            referenced_by="test",
            data_class=DataClass.PROHIBITED,
        )


# ── Role-aware display redaction ─────────────────────────────────────


def test_display_redaction_follows_the_data_classes():
    for principal in (OWNER, VIEWER):
        shown = access.redact_for_display(
            RECORD, CLASSES, principal=principal,
        )
        assert "raw_health_note" not in shown
        assert shown["api_token"] == {"redacted": "secret_reference_only"}
    owner_view = access.redact_for_display(RECORD, CLASSES, principal=OWNER)
    assert owner_view["customer_name"] == "Ada Lovelace"
    viewer_view = access.redact_for_display(
        RECORD, CLASSES, principal=VIEWER,
    )
    assert viewer_view["customer_name"] == {"redacted": "sensitive"}
    with pytest.raises(access.AccessDeniedError):
        access.redact_for_display(
            {"unclassified": 1}, {}, principal=OWNER,
        )


# ── Object-level access on direct lookups ────────────────────────────


async def test_direct_evidence_lookup_repeats_the_access_check(privacy_db):
    await evidence.register_claim(
        run_id=RUN, claim_id="claim-guarded", statement_digest="1" * 64,
        policy=evidence.REGISTERED_POLICIES["deterministic-single"],
        task_fence=FENCE,
    )
    claim = await access.guarded_evidence_lookup(OWNER, "claim-guarded")
    assert claim["claim_id"] == "claim-guarded"
    # A valid identifier crossing a tenant boundary denies.
    with pytest.raises(access.AccessDeniedError) as denial:
        await access.guarded_evidence_lookup(
            FOREIGN_OPERATOR, "claim-guarded",
        )
    assert denial.value.reason == "tenant_boundary"
    # A task-scoped principal cannot read another task's claim.
    scoped = access.Principal(
        principal_id="owner-2",
        tenant_id="tenant-default",
        roles=("task_owner",),
        task_ids=("task-unrelated",),
    )
    with pytest.raises(access.AccessDeniedError) as scope_denial:
        await access.guarded_evidence_lookup(scoped, "claim-guarded")
    assert scope_denial.value.reason == "object_scope"


async def test_direct_artifact_lookup_repeats_the_access_check(
    privacy_db, tmp_path,
):
    store = support.make_store(tmp_path)
    digest = persist_protected_artifact(
        store,
        b"artifact body",
        media_type="text/plain",
        access_policy="foundation-raw-response",
        referenced_by="test",
    )
    stored = access.guarded_artifact_lookup(
        OWNER, store, tenant_id="tenant-default", content_digest=digest,
    )
    assert stored["payload"] == b"artifact body"
    with pytest.raises(access.AccessDeniedError):
        access.guarded_artifact_lookup(
            FOREIGN_OPERATOR,
            store,
            tenant_id="tenant-default",
            content_digest=digest,
        )


async def test_direct_trace_lookup_repeats_the_access_check(privacy_db):
    envelopes = await access.guarded_trace_lookup(
        OWNER, tenant_id="tenant-default", run_id=RUN,
    )
    assert envelopes
    with pytest.raises(access.AccessDeniedError):
        await access.guarded_trace_lookup(
            FOREIGN_OPERATOR, tenant_id="tenant-default", run_id=RUN,
        )


async def test_traces_carry_classification_and_no_sensitive_body(
    privacy_db,
):
    envelopes = await indexes.trace_projection(RUN)
    for envelope in envelopes:
        assert envelope.data_classification in (
            "public", "internal", "sensitive",
        )
        assert envelope.redaction_policy_version
        # The envelope exposes artifact references, never bodies.
        for reference in envelope.protected_artifact_refs:
            assert isinstance(reference, str)
            assert len(reference) == 64
