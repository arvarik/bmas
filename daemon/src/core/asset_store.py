"""Foundation Stage 0C: immutable asset manifests and staged artifacts.

An asset manifest freezes the authorized inputs of one run before
queue admission. Runtimes receive scoped handles only; server paths
stay outside the public contract.

Artifacts write in two steps. The store stages new bytes on the target
filesystem, validates them, and promotes them to an immutable
content-addressed path with an atomic rename. A reference commits only
after the immutable bytes exist. Each physical content address scopes
to one tenant storage domain.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from core.digest_profile import digest_bytes, digest_hex

ASSET_MANIFEST_DIGEST_DOMAIN = "asset-manifest"
ARTIFACT_CONTENT_DIGEST_DOMAIN = "artifact-content"
ASSET_MANIFEST_SCHEMA_VERSION = "1"


class AssetBoundaryError(ValueError):
    """An asset or artifact boundary rule was violated."""


class AssetAccessDeniedError(AssetBoundaryError):
    """The requested asset is outside the caller's task scope."""


class ProhibitedContentError(AssetBoundaryError):
    """Prohibited content never reaches persistence."""


class ArtifactValidationError(AssetBoundaryError):
    """The staged artifact failed validation before promotion."""


class ArtifactQuarantineError(AssetBoundaryError):
    """One content digest resolved to different bytes. Stop the run."""


class ArtifactCommitError(AssetBoundaryError):
    """A reference cannot commit before its immutable bytes exist."""


class DataClass(StrEnum):
    """The registered data classifications."""

    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"
    PROHIBITED = "prohibited"


class TrustLevel(StrEnum):
    """The registered source trust levels."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    QUARANTINED = "quarantined"


class RetentionClass(StrEnum):
    """The immutable artifact retention classes."""

    EPHEMERAL = "ephemeral"
    DIAGNOSTIC = "diagnostic"
    REPLAY_REQUIRED = "replay_required"
    EVIDENCE_REQUIRED = "evidence_required"
    LEGAL_HOLD = "legal_hold"


@dataclass(frozen=True)
class AssetManifestEntry:
    """One immutable manifest entry. The entry carries no server path."""

    asset_id: str
    content_digest: str
    size_bytes: int
    media_type: str
    source: str
    data_class: DataClass
    trust_level: TrustLevel
    access_policy: str
    scanner_version: str
    extraction_version: str
    redacted_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "source": self.source,
            "data_class": self.data_class.value,
            "trust_level": self.trust_level.value,
            "access_policy": self.access_policy,
            "scanner_version": self.scanner_version,
            "extraction_version": self.extraction_version,
            "redacted_refs": list(self.redacted_refs),
        }


@dataclass(frozen=True)
class AssetManifest:
    """The immutable authorized asset set of one run."""

    manifest_id: str
    task_id: str
    entries: tuple[AssetManifestEntry, ...]

    def __post_init__(self) -> None:
        if not self.manifest_id or not self.task_id:
            raise AssetBoundaryError(
                "An asset manifest names its identifier and task"
            )
        seen: set[str] = set()
        for entry in self.entries:
            if entry.asset_id in seen:
                raise AssetBoundaryError(
                    f"Duplicate manifest asset: {entry.asset_id}"
                )
            seen.add(entry.asset_id)
            if entry.data_class is DataClass.PROHIBITED:
                raise ProhibitedContentError(
                    "Prohibited content cannot enter an asset manifest"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "asset_manifest_schema_version": ASSET_MANIFEST_SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def digest(self) -> str:
        """Return the manifest digest under the bmas-digest profile."""
        return digest_hex(ASSET_MANIFEST_DIGEST_DOMAIN, self.to_dict())


@dataclass(frozen=True)
class AssetHandle:
    """One scoped asset handle. The handle exposes no server path."""

    handle_id: str
    asset_id: str
    task_id: str
    media_type: str
    data_class: DataClass


class AssetCatalog:
    """Serve scoped asset handles with a task-access check on every read."""

    def __init__(self, manifest: AssetManifest) -> None:
        self._manifest = manifest
        self._entries = {
            entry.asset_id: entry for entry in manifest.entries
        }

    def handle_for(self, task_id: str, asset_id: str) -> AssetHandle:
        """Return one scoped handle after the task-access check."""
        if task_id != self._manifest.task_id:
            raise AssetAccessDeniedError(
                "The asset belongs to another task scope"
            )
        entry = self._entries.get(asset_id)
        if entry is None:
            raise AssetAccessDeniedError(
                f"The manifest does not authorize asset {asset_id!r}"
            )
        return AssetHandle(
            handle_id=f"asset-handle-{uuid.uuid4()}",
            asset_id=entry.asset_id,
            task_id=task_id,
            media_type=entry.media_type,
            data_class=entry.data_class,
        )


# ── Field classification and redaction ───────────────────────────────────


def sanitize_record_for_persistence(
    record: dict[str, Any],
    classifications: dict[str, DataClass],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Split one record by data class before persistence.

    Prohibited fields never reach any returned structure. Secret values
    become opaque references. Sensitive values move to the declared
    protected storage. The first result is safe to persist, the second
    is the protected store content, and the third maps secret
    references to their field names.
    """
    persistable: dict[str, Any] = {}
    protected: dict[str, Any] = {}
    secret_refs: dict[str, str] = {}
    for name, value in record.items():
        data_class = classifications.get(name)
        if data_class is None:
            raise AssetBoundaryError(f"Field {name!r} has no data class")
        if data_class is DataClass.PROHIBITED:
            continue
        if data_class is DataClass.SECRET:
            reference = f"secret-ref-{uuid.uuid4()}"
            secret_refs[reference] = name
            persistable[name] = {"secret_ref": reference}
            continue
        if data_class is DataClass.SENSITIVE:
            persistable[name] = {"protected": True}
            protected[name] = value
            continue
        persistable[name] = value
    return persistable, protected, secret_refs


