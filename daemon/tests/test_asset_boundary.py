"""Foundation Stage 0C: asset manifests, staged artifacts, and privacy.

The manifest is immutable and path-free, every asset read checks task
access, and staged artifact writes promote atomically to one immutable
content-addressed path per tenant storage domain.
"""
from __future__ import annotations

import dataclasses

import pytest

from core.asset_store import (
    ArtifactCommitError,
    ArtifactQuarantineError,
    ArtifactStore,
    ArtifactValidationError,
    AssetAccessDeniedError,
    AssetBoundaryError,
    AssetCatalog,
    AssetManifest,
    AssetManifestEntry,
    DataClass,
    ProhibitedContentError,
    RetentionClass,
    TrustLevel,
    export_view,
    sanitize_record_for_persistence,
)
from core.digest_profile import digest_bytes


def build_entry(asset_id: str = "asset-brief") -> AssetManifestEntry:
    return AssetManifestEntry(
        asset_id=asset_id,
        content_digest="1" * 64,
        size_bytes=2048,
        media_type="text/markdown",
        source="user-upload",
        data_class=DataClass.INTERNAL,
        trust_level=TrustLevel.UNTRUSTED,
        access_policy="task-scope",
        scanner_version="1",
        extraction_version="1",
    )


def build_manifest(task_id: str = "task-assets") -> AssetManifest:
    return AssetManifest(
        manifest_id="manifest-assets",
        task_id=task_id,
        entries=(build_entry(),),
    )


CLASSIFICATIONS = {
    "title": DataClass.PUBLIC,
    "routing_notes": DataClass.INTERNAL,
    "customer_contact": DataClass.SENSITIVE,
    "provider_token": DataClass.SECRET,
    "raw_payment_card": DataClass.PROHIBITED,
}

RECORD = {
    "title": "Weekly summary",
    "routing_notes": "prefer the fast pool",
    "customer_contact": "person@example.com",
    "provider_token": "token-value",
    "raw_payment_card": "4111-1111",
}


def test_prohibited_content_never_reaches_persistence():
    persistable, protected, secret_refs = sanitize_record_for_persistence(
        RECORD, CLASSIFICATIONS,
    )
    for structure in (persistable, protected, secret_refs):
        assert "4111-1111" not in str(structure)
    assert "raw_payment_card" not in persistable


def test_secret_values_become_references_only():
    persistable, protected, secret_refs = sanitize_record_for_persistence(
        RECORD, CLASSIFICATIONS,
    )
    assert persistable["provider_token"].keys() == {"secret_ref"}
    assert "token-value" not in str(persistable)
    assert "token-value" not in str(protected)
    reference = persistable["provider_token"]["secret_ref"]
    assert secret_refs[reference] == "provider_token"


def test_sensitive_values_use_the_declared_protected_storage():
    persistable, protected, _ = sanitize_record_for_persistence(
        RECORD, CLASSIFICATIONS,
    )
    assert persistable["customer_contact"] == {"protected": True}
    assert protected == {"customer_contact": "person@example.com"}


def test_public_exports_omit_internal_and_sensitive_fields():
    public = export_view(RECORD, CLASSIFICATIONS, view="public")
    assert public == {"title": "Weekly summary"}


def test_advanced_panels_do_not_bypass_redaction():
    advanced = export_view(RECORD, CLASSIFICATIONS, view="advanced")
    assert advanced == {
        "title": "Weekly summary",
        "routing_notes": "prefer the fast pool",
    }
    for value in ("person@example.com", "token-value", "4111-1111"):
        assert value not in str(advanced)
    with pytest.raises(AssetBoundaryError):
        export_view(RECORD, CLASSIFICATIONS, view="everything")


def test_an_unclassified_field_fails_closed():
    with pytest.raises(AssetBoundaryError):
        export_view({"surprise": 1}, {}, view="public")
    with pytest.raises(AssetBoundaryError):
        sanitize_record_for_persistence({"surprise": 1}, {})


