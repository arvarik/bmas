# /opt/bmas/daemon/src/routes/files.py
"""
File upload and download endpoints (doc 17 §3).

POST /tasks/{task_id}/files  — multipart upload with validation + extraction
GET  /tasks/{task_id}/files  — list files for a task
GET  /tasks/{task_id}/files/{file_id}      — download file content
GET  /tasks/{task_id}/files/{file_id}/text  — extracted text only
"""

import contextlib
import logging
import os
import uuid
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import database as db
from auth import check_bearer_or_pass, require_api_key
from config import (
    BMAS_API_KEY,
    BMAS_NODE_KEY,
    STORAGE_ALLOWED_TYPES,
    STORAGE_ENABLED,
    STORAGE_EXTRACTION_MAX_CHARS,
    STORAGE_MAX_UPLOAD_MB,
    STORAGE_PDF_EXTRACTION,
    STORAGE_USER_MEDIA_DIR,
)
from file_utils import (
    compute_sha256,
    extract_pdf_text,
    extract_text_file,
    get_extension,
    get_mime_type,
    read_extracted_text,
    sanitize_filename,
)

logger = logging.getLogger("bmas.files")

router = APIRouter()

# Max bytes for upload (from config, in MB → bytes)
_MAX_UPLOAD_BYTES = STORAGE_MAX_UPLOAD_MB * 1024 * 1024