def export_view(
    record: dict[str, Any],
    classifications: dict[str, DataClass],
    *,
    view: str = "public",
) -> dict[str, Any]:
    """Return one redacted export view.

    The public view holds public fields only. The advanced view adds
    internal fields but never bypasses redaction of sensitive, secret,
    or prohibited fields.
    """
    if view not in ("public", "advanced"):
        raise AssetBoundaryError(f"Unknown export view: {view!r}")
    allowed = {DataClass.PUBLIC}
    if view == "advanced":
        allowed.add(DataClass.INTERNAL)
    result: dict[str, Any] = {}
    for name, value in record.items():
        data_class = classifications.get(name)
        if data_class is None:
            raise AssetBoundaryError(f"Field {name!r} has no data class")
        if data_class in allowed:
            result[name] = value
    return result


# ── Staged artifact store ────────────────────────────────────────────────


@dataclass(frozen=True)
class StagedArtifact:
    """One staged artifact before validation and promotion."""

    staging_id: str
    content_digest: str
    size_bytes: int
    media_type: str
    data_class: DataClass
    retention_class: RetentionClass
    access_policy: str


@dataclass
class ArtifactErasureRecord:
    """The durable record of one legal erasure decision."""

    content_digest: str
    authority_id: str
    reason: str
    erased_at: str


@dataclass
class _StoreState:
    committed_refs: dict[str, set[str]] = field(default_factory=dict)
    quarantined: set[str] = field(default_factory=set)
    erasures: dict[str, ArtifactErasureRecord] = field(default_factory=dict)