def test_cross_task_asset_reads_are_denied():
    catalog = AssetCatalog(build_manifest("task-assets"))
    handle = catalog.handle_for("task-assets", "asset-brief")
    assert handle.asset_id == "asset-brief"
    assert not hasattr(handle, "path")
    with pytest.raises(AssetAccessDeniedError):
        catalog.handle_for("task-other", "asset-brief")
    with pytest.raises(AssetAccessDeniedError):
        catalog.handle_for("task-assets", "asset-unlisted")


def test_the_manifest_is_immutable_path_free_and_stable():
    manifest = build_manifest()
    digest = manifest.digest()
    # The digest is stable across queue, recovery, and blueprint reads.
    for _ in range(3):
        assert build_manifest().digest() == digest
    assert "path" not in str(sorted(manifest.to_dict()["entries"][0]))
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.entries = ()  # type: ignore[misc]
    with pytest.raises(ProhibitedContentError):
        AssetManifest(
            manifest_id="manifest-bad",
            task_id="task-assets",
            entries=(
                AssetManifestEntry(
                    asset_id="asset-toxic",
                    content_digest="2" * 64,
                    size_bytes=1,
                    media_type="text/plain",
                    source="user-upload",
                    data_class=DataClass.PROHIBITED,
                    trust_level=TrustLevel.UNTRUSTED,
                    access_policy="task-scope",
                    scanner_version="1",
                    extraction_version="1",
                ),
            ),
        )
    with pytest.raises(AssetBoundaryError, match="Duplicate"):
        AssetManifest(
            manifest_id="manifest-dup",
            task_id="task-assets",
            entries=(build_entry(), build_entry()),
        )


def test_retention_classes_are_the_registered_set():
    assert [item.value for item in RetentionClass] == [
        "ephemeral",
        "diagnostic",
        "replay_required",
        "evidence_required",
        "legal_hold",
    ]


def stage_payload(store: ArtifactStore, payload: bytes, **overrides):
    digest = digest_bytes("artifact-content", payload)
    arguments = dict(
        declared_digest=digest,
        declared_size=len(payload),
        media_type="text/plain",
        scanner_result="clean",
        data_class=DataClass.INTERNAL,
        access_policy="task-scope",
        retention_class=RetentionClass.DIAGNOSTIC,
    )
    arguments.update(overrides)
    return store.stage(payload, **arguments)


def test_staged_writes_promote_to_immutable_digest_paths(tmp_path):
    store = ArtifactStore(tmp_path, "tenant-a")
    payload = b"artifact body"

    # A commit before promotion cannot reference missing bytes.
    staged = stage_payload(store, payload)
    with pytest.raises(ArtifactCommitError):
        store.commit_reference(
            staged.content_digest, referenced_by="journal:1",
        )

    promoted = store.promote(staged)
    assert promoted == staged.content_digest
    assert store.has_object(promoted)
    store.commit_reference(promoted, referenced_by="journal:1")
    assert store.read_object(promoted)["payload"] == payload

    # Promotion of equal bytes deduplicates inside this tenant.
    duplicate = stage_payload(store, payload)
    assert store.promote(duplicate) == promoted


def test_validation_rejects_bad_declarations(tmp_path):
    store = ArtifactStore(tmp_path, "tenant-a")
    payload = b"artifact body"
    good_digest = digest_bytes("artifact-content", payload)
    with pytest.raises(ArtifactValidationError):
        stage_payload(store, payload, declared_digest="9" * 64)
    with pytest.raises(ArtifactValidationError):
        stage_payload(store, payload, declared_size=1)
    with pytest.raises(ArtifactValidationError):
        stage_payload(store, payload, media_type="plaintext")
    with pytest.raises(ArtifactValidationError):
        stage_payload(store, payload, scanner_result="malware-found")
    with pytest.raises(ProhibitedContentError):
        stage_payload(store, payload, data_class=DataClass.PROHIBITED)
    # Nothing was persisted by the rejected attempts.
    assert not store.has_object(good_digest)