class FileUploadError(ValueError):
    """Describe one expected upload validation failure."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _check_bearer_or_pass(request: Request) -> None:
    """Auth helper — delegates to shared auth module."""
    check_bearer_or_pass(request, BMAS_NODE_KEY)


@router.post("/tasks/{task_id}/files")
async def upload_file(task_id: str, request: Request, file: UploadFile = File(...)):
    """Upload a file to a task.

    Validates size, type, sanitizes filename, extracts text for PDFs,
    stores on disk, and creates a task_files row.
    """
    require_api_key(request, BMAS_API_KEY)
    try:
        stored = await store_task_file(task_id, file, announce=True)
    except FileUploadError as exc:
        return JSONResponse(
            {"error": exc.message},
            status_code=exc.status_code,
        )
    return public_upload_result(stored)


def public_upload_result(stored: dict) -> dict:
    """Remove internal storage paths from one upload response."""
    return {
        key: stored[key]
        for key in (
            "file_id",
            "filename",
            "bytes",
            "sha256",
            "extracted_chars",
        )
    }


async def store_task_file(
    task_id: str,
    file: UploadFile,
    *,
    announce: bool,
) -> dict:
    """Validate and store one task file without route-specific authentication."""
    if not STORAGE_ENABLED:
        raise FileUploadError(
            "Storage is not enabled. Set storage.enabled: true in bmas.yaml",
        )

    # Verify task exists
    task = await db.get_task(task_id)
    if not task:
        raise FileUploadError("Task not found", status_code=404)

    # Validate filename and extension
    original_name = file.filename or "upload"
    try:
        safe_name = sanitize_filename(original_name)
    except ValueError as e:
        raise FileUploadError(f"Invalid filename: {e}") from e

    ext = get_extension(safe_name)
    if ext not in STORAGE_ALLOWED_TYPES:
        raise FileUploadError(
            f"File type '{ext}' not allowed. Allowed: {sorted(STORAGE_ALLOWED_TYPES)}",
        )

    # Read at most one byte beyond the limit. This bounds memory use for
    # rejected files.
    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(content) > _MAX_UPLOAD_BYTES:
        raise FileUploadError(
            f"File too large. Max: {STORAGE_MAX_UPLOAD_MB}MB",
            status_code=413,
        )

    if len(content) == 0:
        raise FileUploadError("Empty file")

    # Compute hash
    sha256 = compute_sha256(content)
    mime = get_mime_type(safe_name)

    # Store on disk: {user_media_dir}/{task_id}/{filename}
    task_dir = os.path.join(STORAGE_USER_MEDIA_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    stored_path = os.path.join(task_dir, safe_name)

    # Keep every database row on its own immutable path.
    if os.path.exists(stored_path):
        base, file_ext = os.path.splitext(safe_name)
        safe_name = f"{base}-{sha256[:8]}{file_ext}"
        stored_path = os.path.join(task_dir, safe_name)
        suffix = 2
        while os.path.exists(stored_path):
            safe_name = f"{base}-{sha256[:8]}-{suffix}{file_ext}"
            stored_path = os.path.join(task_dir, safe_name)
            suffix += 1

    # Extract text
    extracted_text = ""
    if STORAGE_PDF_EXTRACTION != "off":
        if ext == "pdf":
            extracted_text = extract_pdf_text(content, STORAGE_EXTRACTION_MAX_CHARS)
        elif ext in ("txt", "md", "csv", "json"):
            extracted_text = extract_text_file(content, STORAGE_EXTRACTION_MAX_CHARS)

    # Save the file and sidecar before the database row becomes visible.
    file_id = f"f-{str(uuid.uuid4())[:8]}"
    text_path = stored_path + ".extracted.txt" if extracted_text else None
    try:
        with open(stored_path, "xb") as stored_file:
            stored_file.write(content)
        if text_path:
            with open(text_path, "x", encoding="utf-8") as text_file:
                text_file.write(extracted_text)
        await db.insert_task_file(
            file_id, task_id, safe_name, mime,
            len(content), sha256, stored_path, len(extracted_text),
        )
    except BaseException:
        for path in (text_path, stored_path):
            if path:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
        raise

    if announce:
        await announce_task_file(
            task_id=task_id,
            file_id=file_id,
            safe_name=safe_name,
            mime=mime,
            size_bytes=len(content),
            sha256=sha256,
            extracted_text=extracted_text,
        )

    logger.info(f"File uploaded: {file_id} ({safe_name}, {len(content)} bytes, sha256={sha256[:16]}…)")

    return {
        "file_id": file_id,
        "filename": safe_name,
        "bytes": len(content),
        "sha256": sha256,
        "extracted_chars": len(extracted_text),
        "stored_path": stored_path,
        "text_path": text_path,
    }


async def announce_task_file(
    *,
    task_id: str,
    file_id: str,
    safe_name: str,
    mime: str,
    size_bytes: int,
    sha256: str,
    extracted_text: str,
) -> None:
    """Publish one stored upload to live views and the task board."""
    try:
        from app import app

        orchestrator = app.state.orchestrator
        await orchestrator.bb.publish_event(task_id, "file_added", {
            "file_id": file_id,
            "name": safe_name,
            "mime": mime,
            "bytes": size_bytes,
            "sha256": sha256,
            "extracted_chars": len(extracted_text),
        })
    except Exception:
        pass

    try:
        from app import app

        orchestrator = app.state.orchestrator
        preview = extracted_text[:1500]
        body_parts = [
            f"**{safe_name}** ({mime}, {size_bytes} bytes, sha256: {sha256[:16]}…)",
        ]
        if preview:
            body_parts.append(f"\n\nExtracted text preview:\n{preview}")
        body_parts.append("\n\nFetch the full content from the task attachments.")
        await orchestrator.append_task_entry(
            task_id=task_id,
            actor="daemon",
            capabilities=["post:attachment"],
            proposed=[{
                "type": "attachment",
                "title": safe_name,
                "body": "".join(body_parts),
                "confidence": 1.0,
            }],
            turn_id=f"upload-{file_id}",
            round_no=0,
        )
    except Exception as exc:
        logger.warning(
            "Failed to create attachment board entry for %s: %s",
            file_id,
            exc,
        )


@router.get("/tasks/{task_id}/files")
async def list_files(task_id: str):
    """List all uploaded files for a task."""
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    files = await db.get_task_files(task_id)
    # Don't return stored_path or extracted_text in listing
    return {
        "files": [
            {
                "id": f["id"],
                "name": f["name"],
                "mime": f["mime"],
                "bytes": f["bytes"],
                "sha256": f["sha256"],
                "extracted_chars": f["extracted_chars"],
                "created_at": f["created_at"],
            }
            for f in files
        ]
    }


@router.get("/tasks/{task_id}/files/{file_id}")
async def download_file(task_id: str, file_id: str, request: Request):
    """Download a task file.

    Auth: dashboard session (no auth) or BMAS_NODE_KEY bearer.
    Forces download via Content-Disposition: attachment.
    """
    _check_bearer_or_pass(request)

    file_row = await db.get_task_file(file_id)
    if not file_row or file_row["task_id"] != task_id:
        return JSONResponse({"error": "File not found"}, status_code=404)

    stored_path = file_row["stored_path"]
    if not os.path.exists(stored_path):
        return JSONResponse({"error": "File not found on disk"}, status_code=404)

    return FileResponse(
        path=stored_path,
        filename=file_row["name"],
        media_type=file_row["mime"],
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_row['name'])}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/tasks/{task_id}/files/{file_id}/text")
async def get_file_text(task_id: str, file_id: str, request: Request):
    """Return extracted text for a task file.

    Used by agents to get file content without re-parsing.
    Auth: same as download_file.
    """
    _check_bearer_or_pass(request)

    file_row = await db.get_task_file(file_id)
    if not file_row or file_row["task_id"] != task_id:
        return JSONResponse({"error": "File not found"}, status_code=404)

    extracted_text = read_extracted_text(file_row.get("stored_path", ""))

    return {
        "file_id": file_id,
        "name": file_row["name"],
        "extracted_text": extracted_text,
        "extracted_chars": file_row.get("extracted_chars", 0),
    }
