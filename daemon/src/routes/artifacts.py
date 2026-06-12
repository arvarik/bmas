# /opt/bmas/daemon/src/routes/artifacts.py
"""
Artifact ingest and retrieval endpoints (doc 17 §6).

POST /ingest/artifacts/{task_id}/{turn_id}  — node artifact sync (bearer auth)
GET  /tasks/{task_id}/artifacts             — list artifacts
GET  /tasks/{task_id}/artifacts/{artifact_id} — download artifact
"""

import logging
import os
import shutil
import uuid
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import database as db
from auth import check_bearer_or_pass, require_node_key
from config import (
    BMAS_NODE_KEY,
    STORAGE_ARTIFACTS_DIR,
    STORAGE_ENABLED,
    STORAGE_MAX_TASK_OUTPUT_MB,
)
from file_utils import (
    compute_sha256,
    get_mime_type,
    resolve_slug_collision,
    slugify_task,
    validate_path_traversal,
)

logger = logging.getLogger("bmas.artifacts")

router = APIRouter()

_MAX_TASK_OUTPUT_BYTES = STORAGE_MAX_TASK_OUTPUT_MB * 1024 * 1024


def _require_node_key(request: Request) -> None:
    """Auth helper — delegates to shared auth module."""
    require_node_key(request, BMAS_NODE_KEY)


def _check_bearer_or_pass(request: Request) -> None:
    """Auth helper — delegates to shared auth module."""
    check_bearer_or_pass(request, BMAS_NODE_KEY)


async def _ensure_task_output_dir(task_id: str) -> str:
    """Lazily create and return the task output directory.

    Uses the task slug from the task label, with collision resolution.
    Records the output_dir in the tasks row.
    """
    task = await db.get_task(task_id)
    if not task:
        raise ValueError("Task not found")

    # Check if output_dir is already set
    existing = task.get("output_dir")
    if existing and os.path.isdir(existing):
        return existing

    # Generate slug from task label
    label = task.get("label", task_id)
    slug = slugify_task(label)
    slug = resolve_slug_collision(STORAGE_ARTIFACTS_DIR, slug)

    output_dir = os.path.join(STORAGE_ARTIFACTS_DIR, slug)
    os.makedirs(output_dir, exist_ok=True)

    await db.update_task_output_dir(task_id, output_dir)
    logger.info(f"Created output dir for {task_id}: {output_dir}")
    return output_dir


