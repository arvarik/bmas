# /opt/bmas/daemon/src/routes/files.py
"""
File upload and download endpoints (doc 17 §3).

POST /tasks/{task_id}/files  — multipart upload with validation + extraction
GET  /tasks/{task_id}/files  — list files for a task
GET  /tasks/{task_id}/files/{file_id}      — download file content
GET  /tasks/{task_id}/files/{file_id}/text  — extracted text only
"""

import logging
import os
import uuid

from urllib.parse import quote

from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse

import database as db
from auth import check_bearer_or_pass
from config import (
    STORAGE_ENABLED, STORAGE_USER_MEDIA_DIR, STORAGE_MAX_UPLOAD_MB,
    STORAGE_ALLOWED_TYPES, STORAGE_PDF_EXTRACTION, STORAGE_EXTRACTION_MAX_CHARS,
    BMAS_NODE_KEY,
)
from file_utils import (
    sanitize_filename, compute_sha256, get_mime_type, get_extension,
    extract_pdf_text, extract_text_file,
)

logger = logging.getLogger("bmas.files")

router = APIRouter()

# Max bytes for upload (from config, in MB → bytes)
_MAX_UPLOAD_BYTES = STORAGE_MAX_UPLOAD_MB * 1024 * 1024


def _check_bearer_or_pass(request: Request) -> None:
    """Auth helper — delegates to shared auth module."""
    check_bearer_or_pass(request, BMAS_NODE_KEY)


@router.post("/tasks/{task_id}/files")
async def upload_file(task_id: str, request: Request, file: UploadFile = File(...)):
    """Upload a file to a task.

    Validates size, type, sanitizes filename, extracts text for PDFs,
    stores on disk, and creates a task_files row.
    """
    if not STORAGE_ENABLED:
        return JSONResponse(
            {"error": "Storage is not enabled. Set storage.enabled: true in bmas.yaml"},
            status_code=422,
        )

    # Verify task exists
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    # Validate filename and extension
    original_name = file.filename or "upload"
    try:
        safe_name = sanitize_filename(original_name)
    except ValueError as e:
        return JSONResponse({"error": f"Invalid filename: {e}"}, status_code=422)

    ext = get_extension(safe_name)
    if ext not in STORAGE_ALLOWED_TYPES:
        return JSONResponse(
            {"error": f"File type '{ext}' not allowed. Allowed: {sorted(STORAGE_ALLOWED_TYPES)}"},
            status_code=422,
        )

    # Read file content with size limit
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": f"File too large ({len(content)} bytes). Max: {STORAGE_MAX_UPLOAD_MB}MB"},
            status_code=413,
        )

    if len(content) == 0:
        return JSONResponse({"error": "Empty file"}, status_code=422)

    # Compute hash
    sha256 = compute_sha256(content)
    mime = get_mime_type(safe_name)

    # Store on disk: {user_media_dir}/{task_id}/{filename}
    task_dir = os.path.join(STORAGE_USER_MEDIA_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    stored_path = os.path.join(task_dir, safe_name)

    # If file already exists with same name, append hash prefix
    if os.path.exists(stored_path):
        base, file_ext = os.path.splitext(safe_name)
        safe_name = f"{base}-{sha256[:8]}{file_ext}"
        stored_path = os.path.join(task_dir, safe_name)

    with open(stored_path, "wb") as f:
        f.write(content)

    # Extract text
    extracted_text = ""
    if STORAGE_PDF_EXTRACTION != "off":
        if ext == "pdf":
            extracted_text = extract_pdf_text(content, STORAGE_EXTRACTION_MAX_CHARS)
        elif ext in ("txt", "md", "csv", "json"):
            extracted_text = extract_text_file(content, STORAGE_EXTRACTION_MAX_CHARS)

    # Create DB row (B2 fix: positional args, not dict)
    file_id = f"f-{str(uuid.uuid4())[:8]}"
    await db.insert_task_file(
        file_id, task_id, safe_name, mime,
        len(content), sha256, stored_path, len(extracted_text),
    )

    # Persist extracted text alongside the file (B5 fix)
    if extracted_text:
        text_path = stored_path + ".extracted.txt"
        with open(text_path, "w", encoding="utf-8") as tf:
            tf.write(extracted_text)

    # Emit SSE event
    try:
        from app import app
        orch = app.state.orchestrator
        await orch.bb.publish_event(task_id, "file_added", {
            "file_id": file_id,
            "name": safe_name,
            "mime": mime,
            "bytes": len(content),
            "sha256": sha256,
            "extracted_chars": len(extracted_text),
        })
    except Exception:
        pass  # SSE is best-effort

    # Post attachment board entry (spec §4)
    try:
        from app import app
        orch = app.state.orchestrator
        preview = extracted_text[:1500] if extracted_text else ""
        body_parts = [f"**{safe_name}** ({mime}, {len(content)} bytes, sha256: {sha256[:16]}…)"]
        if preview:
            body_parts.append(f"\n\nExtracted text preview:\n{preview}")
        body_parts.append("\n\nFetch the full content via your attachments list.")

        await orch.gateway.append(
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
    except Exception as e:
        logger.warning(f"Failed to create attachment board entry for {file_id}: {e}")

    logger.info(f"File uploaded: {file_id} ({safe_name}, {len(content)} bytes, sha256={sha256[:16]}…)")

    return {
        "file_id": file_id,
        "filename": safe_name,
        "bytes": len(content),
        "sha256": sha256,
        "extracted_chars": len(extracted_text),
    }


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

    # Read extracted text from sidecar file (B5 fix)
    extracted_text = ""
    stored_path = file_row.get("stored_path", "")
    text_path = stored_path + ".extracted.txt" if stored_path else ""
    if text_path and os.path.exists(text_path):
        try:
            with open(text_path, "r", encoding="utf-8") as tf:
                extracted_text = tf.read()
        except Exception:
            pass

    return {
        "file_id": file_id,
        "name": file_row["name"],
        "extracted_text": extracted_text,
        "extracted_chars": file_row.get("extracted_chars", 0),
    }