def test_a_digest_collision_quarantines_and_stops_the_run(tmp_path):
    store = ArtifactStore(tmp_path, "tenant-a")
    payload = b"original bytes"
    staged = stage_payload(store, payload)
    promoted = store.promote(staged)

    # Different bytes claim the same digest through a forged declaration.
    forged = stage_payload(store, b"different bytes")
    object.__setattr__(forged, "content_digest", promoted)
    with pytest.raises(ArtifactQuarantineError):
        store.promote(forged)
    # The original bytes stay immutable, and the digest is quarantined.
    assert store.read_object(promoted)["payload"] == payload
    with pytest.raises(ArtifactQuarantineError):
        store.commit_reference(promoted, referenced_by="journal:2")


def test_the_orphan_sweep_waits_and_keeps_every_reference(tmp_path):
    store = ArtifactStore(tmp_path, "tenant-a")
    kept_kinds = {
        "journal": stage_payload(store, b"journal ref"),
        "snapshot": stage_payload(store, b"snapshot ref"),
        "legal": stage_payload(store, b"legal hold ref"),
        "staging": stage_payload(store, b"staging ref"),
        "committed": stage_payload(store, b"committed ref"),
    }
    digests = {
        name: store.promote(staged) for name, staged in kept_kinds.items()
    }
    store.commit_reference(digests["committed"], referenced_by="journal:3")
    orphan = store.promote(stage_payload(store, b"orphan bytes"))

    ages = dict.fromkeys(digests.values(), 9_999.0)
    # The sweep waits through the grace period.
    young = store.sweep_orphans(
        grace_seconds=600.0,
        age_seconds={**ages, orphan: 30.0},
        journal_refs=frozenset({digests["journal"]}),
        snapshot_refs=frozenset({digests["snapshot"]}),
        legal_hold_refs=frozenset({digests["legal"]}),
        staging_refs=frozenset({digests["staging"]}),
    )
    assert young == []
    assert store.has_object(orphan)

    # After the grace period, only the unreferenced object disappears.
    swept = store.sweep_orphans(
        grace_seconds=600.0,
        age_seconds={**ages, orphan: 700.0},
        journal_refs=frozenset({digests["journal"]}),
        snapshot_refs=frozenset({digests["snapshot"]}),
        legal_hold_refs=frozenset({digests["legal"]}),
        staging_refs=frozenset({digests["staging"]}),
    )
    assert swept == [orphan]
    for digest in digests.values():
        assert store.has_object(digest)


def test_tenant_scopes_share_no_existence_signal(tmp_path):
    first = ArtifactStore(tmp_path, "tenant-a")
    second = ArtifactStore(tmp_path, "tenant-b")
    payload = b"identical bytes"
    promoted = first.promote(stage_payload(first, payload))

    # Equal bytes in another tenant scope resolve separately.
    assert second.has_object(promoted) is False
    with pytest.raises(ArtifactCommitError):
        second.commit_reference(promoted, referenced_by="journal:4")
    second_promoted = second.promote(stage_payload(second, payload))
    assert second_promoted == promoted
    assert first.read_object(promoted)["payload"] == payload
    assert second.read_object(second_promoted)["payload"] == payload


def test_legal_erasure_leaves_a_durable_record_and_redacted_replay(tmp_path):
    store = ArtifactStore(tmp_path, "tenant-a")
    staged = stage_payload(
        store, b"replay body", retention_class=RetentionClass.REPLAY_REQUIRED,
    )
    promoted = store.promote(staged)
    store.commit_reference(promoted, referenced_by="journal:5")

    record = store.erase(
        promoted,
        authority_id="authority-privacy-office",
        reason="erasure request",
        erased_at="2026-09-01T00:00:00.000Z",
    )
    assert record.content_digest == promoted
    assert store.has_object(promoted) is False
    replayed = store.read_object(promoted)
    assert replayed == {
        "redacted": True,
        "reason": "legal_erasure",
        "erased_at": "2026-09-01T00:00:00.000Z",
    }
