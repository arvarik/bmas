"""Versioned benchmark dataset registry endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import database as db
from auth import require_api_key
from benchmarks.datasets import validate_dataset
from config import BMAS_API_KEY
from file_utils import sanitize_filename

router = APIRouter(prefix="/datasets", tags=["datasets"])
DATASET_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
MAPPING_FIELDS = {"id", "input", "expected_output", "subject", "split", "tags"}
MAX_DATASET_UPLOAD_BYTES = (
    min(max(int(os.getenv("BMAS_DATASET_MAX_UPLOAD_MB", "100")), 1), 1024) * 1024 * 1024
)
DATASET_SOURCE_DIR = Path(
    os.getenv(
        "BMAS_DATASET_SOURCE_DIR",
        str(Path(db.DB_PATH).parent / "datasets"),
    )
)


def _mapping(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=422, detail="The field mapping is invalid JSON") from error
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="The field mapping must be an object")
    unknown = set(value) - MAPPING_FIELDS
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported mapping fields: {', '.join(sorted(unknown))}",
        )
    return {
        str(key): str(column).strip()
        for key, column in value.items()
        if isinstance(column, str) and column.strip()
    }


async def _read_upload(upload: UploadFile) -> bytes:
    content = await upload.read(MAX_DATASET_UPLOAD_BYTES + 1)
    if len(content) > MAX_DATASET_UPLOAD_BYTES:
        limit_mb = MAX_DATASET_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"The dataset file exceeds the {limit_mb} MB limit",
        )
    if not content:
        raise HTTPException(status_code=422, detail="The dataset file is empty")
    return content


def _clean_optional(value: str | None, limit: int = 1000) -> str | None:
    cleaned = value.strip()[:limit] if value else ""
    return cleaned or None


def _source_uri(value: str | None) -> str | None:
    """Accept only absolute HTTP source links."""
    cleaned = _clean_optional(value, 2000)
    if cleaned is None:
        return None
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="The source URL must use HTTP or HTTPS",
        )
    return cleaned


@router.get("")
async def list_datasets_endpoint(
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Return one searchable dataset page."""
    datasets, total = await db.list_datasets(
        search=search,
        limit=limit,
        offset=offset,
    )
    return {
        "datasets": datasets,
        "total": total,
        "limit": min(max(limit, 1), 200),
        "offset": max(offset, 0),
        "max_upload_bytes": MAX_DATASET_UPLOAD_BYTES,
        "accepted_types": ["csv", "jsonl"],
    }


@router.post("/validate")
async def validate_dataset_endpoint(
    file: Annotated[UploadFile, File(...)],
    mapping: Annotated[str, Form()] = "{}",
):
    """Validate and preview an upload without storing it."""
    content = await _read_upload(file)
    validation = validate_dataset(
        content,
        filename=file.filename or "dataset",
        mapping=_mapping(mapping),
    )
    return {
        **validation.public_dict(),
        "filename": file.filename,
        "bytes": len(content),
        "max_upload_bytes": MAX_DATASET_UPLOAD_BYTES,
    }


@router.post("/import", status_code=201)
async def import_dataset_endpoint(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    name: Annotated[str, Form()],
    mapping: Annotated[str, Form()],
    dataset_id: Annotated[str | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    source_uri: Annotated[str | None, Form()] = None,
    license_name: Annotated[str | None, Form(alias="license")] = None,
    author: Annotated[str | None, Form()] = None,
):
    """Store one validated upload as a published immutable version."""
    require_api_key(request, BMAS_API_KEY)
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 200:
        raise HTTPException(
            status_code=422, detail="The dataset name must contain 1 to 200 characters"
        )
    if dataset_id and not DATASET_ID_PATTERN.fullmatch(dataset_id):
        raise HTTPException(status_code=422, detail="The dataset identifier is invalid")

    content = await _read_upload(file)
    field_mapping = _mapping(mapping)
    validation = validate_dataset(
        content,
        filename=file.filename or "dataset",
        mapping=field_mapping,
    )
    if not validation.valid or not validation.checksum:
        return JSONResponse(
            {
                "error": "Dataset validation failed",
                **validation.public_dict(),
            },
            status_code=422,
        )

    resolved_dataset_id = dataset_id or f"dataset-{uuid.uuid4().hex[:12]}"
    version_id = f"dsv-{uuid.uuid4().hex}"
    source_dir = DATASET_SOURCE_DIR / resolved_dataset_id / version_id
    source_filename = sanitize_filename(file.filename or "dataset")
    source_path = source_dir / source_filename
    source_checksum = hashlib.sha256(content).hexdigest()
    await asyncio.to_thread(source_dir.mkdir, parents=True, exist_ok=False)
    try:
        await asyncio.to_thread(source_path.write_bytes, content)
    except Exception:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(source_dir.rmdir)
        raise
    try:
        dataset = await db.create_dataset_version(
            dataset_id=resolved_dataset_id,
            version_id=version_id,
            name=clean_name,
            description=description.strip()[:4000],
            source_uri=_source_uri(source_uri),
            license_name=_clean_optional(license_name, 200),
            author=_clean_optional(author, 300),
            dataset_metadata={},
            checksum=validation.checksum,
            schema={
                "version": "1",
                "source_format": validation.format,
                "mapping": field_mapping,
                "columns": validation.columns,
            },
            source_filename=source_filename,
            source_mime=file.content_type or "application/octet-stream",
            source_checksum=source_checksum,
            source_path=str(source_path),
            version_metadata={"import_bytes": len(content)},
            items=validation.items,
            publish=True,
        )
    except db.DatasetVersionConflict as error:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(source_path.unlink)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(source_dir.rmdir)
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(source_path.unlink)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(source_dir.rmdir)
        raise

    return {
        "dataset": dataset,
        "version_id": version_id,
        "checksum": validation.checksum,
        "item_count": validation.row_count,
    }


@router.get("/{dataset_id}")
async def get_dataset_endpoint(dataset_id: str):
    """Return one dataset and all immutable versions."""
    if not DATASET_ID_PATTERN.fullmatch(dataset_id):
        raise HTTPException(status_code=400, detail="The dataset identifier is invalid")
    dataset = await db.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return {
        "dataset": dataset,
        "max_upload_bytes": MAX_DATASET_UPLOAD_BYTES,
        "accepted_types": ["csv", "jsonl"],
    }


@router.get("/{dataset_id}/versions/{version_id}/items")
async def list_dataset_items_endpoint(
    dataset_id: str,
    version_id: str,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Return one searchable page of canonical dataset items."""
    dataset = await db.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    if not any(version["id"] == version_id for version in dataset["versions"]):
        raise HTTPException(status_code=404, detail="Dataset version not found")
    items, total = await db.list_dataset_items(
        version_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return {
        "items": items,
        "total": total,
        "limit": min(max(limit, 1), 200),
        "offset": max(offset, 0),
    }


@router.get("/{dataset_id}/versions/{version_id}/source")
async def download_dataset_source_endpoint(dataset_id: str, version_id: str):
    """Download the preserved source file for one immutable version."""
    version = await db.get_dataset_version_source(dataset_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Dataset version not found")
    source_path = str(version.get("source_path") or "")
    if not source_path or not os.path.isfile(source_path):
        raise HTTPException(status_code=410, detail="The preserved source file is unavailable")
    return FileResponse(
        source_path,
        media_type=str(version.get("source_mime") or "application/octet-stream"),
        filename=str(version.get("source_filename") or "dataset"),
    )