@router.post("/ingest/artifacts/{task_id}/{turn_id}")
async def ingest_artifact(
    task_id: str,
    turn_id: str,
    request: Request,
    rel_path: str = Form(...),
    sha256: str = Form(...),
    author: str = Form(None),
    file: UploadFile = File(...),
):
    """Ingest an agent-created artifact from a node.

    Validates path safety, quota, sha256, and stores the file in the
    task's output directory with versioning.
    """
    if not STORAGE_ENABLED:
        return JSONResponse(
            {"error": "Storage is not enabled"},
            status_code=422,
        )

    # Auth: require node key
    try:
        _require_node_key(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=401)

    # Verify task exists
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    # Read file content
    content = await file.read()
    if len(content) == 0:
        return JSONResponse({"error": "Empty file"}, status_code=422)

    # Validate path traversal BEFORE touching the filesystem
    # Reject absolute paths before normalization
    normalized_check = rel_path.replace("\\", "/")
    if normalized_check.startswith("/"):
        return JSONResponse(
            {"error": "Path rejected: absolute paths not allowed"},
            status_code=422,
        )
    # Normalize the rel_path
    rel_path = normalized_check.strip("/")
    if not rel_path:
        return JSONResponse({"error": "Empty rel_path"}, status_code=422)

    # Get or create the output directory
    try:
        output_dir = await _ensure_task_output_dir(task_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    # Validate the path stays within the output directory
    try:
        safe_path = validate_path_traversal(rel_path, output_dir)
    except ValueError as e:
        logger.warning(f"Path traversal rejected for {task_id}: {rel_path} — {e}")
        return JSONResponse(
            {"error": f"Path rejected: {e}"},
            status_code=422,
        )

    # Verify sha256
    computed_sha256 = compute_sha256(content)
    if computed_sha256 != sha256:
        return JSONResponse(
            {"error": f"SHA256 mismatch: expected {sha256}, got {computed_sha256}"},
            status_code=422,
        )

    # Check quota
    current_total = await db.get_task_artifacts_total_bytes(task_id)
    if current_total + len(content) > _MAX_TASK_OUTPUT_BYTES:
        logger.warning(
            f"Artifact quota exceeded for {task_id}: "
            f"{current_total + len(content)} > {_MAX_TASK_OUTPUT_BYTES}"
        )
        return JSONResponse(
            {
                "error": f"Task output quota exceeded "
                f"({(current_total + len(content)) / (1024*1024):.1f}MB / {STORAGE_MAX_TASK_OUTPUT_MB}MB)"
            },
            status_code=413,
        )

    # Version handling — check if this path was already synced (B3 fix)
    current_version = await db.get_artifact_max_version(task_id, rel_path)
    new_version = current_version + 1

    # If file exists, archive previous version
    if os.path.exists(safe_path) and current_version > 0:
        versions_dir = os.path.join(output_dir, ".bmas-versions")
        os.makedirs(versions_dir, exist_ok=True)
        archive_name = f"{rel_path.replace('/', '__')}.v{current_version}"
        archive_path = os.path.join(versions_dir, archive_name)
        try:
            shutil.copy2(safe_path, archive_path)
        except Exception as e:
            logger.warning(f"Failed to archive {safe_path} → {archive_path}: {e}")

    # Write the file
    os.makedirs(os.path.dirname(safe_path), exist_ok=True)
    with open(safe_path, "wb") as f:
        f.write(content)

    # Insert DB row (B2 fix: positional args, not dict)
    artifact_id = f"a-{str(uuid.uuid4())[:8]}"
    mime = get_mime_type(rel_path.split("/")[-1]) if "/" in rel_path else get_mime_type(rel_path)
    await db.insert_artifact(
        artifact_id, task_id, turn_id, author,
        rel_path, safe_path, mime,
        len(content), computed_sha256, new_version,
    )

    # Emit SSE event
    try:
        from app import app
        orch = app.state.orchestrator
        await orch.bb.publish_event(task_id, "artifact_created", {
            "artifact_id": artifact_id,
            "rel_path": rel_path,
            "version": new_version,
            "bytes": len(content),
            "sha256": computed_sha256,
            "author": author,
            "turn_id": turn_id,
        })
    except Exception:
        pass  # SSE is best-effort

    # Post artifact board entry (doc 17 §6)
    try:
        from app import app  # noqa: PLC0415 — app not importable at module level
        orch = app.state.orchestrator
        body = (
            f"**{rel_path}** v{new_version} ({mime or 'unknown'}, "
            f"{len(content)} bytes, sha256: {computed_sha256[:16]}…)"
        )
        if author:
            body += f"\nProduced by: {author}"

        await orch.gateway.append(
            task_id=task_id,
            actor=author or "daemon",
            capabilities=["post:artifact"],
            proposed=[{
                "type": "artifact",
                "title": rel_path,
                "body": body,
                "confidence": 1.0,
            }],
            turn_id=turn_id,
            round_no=0,
        )
    except Exception as e:
        logger.warning("Failed to create artifact board entry for %s: %s", artifact_id, e)

    logger.info(
        f"Artifact ingested: {artifact_id} ({rel_path} v{new_version}, "
        f"{len(content)} bytes, sha256={computed_sha256[:16]}…)"
    )

    return {
        "artifact_id": artifact_id,
        "rel_path": rel_path,
        "version": new_version,
        "bytes": len(content),
        "sha256": computed_sha256,
    }


@router.get("/tasks/{task_id}/artifacts")
async def list_artifacts(task_id: str):
    """List all artifacts for a task."""
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    artifacts = await db.get_artifacts(task_id)
    return {
        "artifacts": [
            {
                "id": a["id"],
                "rel_path": a["rel_path"],
                "mime": a.get("mime"),
                "bytes": a["bytes"],
                "sha256": a["sha256"],
                "version": a["version"],
                "author": a.get("author"),
                "turn_id": a.get("turn_id"),
                "created_at": a["created_at"],
            }
            for a in artifacts
        ],
        "output_dir": task.get("output_dir"),
    }


@router.get("/tasks/{task_id}/artifacts/{artifact_id}")
async def download_artifact(task_id: str, artifact_id: str, request: Request):
    """Download an artifact file.

    Auth: dashboard session (no auth) or BMAS_NODE_KEY bearer.
    Forces download via Content-Disposition: attachment.
    """
    _check_bearer_or_pass(request)

    artifact = await db.get_artifact(artifact_id)
    if not artifact or artifact["task_id"] != task_id:
        return JSONResponse({"error": "Artifact not found"}, status_code=404)

    stored_path = artifact["stored_path"]
    if not os.path.exists(stored_path):
        return JSONResponse({"error": "Artifact not found on disk"}, status_code=404)

    filename = artifact["rel_path"].split("/")[-1] if "/" in artifact["rel_path"] else artifact["rel_path"]
    return FileResponse(
        path=stored_path,
        filename=filename,
        media_type=artifact.get("mime", "application/octet-stream"),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )
