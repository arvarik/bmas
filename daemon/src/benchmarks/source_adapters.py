"""The trusted registry of public source adapters.

Each adapter implements one small trusted interface: resolve a pinned
source, list configurations and splits, preview normalized records,
import records, and report license, citation, checksums, adapter
version, trust level, policy version, and the default execution
capability profile. The registry enforces the source trust policy
before any preview or import, every import persists immutable source
bytes with complete provenance and rights metadata through the one
facade, and dataset text stays data through the whole path: no field
of it ever becomes an instruction, a capability, or a secret.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import database as db
from benchmarks.datasets import validate_dataset
from benchmarks.import_worker import SafeFetcher
from benchmarks.provenance import content_checksum

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

SOURCE_TRUST_POLICY_VERSION = "1"

# The identity column mapping for sources that already use the
# canonical field names.
DEFAULT_FIELD_MAPPING = {
    "id": "id",
    "input": "input",
    "expected_output": "expected_output",
    "subject": "subject",
    "split": "split",
    "tags": "tags",
}

# The default execution capability profile per trust level. Every
# restriction declares hard or reviewable override behavior, and the
# unknown level permits preview and quarantine only.
TRUST_CAPABILITY_PROFILES: dict[str, list[dict[str, str]]] = {
    "built_in_verified": [
        {"name": "benchmark_capability_profile_only", "behavior": "hard"},
        {"name": "deny_secrets", "behavior": "hard"},
    ],
    "publisher_verified": [
        {"name": "deny_secrets", "behavior": "hard"},
        {"name": "bounded_tools_only", "behavior": "reviewable"},
    ],
    "owner_uploaded": [
        {"name": "deny_secrets", "behavior": "hard"},
        {"name": "bounded_tools_only", "behavior": "reviewable"},
    ],
    "public_untrusted": [
        {"name": "deny_secrets", "behavior": "hard"},
        {"name": "deny_network", "behavior": "hard"},
        {"name": "deny_unsafe_tools", "behavior": "hard"},
        {"name": "bounded_network_destinations", "behavior": "reviewable"},
    ],
    "unknown": [
        {"name": "preview_and_quarantine_only", "behavior": "hard"},
        {"name": "deny_secrets", "behavior": "hard"},
        {"name": "deny_network", "behavior": "hard"},
        {"name": "deny_unsafe_tools", "behavior": "hard"},
    ],
}

# The restrictions no promotion can ever override.
HARD_RESTRICTION_NAMES = {
    "deny_secrets",
    "deny_unsafe_tools",
    "preview_and_quarantine_only",
    "benchmark_capability_profile_only",
}


class SourceAdapterError(ValueError):
    """A source adapter request violates its contract or policy."""


class TrustPolicyError(SourceAdapterError):
    """The source trust policy blocks the requested operation."""


def capability_profile_for(trust_level: str) -> list[dict[str, str]]:
    """Return the default execution capability profile for one level."""
    profile = TRUST_CAPABILITY_PROFILES.get(trust_level)
    if profile is None:
        raise TrustPolicyError(f"Unknown trust level: {trust_level!r}")
    return [dict(restriction) for restriction in profile]


def enforce_trust_before_use(trust_level: str, operation: str) -> None:
    """Enforce the trust policy before any preview or execution.

    The unknown level permits quarantine inspection only: preview,
    import, and execution stay blocked until an operator assigns a
    real trust level.
    """
    if trust_level not in TRUST_CAPABILITY_PROFILES:
        raise TrustPolicyError(f"Unknown trust level: {trust_level!r}")
    if trust_level == "unknown" and operation != "quarantine":
        raise TrustPolicyError(
            "A source without adequate provenance permits quarantine "
            f"inspection only; {operation} is blocked"
        )


def authorize_capability_increase(
    profile: list[dict[str, str]],
    restriction_name: str,
    *,
    operator_id: str,
    evidence: str,
) -> list[dict[str, str]]:
    """Lift one reviewable restriction through one operator action.

    Only an authenticated operator action increases a capability
    profile. Dataset text, an agent, or an anonymous request can never
    call this with authority, and a hard restriction never lifts.
    """
    if not operator_id or not operator_id.strip():
        raise TrustPolicyError(
            "A capability increase requires one authenticated operator"
        )
    if not evidence or not evidence.strip():
        raise TrustPolicyError(
            "A capability increase records its review evidence"
        )
    if restriction_name in HARD_RESTRICTION_NAMES:
        raise TrustPolicyError(
            f"The restriction {restriction_name} is hard; no promotion "
            "overrides it"
        )
    remaining: list[dict[str, str]] = []
    lifted = False
    for restriction in profile:
        if restriction["name"] == restriction_name:
            if restriction["behavior"] != "reviewable":
                raise TrustPolicyError(
                    f"The restriction {restriction_name} is hard; no "
                    "promotion overrides it"
                )
            lifted = True
            continue
        remaining.append(dict(restriction))
    if not lifted:
        raise TrustPolicyError(
            f"The profile has no reviewable restriction named "
            f"{restriction_name}"
        )
    return remaining


@dataclass(frozen=True)
class SourceResolution:
    """One resolved source pinned to an exact revision."""

    adapter_id: str
    adapter_version: str
    source_type: str
    locator: str
    pinned_revision: str
    trust_level: str
    trust_policy_version: str = SOURCE_TRUST_POLICY_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceImport:
    """One imported source with its bytes, provenance, and rights."""

    resolution: SourceResolution
    raw_bytes: bytes
    content_checksum: str
    media_type: str
    license: dict[str, Any]
    citation: str | None
    documentation: dict[str, Any]
    items: list[dict[str, Any]]
    configuration: dict[str, Any]


class SourceAdapter(Protocol):
    """The small trusted interface every adapter implements."""

    adapter_id: str
    adapter_version: str
    source_type: str
    trust_level: str

    async def resolve(self, request: dict[str, Any]) -> SourceResolution:
        """Resolve one source request to an exact pinned revision."""
        ...

    async def list_options(
        self, resolution: SourceResolution,
    ) -> dict[str, Any]:
        """List the source's configurations and splits."""
        ...

    async def preview(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Preview normalized records before any import."""
        ...

    async def import_records(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        row_limit: int | None = None,
    ) -> SourceImport:
        """Import records with license, citation, and checksums."""
        ...


_REGISTRY: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    """Register one trusted adapter exactly once."""
    if adapter.adapter_id in _REGISTRY:
        existing = _REGISTRY[adapter.adapter_id]
        if existing.adapter_version != adapter.adapter_version:
            raise SourceAdapterError(
                f"The adapter {adapter.adapter_id} already registers a "
                "different version"
            )
        return
    _REGISTRY[adapter.adapter_id] = adapter


def get_adapter(adapter_id: str) -> SourceAdapter:
    """Return one registered adapter or fail closed."""
    _register_builtin_adapters()
    adapter = _REGISTRY.get(adapter_id)
    if adapter is None:
        raise SourceAdapterError(f"Unknown source adapter: {adapter_id!r}")
    return adapter


def list_adapters() -> list[dict[str, Any]]:
    """Describe every registered adapter with its trust defaults."""
    _register_builtin_adapters()
    return [
        {
            "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.adapter_version,
            "source_type": adapter.source_type,
            "trust_level": adapter.trust_level,
            "trust_policy_version": SOURCE_TRUST_POLICY_VERSION,
            "capability_profile": capability_profile_for(
                adapter.trust_level,
            ),
        }
        for adapter in sorted(
            _REGISTRY.values(), key=lambda item: item.adapter_id,
        )
    ]


def _normalized_items(
    validation_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep imported text as inert data in the canonical item shape."""
    return [dict(item) for item in validation_items]


def _source_record(source_import: SourceImport) -> dict[str, Any]:
    """Build the benchmark-source contract record for one import."""
    resolution = source_import.resolution
    return {
        "schema_id": "benchmark-source",
        "schema_version": 2,
        "source_id": "source-" + content_checksum({
            "locator": resolution.locator,
            "revision": resolution.pinned_revision,
            "checksum": source_import.content_checksum,
        })[:32],
        "source_type": resolution.source_type,
        "locator": resolution.locator,
        "pinned_revision": resolution.pinned_revision,
        "content_checksum": source_import.content_checksum,
        "license": source_import.license,
        "adapter": {
            "id": resolution.adapter_id,
            "version": resolution.adapter_version,
        },
        "fetched_at": source_import.documentation.get(
            "fetched_at", "1970-01-01T00:00:00Z",
        ),
        "imported_by": str(
            source_import.configuration.get("imported_by") or "operator",
        ),
        "configuration": {
            key: value
            for key, value in {
                "selected_configuration": source_import.configuration.get(
                    "selected_configuration",
                ),
                "selected_splits": source_import.configuration.get(
                    "selected_splits",
                ),
            }.items()
            if value is not None
        },
        "documentation_digest": content_checksum(
            source_import.documentation,
        ),
        "trust": {
            "level": resolution.trust_level,
            "policy_version": resolution.trust_policy_version,
        },
        "execution_restrictions": capability_profile_for(
            resolution.trust_level,
        ),
    }


def _source_store() -> Any:
    from core.asset_store import ArtifactStore

    root = Path(db.DB_PATH).parent / "source-artifacts"
    return ArtifactStore(root, "tenant-default")


async def import_through_registry(
    adapter_id: str,
    request: dict[str, Any],
    *,
    configuration: str | None = None,
    split: str | None = None,
    row_limit: int | None = None,
    imported_by: str = "operator",
) -> dict[str, Any]:
    """Run one complete import through the registry and the facade.

    The import resolves an exact revision, enforces the trust policy,
    persists the immutable source bytes, and stores one validated
    benchmark-source record with complete provenance and rights
    metadata through the one canonical facade.
    """
    from activation_service import persist_protected_artifact
    from benchmarks import facade
    from core.asset_store import DataClass

    adapter = get_adapter(adapter_id)
    resolution = await adapter.resolve(request)
    enforce_trust_before_use(resolution.trust_level, "import")
    source_import = await adapter.import_records(
        resolution,
        configuration=configuration,
        split=split,
        row_limit=row_limit,
    )
    if content_checksum(
        source_import.raw_bytes.decode("utf-8", errors="replace"),
    ) != source_import.content_checksum:
        raise SourceAdapterError(
            "The adapter's content checksum does not match its bytes"
        )
    record = _source_record(source_import)
    record["imported_by"] = imported_by
    artifact_digest = persist_protected_artifact(
        _source_store(),
        source_import.raw_bytes,
        media_type=source_import.media_type or "application/octet-stream",
        access_policy="benchmark-source-bytes",
        data_class=DataClass.INTERNAL,
        referenced_by=record["source_id"],
    )
    saved = await facade.execute("import_source", {"record": record})
    return {
        "source": record,
        "source_id": saved["id"],
        "record_checksum": saved["record_checksum"],
        "artifact_digest": artifact_digest,
        "items": source_import.items,
        "item_count": len(source_import.items),
    }


# ── The local upload adapter ─────────────────────────────────────────


class LocalUploadAdapter:
    """Import one owner-uploaded CSV or JSONL file."""

    adapter_id = "adapter-local-upload"
    adapter_version = "1"
    source_type = "local_upload"
    trust_level = "owner_uploaded"

    async def resolve(self, request: dict[str, Any]) -> SourceResolution:
        content = request.get("content")
        filename = str(request.get("filename") or "")
        if not isinstance(content, bytes) or not filename:
            raise SourceAdapterError(
                "A local upload names its filename and carries its bytes"
            )
        return SourceResolution(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            source_type=self.source_type,
            locator=f"upload://{filename}",
            # The uploaded bytes are the revision: their digest pins
            # the exact content.
            pinned_revision=content_checksum(
                content.decode("utf-8", errors="replace"),
            ),
            trust_level=self.trust_level,
            metadata={
                "filename": filename,
                "content": content,
                "mapping": dict(
                    request.get("mapping") or DEFAULT_FIELD_MAPPING,
                ),
            },
        )

    async def list_options(
        self, resolution: SourceResolution,
    ) -> dict[str, Any]:
        del resolution
        return {"configurations": ["default"], "splits": []}

    async def preview(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        del configuration, split
        enforce_trust_before_use(resolution.trust_level, "preview")
        validation = self._validate(resolution)
        return _normalized_items(validation.items)[:limit]

    async def import_records(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        row_limit: int | None = None,
    ) -> SourceImport:
        del configuration, split
        enforce_trust_before_use(resolution.trust_level, "import")
        validation = self._validate(resolution)
        if not validation.valid:
            raise SourceAdapterError(
                "The upload does not validate into canonical items"
            )
        items = _normalized_items(validation.items)
        if row_limit is not None:
            items = items[: max(int(row_limit), 0)]
        content = resolution.metadata["content"]
        return SourceImport(
            resolution=resolution,
            raw_bytes=content,
            content_checksum=resolution.pinned_revision,
            media_type="text/plain",
            license={"name": str(
                resolution.metadata.get("license") or "owner-declared",
            )},
            citation=None,
            documentation={
                "filename": resolution.metadata["filename"],
                "format": validation.format,
                "columns": validation.columns,
                "item_checksum": str(validation.checksum),
            },
            items=items,
            configuration={"selected_splits": None},
        )

    def _validate(self, resolution: SourceResolution) -> Any:
        return validate_dataset(
            resolution.metadata["content"],
            filename=str(resolution.metadata["filename"]),
            mapping=dict(resolution.metadata.get("mapping") or {}),
        )


# ── The built-in curated catalog adapter ─────────────────────────────


_CATALOG_DIRECTORY = Path(__file__).parent / "catalog"


class BuiltInCatalogAdapter:
    """Import one versioned local fixture that passed review."""

    adapter_id = "adapter-built-in-catalog"
    adapter_version = "1"
    source_type = "built_in_catalog"
    trust_level = "built_in_verified"

    async def resolve(self, request: dict[str, Any]) -> SourceResolution:
        name = str(request.get("catalog_entry") or "")
        path = _CATALOG_DIRECTORY / f"{name}.jsonl"
        if not name or not path.is_file():
            raise SourceAdapterError(
                f"Unknown catalog entry: {name!r}"
            )
        content = path.read_bytes()
        return SourceResolution(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            source_type=self.source_type,
            locator=f"catalog://{name}",
            pinned_revision=content_checksum(content.decode("utf-8")),
            trust_level=self.trust_level,
            metadata={"content": content, "name": name},
        )

    async def list_options(
        self, resolution: SourceResolution,
    ) -> dict[str, Any]:
        del resolution
        entries = sorted(
            path.stem for path in _CATALOG_DIRECTORY.glob("*.jsonl")
        )
        return {"configurations": entries, "splits": ["test"]}

    async def preview(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        del configuration, split
        enforce_trust_before_use(resolution.trust_level, "preview")
        validation = validate_dataset(
            resolution.metadata["content"],
            filename=f"{resolution.metadata['name']}.jsonl",
            mapping=dict(DEFAULT_FIELD_MAPPING),
        )
        return _normalized_items(validation.items)[:limit]

    async def import_records(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        row_limit: int | None = None,
    ) -> SourceImport:
        del configuration, split
        enforce_trust_before_use(resolution.trust_level, "import")
        content = resolution.metadata["content"]
        validation = validate_dataset(
            content,
            filename=f"{resolution.metadata['name']}.jsonl",
            mapping=dict(DEFAULT_FIELD_MAPPING),
        )
        items = _normalized_items(validation.items)
        if row_limit is not None:
            items = items[: max(int(row_limit), 0)]
        return SourceImport(
            resolution=resolution,
            raw_bytes=content,
            content_checksum=resolution.pinned_revision,
            media_type="application/x-ndjson",
            license={"name": "repository-reviewed",
                     "citation": "bmas built-in catalog"},
            citation="bmas built-in catalog",
            documentation={
                "catalog_entry": resolution.metadata["name"],
                "item_checksum": str(validation.checksum),
            },
            items=items,
            configuration={"selected_splits": ["test"]},
        )


# ── The Hugging Face adapter ─────────────────────────────────────────


HUGGING_FACE_API = "https://huggingface.co/api/datasets"
HUGGING_FACE_VIEWER = "https://datasets-server.huggingface.co"


class HuggingFaceAdapter:
    """Import through the official Dataset Viewer and hub endpoints.

    Every mutable revision resolves to an exact commit before any
    preview or import, the dataset card and license metadata store
    with the source, and every request flows through the safe egress
    broker.
    """

    adapter_id = "adapter-hugging-face"
    adapter_version = "1"
    source_type = "hugging_face"
    trust_level = "public_untrusted"

    def __init__(
        self,
        *,
        fetch_json: Callable[[str], Awaitable[dict[str, Any]]]
        | None = None,
    ) -> None:
        self._fetch_json = fetch_json or self._broker_fetch_json

    @staticmethod
    async def _broker_fetch_json(url: str) -> dict[str, Any]:
        fetcher = SafeFetcher()
        result = await fetcher.fetch(url)
        return json.loads(result.content.decode("utf-8"))

    async def resolve(self, request: dict[str, Any]) -> SourceResolution:
        repository = str(request.get("repository") or "")
        if not repository or repository.count("/") > 1:
            raise SourceAdapterError(
                "A Hugging Face import names one dataset repository"
            )
        detail = await self._fetch_json(
            f"{HUGGING_FACE_API}/{repository}",
        )
        commit = str(detail.get("sha") or "")
        if not commit:
            # A mutable revision never imports silently; it resolves
            # to one exact commit first.
            raise SourceAdapterError(
                "The repository reports no resolvable commit; a mutable "
                "revision cannot import"
            )
        card = detail.get("cardData") or {}
        license_name = str(
            card.get("license") or detail.get("license") or "unknown",
        )
        return SourceResolution(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            source_type=self.source_type,
            locator=f"hf://datasets/{repository}",
            pinned_revision=commit,
            trust_level=self.trust_level,
            metadata={
                "repository": repository,
                "card": card,
                "license": license_name,
                "citation": card.get("citation"),
                "last_modified": detail.get("lastModified"),
            },
        )

    async def list_options(
        self, resolution: SourceResolution,
    ) -> dict[str, Any]:
        repository = resolution.metadata["repository"]
        answer = await self._fetch_json(
            f"{HUGGING_FACE_VIEWER}/splits?dataset={repository}",
        )
        configurations: list[str] = []
        splits: list[dict[str, str]] = []
        for entry in answer.get("splits") or []:
            config = str(entry.get("config") or "default")
            if config not in configurations:
                configurations.append(config)
            splits.append({
                "config": config,
                "split": str(entry.get("split") or ""),
            })
        return {"configurations": configurations, "splits": splits}

    async def preview(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        enforce_trust_before_use(resolution.trust_level, "preview")
        rows = await self._rows(
            resolution, configuration, split, min(limit, 100),
        )
        return [self._normalize_row(row) for row in rows]

    async def import_records(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        row_limit: int | None = None,
    ) -> SourceImport:
        enforce_trust_before_use(resolution.trust_level, "import")
        limit = min(int(row_limit or 100), 10_000)
        rows = await self._rows(resolution, configuration, split, limit)
        items = [self._normalize_row(row) for row in rows]
        raw = "\n".join(
            json.dumps(row, separators=(",", ":"), sort_keys=True)
            for row in rows
        ).encode("utf-8")
        documentation = {
            "card": resolution.metadata.get("card") or {},
            "repository": resolution.metadata["repository"],
            "revision": resolution.pinned_revision,
            "fetched_at": "1970-01-01T00:00:00Z",
        }
        return SourceImport(
            resolution=resolution,
            raw_bytes=raw,
            content_checksum=content_checksum(raw.decode("utf-8")),
            media_type="application/x-ndjson",
            license={"name": str(resolution.metadata["license"])},
            citation=(
                str(resolution.metadata["citation"])
                if resolution.metadata.get("citation")
                else None
            ),
            documentation=documentation,
            items=items,
            configuration={
                "selected_configuration": configuration or "default",
                "selected_splits": [split] if split else None,
            },
        )

    async def _rows(
        self,
        resolution: SourceResolution,
        configuration: str | None,
        split: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        repository = resolution.metadata["repository"]
        answer = await self._fetch_json(
            f"{HUGGING_FACE_VIEWER}/rows?dataset={repository}"
            f"&config={configuration or 'default'}"
            f"&split={split or 'test'}&offset=0&length={limit}",
        )
        return [
            dict(entry.get("row") or {})
            for entry in (answer.get("rows") or [])[:limit]
        ]

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        # Imported text is inert data: fields map by name only, and no
        # value changes the mapping, the trust, or the capabilities.
        question = row.get("question") or row.get("input") or ""
        answer = row.get("answer") or row.get("expected_output") or ""
        return {
            "item_key": str(row.get("id") or content_checksum(row)[:16]),
            "input": str(question),
            "expected_output": str(answer),
            "subject": str(row.get("subject") or "default"),
            "split": str(row.get("split") or "test"),
            "tags": [],
            "metadata": {"imported_fields": sorted(row)},
        }


# ── The safe HTTPS file adapter ──────────────────────────────────────


class HttpsFileAdapter:
    """Import one static CSV or JSONL file over the egress broker.

    Parquet bytes fetch and persist immutably; their parsing waits for
    the sandboxed import worker, so no remote format ever executes
    code in the daemon.
    """

    adapter_id = "adapter-https-file"
    adapter_version = "1"
    source_type = "https_file"
    trust_level = "public_untrusted"

    def __init__(self, *, fetcher: SafeFetcher | None = None) -> None:
        self._fetcher = fetcher or SafeFetcher()

    async def resolve(self, request: dict[str, Any]) -> SourceResolution:
        url = str(request.get("url") or "")
        if not url:
            raise SourceAdapterError("An HTTPS import names one URL")
        result = await self._fetcher.fetch(url)
        return SourceResolution(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            source_type=self.source_type,
            locator=url,
            pinned_revision=content_checksum(
                result.content.decode("utf-8", errors="replace"),
            ),
            trust_level=self.trust_level,
            metadata={
                "content": result.content,
                "media_type": result.media_type,
                "final_url": result.final_url,
                "redirects_followed": result.redirects_followed,
                "license": request.get("license"),
            },
        )

    async def list_options(
        self, resolution: SourceResolution,
    ) -> dict[str, Any]:
        del resolution
        return {"configurations": ["default"], "splits": []}

    async def preview(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        del configuration, split
        enforce_trust_before_use(resolution.trust_level, "preview")
        return self._parsed_items(resolution)[:limit]

    async def import_records(
        self,
        resolution: SourceResolution,
        *,
        configuration: str | None = None,
        split: str | None = None,
        row_limit: int | None = None,
    ) -> SourceImport:
        del configuration, split
        enforce_trust_before_use(resolution.trust_level, "import")
        items = self._parsed_items(resolution)
        if row_limit is not None:
            items = items[: max(int(row_limit), 0)]
        content = resolution.metadata["content"]
        license_name = str(resolution.metadata.get("license") or "unknown")
        return SourceImport(
            resolution=resolution,
            raw_bytes=content,
            content_checksum=resolution.pinned_revision,
            media_type=str(resolution.metadata["media_type"] or ""),
            # A missing license stays unknown; it never becomes an
            # implicit permission.
            license={"name": license_name},
            citation=None,
            documentation={
                "final_url": resolution.metadata["final_url"],
                "redirects_followed": resolution.metadata[
                    "redirects_followed"
                ],
            },
            items=items,
            configuration={"selected_splits": None},
        )

    def _parsed_items(
        self, resolution: SourceResolution,
    ) -> list[dict[str, Any]]:
        url = str(resolution.locator).split("?")[0].lower()
        if url.endswith(".parquet"):
            raise SourceAdapterError(
                "Parquet parsing runs in the sandboxed import worker; "
                "the fetched bytes stay stored for it"
            )
        filename = "import.csv" if url.endswith(".csv") else "import.jsonl"
        validation = validate_dataset(
            resolution.metadata["content"],
            filename=filename,
            mapping=dict(DEFAULT_FIELD_MAPPING),
        )
        if not validation.valid:
            raise SourceAdapterError(
                "The fetched file does not validate into canonical items"
            )
        return _normalized_items(validation.items)


_builtin_registered = False


def _register_builtin_adapters() -> None:
    global _builtin_registered
    if _builtin_registered:
        return
    register_adapter(LocalUploadAdapter())
    register_adapter(BuiltInCatalogAdapter())
    register_adapter(HuggingFaceAdapter())
    register_adapter(HttpsFileAdapter())
    _builtin_registered = True