class ArtifactStore:
    """One tenant-scoped content-addressed artifact store.

    Each physical content address scopes to this tenant's storage
    domain. The store never signals whether another tenant holds equal
    bytes.
    """

    def __init__(self, root: Path, tenant_id: str) -> None:
        self._tenant_id = tenant_id
        self._root = Path(root) / tenant_id
        self._staging_dir = self._root / "staging"
        self._objects_dir = self._root / "objects"
        self._quarantine_dir = self._root / "quarantine"
        for directory in (
            self._staging_dir, self._objects_dir, self._quarantine_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._staged: dict[str, Path] = {}
        self._state = _StoreState()

    def _object_path(self, content_digest: str) -> Path:
        return self._objects_dir / content_digest[:2] / content_digest

    def stage(
        self,
        payload: bytes,
        *,
        declared_digest: str,
        declared_size: int,
        media_type: str,
        scanner_result: str,
        data_class: DataClass,
        access_policy: str,
        retention_class: RetentionClass,
    ) -> StagedArtifact:
        """Stage and validate one new artifact on the target filesystem."""
        if data_class is DataClass.PROHIBITED:
            raise ProhibitedContentError(
                "Prohibited content never reaches persistence"
            )
        content_digest = digest_bytes(
            ARTIFACT_CONTENT_DIGEST_DOMAIN, payload,
        )
        if declared_digest != content_digest:
            raise ArtifactValidationError(
                "The declared content digest does not match the bytes"
            )
        if declared_size != len(payload):
            raise ArtifactValidationError(
                "The declared size does not match the bytes"
            )
        if "/" not in media_type:
            raise ArtifactValidationError(
                f"Invalid media type: {media_type!r}"
            )
        if scanner_result != "clean":
            raise ArtifactValidationError(
                f"The scanner rejected the artifact: {scanner_result!r}"
            )
        if not access_policy:
            raise ArtifactValidationError(
                "An artifact names its access policy"
            )
        staging_id = f"staging-{uuid.uuid4()}"
        staged_path = self._staging_dir / staging_id
        with open(staged_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        self._staged[staging_id] = staged_path
        return StagedArtifact(
            staging_id=staging_id,
            content_digest=content_digest,
            size_bytes=len(payload),
            media_type=media_type,
            data_class=data_class,
            retention_class=retention_class,
            access_policy=access_policy,
        )

    def promote(self, staged: StagedArtifact) -> str:
        """Promote staged bytes to the immutable content-addressed path.

        The promotion uses an atomic rename on the same filesystem and
        syncs the file and its directory. An existing digest with
        different bytes moves the new bytes to quarantine and stops the
        run.
        """
        staged_path = self._staged.get(staged.staging_id)
        if staged_path is None or not staged_path.is_file():
            raise ArtifactValidationError(
                f"Unknown staged artifact: {staged.staging_id}"
            )
        target = self._object_path(staged.content_digest)
        if target.is_file():
            if target.read_bytes() != staged_path.read_bytes():
                quarantine_path = (
                    self._quarantine_dir / staged.staging_id
                )
                os.replace(staged_path, quarantine_path)
                self._state.quarantined.add(staged.content_digest)
                self._staged.pop(staged.staging_id, None)
                raise ArtifactQuarantineError(
                    "One content digest resolved to different bytes. "
                    "The new bytes are quarantined; stop the run."
                )
            staged_path.unlink()
            self._staged.pop(staged.staging_id, None)
            return staged.content_digest
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_path, target)
        with open(target, "rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self._staged.pop(staged.staging_id, None)
        return staged.content_digest

    def commit_reference(
        self, content_digest: str, *, referenced_by: str,
    ) -> None:
        """Commit one reference only after the immutable bytes exist."""
        if content_digest in self._state.quarantined:
            raise ArtifactQuarantineError(
                f"The digest {content_digest} is quarantined"
            )
        if not self._object_path(content_digest).is_file():
            raise ArtifactCommitError(
                "A reference cannot commit before its immutable bytes exist"
            )
        self._state.committed_refs.setdefault(
            content_digest, set(),
        ).add(referenced_by)

    def has_object(self, content_digest: str) -> bool:
        """Report existence inside this tenant storage domain only."""
        return self._object_path(content_digest).is_file()

    def read_object(self, content_digest: str) -> dict[str, Any]:
        """Read one committed object or its policy-redacted erasure."""
        erasure = self._state.erasures.get(content_digest)
        if erasure is not None:
            return {
                "redacted": True,
                "reason": "legal_erasure",
                "erased_at": erasure.erased_at,
            }
        path = self._object_path(content_digest)
        if not path.is_file():
            raise ArtifactCommitError(
                f"Unknown artifact object: {content_digest}"
            )
        return {"redacted": False, "payload": path.read_bytes()}

    def erase(
        self,
        content_digest: str,
        *,
        authority_id: str,
        reason: str,
        erased_at: str,
    ) -> ArtifactErasureRecord:
        """Erase one object through a legal decision.

        The bytes disappear, and a durable erasure record replaces
        them. A later replay returns the policy-redacted result.
        """
        path = self._object_path(content_digest)
        if not path.is_file():
            raise ArtifactCommitError(
                f"Unknown artifact object: {content_digest}"
            )
        record = ArtifactErasureRecord(
            content_digest=content_digest,
            authority_id=authority_id,
            reason=reason,
            erased_at=erased_at,
        )
        self._state.erasures[content_digest] = record
        path.unlink()
        return record

    def health_report(
        self,
        *,
        journal_refs: frozenset[str] = frozenset(),
    ) -> dict[str, list[str]]:
        """Report unhealthy artifact objects for the Recovery Center.

        Missing: a committed reference without stored bytes. Corrupt:
        stored bytes whose digest no longer matches the address.
        Quarantined: bytes moved aside after a digest collision.
        Orphans: stored objects that no committed or journal
        reference names.
        """
        from core.digest_profile import digest_bytes as _digest_bytes

        referenced = set(self._state.committed_refs) | set(journal_refs)
        missing = sorted(
            digest
            for digest in self._state.committed_refs
            if not self._object_path(digest).is_file()
            and digest not in self._state.erasures
        )
        corrupt: list[str] = []
        orphans: list[str] = []
        if self._objects_dir.is_dir():
            for shard in sorted(self._objects_dir.iterdir()):
                for path in sorted(shard.iterdir()):
                    stored_digest = path.name
                    actual = _digest_bytes(
                        ARTIFACT_CONTENT_DIGEST_DOMAIN, path.read_bytes(),
                    )
                    if actual != stored_digest:
                        corrupt.append(stored_digest)
                    if stored_digest not in referenced:
                        orphans.append(stored_digest)
        quarantined = sorted(
            path.name for path in self._quarantine_dir.iterdir()
        )
        return {
            "missing": missing,
            "corrupt": corrupt,
            "quarantined": quarantined,
            "orphans": orphans,
        }

    def sweep_orphans(
        self,
        *,
        grace_seconds: float,
        age_seconds: dict[str, float],
        journal_refs: frozenset[str] = frozenset(),
        snapshot_refs: frozenset[str] = frozenset(),
        legal_hold_refs: frozenset[str] = frozenset(),
        staging_refs: frozenset[str] = frozenset(),
    ) -> list[str]:
        """Delete unreferenced objects after the grace period.

        The sweep keeps every object that a journal record, a snapshot,
        a legal hold, an active staging row, or a committed reference
        still names.
        """
        retained = (
            journal_refs
            | snapshot_refs
            | legal_hold_refs
            | staging_refs
            | frozenset(self._state.committed_refs)
        )
        deleted: list[str] = []
        for shard in sorted(self._objects_dir.iterdir()):
            for path in sorted(shard.iterdir()):
                content_digest = path.name
                if content_digest in retained:
                    continue
                if age_seconds.get(content_digest, 0.0) < grace_seconds:
                    continue
                path.unlink()
                deleted.append(content_digest)
        return deleted
