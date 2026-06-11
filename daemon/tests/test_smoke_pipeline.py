# /opt/bmas/daemon/tests/test_smoke_pipeline.py
"""End-to-end smoke tests for the file/artifact pipeline.

Exercises the full HTTP stack (routes → file I/O → DB → board entries)
using FastAPI TestClient against a temp filesystem + SQLite DB.

Covers: upload, validation, extraction, artifact ingest, versioning,
auth, path traversal, downloads, PDF extraction, and filesystem
verification.  Runs in ~0.3s — fast enough for CI.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Helpers ──────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _init_test_db(path: str):
    import database as db
    db.DB_PATH = path
    await db.init_db()


async def _create_task(tid: str, title: str, brief: str):
    import database as db
    await db.create_task(tid, title, brief)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pipeline_env(tmp_path_factory):
    """Set up temp dirs, DB, config overrides, and FastAPI test client.

    Module-scoped so the DB and filesystem are shared across all smoke
    tests in this file (faster than per-test setup).
    """
    tmp = tmp_path_factory.mktemp("smoke")
    uploads = str(tmp / "uploads")
    output = str(tmp / "output")
    db_path = str(tmp / "smoke.db")
    os.makedirs(uploads)
    os.makedirs(output)

    # Patch the fake config injected by conftest.py
    import config
    orig = {}
    overrides = {
        "STORAGE_ENABLED": True,
        "STORAGE_USER_MEDIA_DIR": uploads,
        "STORAGE_ARTIFACTS_DIR": output,
        "STORAGE_MAX_UPLOAD_MB": 50,
        "STORAGE_MAX_TASK_OUTPUT_MB": 500,
        "STORAGE_ALLOWED_TYPES": {"pdf", "txt", "md", "csv", "json", "png", "jpg", "docx"},
        "STORAGE_PDF_EXTRACTION": "pymupdf",
        "STORAGE_EXTRACTION_MAX_CHARS": 60000,
        "BMAS_NODE_KEY": "smoke-test-key-12345",
    }
    for k, v in overrides.items():
        orig[k] = getattr(config, k, None)
        setattr(config, k, v)

    # Init DB + seed a task
    import database as db
    db.DB_PATH = db_path
    asyncio.run(_init_test_db(db_path))

    task_id = "task-smoke-001"
    asyncio.run(_create_task(task_id, "Smoke Test Task", "Pipeline verification"))

    # Build app — must import AFTER config is patched
    # Force reimport of route modules to pick up new config values
    import importlib
    import routes.files
    import routes.artifacts
    importlib.reload(routes.files)
    importlib.reload(routes.artifacts)

    app = FastAPI()
    app.include_router(routes.files.router)
    app.include_router(routes.artifacts.router)
    client = TestClient(app)

    yield {
        "client": client,
        "task_id": task_id,
        "uploads": uploads,
        "output": output,
        "node_key": "smoke-test-key-12345",
    }

    # Restore config + reload route modules so later test modules
    # pick up the original (conftest-injected) config values.
    for k, v in orig.items():
        setattr(config, k, v)
    import importlib as _il
    import routes.files as _rf
    import routes.artifacts as _ra
    _il.reload(_rf)
    _il.reload(_ra)


@pytest.fixture
def client(pipeline_env):
    return pipeline_env["client"]


@pytest.fixture
def task_id(pipeline_env):
    return pipeline_env["task_id"]


@pytest.fixture
def auth_headers(pipeline_env):
    return {"Authorization": f"Bearer {pipeline_env['node_key']}"}


# ══════════════════════════════════════════════════════════════════════
# Upload Pipeline
# ══════════════════════════════════════════════════════════════════════

class TestUploadPipeline:
    """Upload, validation, extraction, listing, download."""

    def test_upload_text_file(self, client, task_id):
        """Text file upload → 200, sha256 matches, text extracted."""
        content = b"Hello from the smoke test!\nEnd-to-end pipeline verification."
        expected_sha = _sha256(content)

        r = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("smoke-test.txt", content, "text/plain")},
        )
        assert r.status_code == 200
        d = r.json()
        assert d["sha256"] == expected_sha
        assert d["extracted_chars"] > 0
        assert d["filename"] == "smoke-test.txt"
        # Store for later tests
        self.__class__._txt_file_id = d["file_id"]
        self.__class__._txt_content = content

    def test_upload_rejects_oversized(self, client, task_id):
        """File > max_upload_mb → 413."""
        big = b"x" * (51 * 1024 * 1024)
        r = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("huge.txt", big, "text/plain")},
        )
        assert r.status_code == 413
        del big

    def test_upload_rejects_disallowed_type(self, client, task_id):
        """Disallowed extension (.exe) → 422."""
        r = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        )
        assert r.status_code == 422

    def test_upload_rejects_empty_file(self, client, task_id):
        """Empty file → 422."""
        r = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert r.status_code == 422

    def test_upload_sanitizes_traversal_filename(self, client, task_id):
        """Traversal in filename → sanitized, no '..' or '/' in stored name."""
        r = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("../../etc/passwd.txt", b"traversal attempt", "text/plain")},
        )
        assert r.status_code == 200
        name = r.json()["filename"]
        assert ".." not in name
        assert "/" not in name

    def test_list_files(self, client, task_id):
        """List files → includes uploaded files."""
        r = client.get(f"/tasks/{task_id}/files")
        assert r.status_code == 200
        files = r.json()["files"]
        assert len(files) >= 2
        names = [f["name"] for f in files]
        assert "smoke-test.txt" in names

    def test_get_extracted_text(self, client, task_id):
        """GET /text → extracted text matches original content."""
        fid = getattr(self.__class__, "_txt_file_id", None)
        if not fid:
            pytest.skip("text file not uploaded")
        r = client.get(f"/tasks/{task_id}/files/{fid}/text")
        assert r.status_code == 200
        assert "Hello from the smoke test" in r.json()["extracted_text"]

    def test_download_file(self, client, task_id):
        """Download → content matches, proper headers set."""
        fid = getattr(self.__class__, "_txt_file_id", None)
        content = getattr(self.__class__, "_txt_content", None)
        if not fid:
            pytest.skip("text file not uploaded")
        r = client.get(f"/tasks/{task_id}/files/{fid}")
        assert r.status_code == 200
        assert r.content == content
        assert "attachment" in r.headers.get("content-disposition", "")
        assert "nosniff" in r.headers.get("x-content-type-options", "")

    def test_upload_nonexistent_task(self, client):
        """Upload to nonexistent task → 404."""
        r = client.post(
            "/tasks/task-nonexistent/files",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# Artifact Pipeline
# ══════════════════════════════════════════════════════════════════════

class TestArtifactPipeline:
    """Ingest, versioning, auth, traversal, download."""

    def test_ingest_valid_artifact(self, client, task_id, auth_headers):
        """Valid artifact ingest → 200, version 1, sha256 matches."""
        content = b"def main():\n    print('Smoke test artifact v1')\n"
        expected_sha = _sha256(content)

        r = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=auth_headers,
            data={"rel_path": "src/main.py", "sha256": expected_sha},
            files={"file": ("main.py", content, "text/x-python")},
        )
        assert r.status_code == 200
        d = r.json()
        assert d["version"] == 1
        assert d["sha256"] == expected_sha
        assert d["rel_path"] == "src/main.py"
        self.__class__._art_id = d["artifact_id"]

    def test_versioning_bumps_on_resync(self, client, task_id, auth_headers):
        """Re-ingest same rel_path → version bumps to 2."""
        v2 = b"def main():\n    print('Updated to v2!')\n"
        r = client.post(
            f"/ingest/artifacts/{task_id}/turn-2",
            headers=auth_headers,
            data={"rel_path": "src/main.py", "sha256": _sha256(v2)},
            files={"file": ("main.py", v2, "text/x-python")},
        )
        assert r.status_code == 200
        assert r.json()["version"] == 2

    def test_path_traversal_rejected(self, client, task_id, auth_headers):
        """Relative traversal (../../etc/passwd) → 422."""
        evil = b"evil"
        r = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=auth_headers,
            data={"rel_path": "../../etc/passwd", "sha256": _sha256(evil)},
            files={"file": ("passwd", evil, "text/plain")},
        )
        assert r.status_code == 422

    def test_absolute_path_rejected(self, client, task_id, auth_headers):
        """Absolute path (/etc/shadow) → 422."""
        evil = b"evil"
        r = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=auth_headers,
            data={"rel_path": "/etc/shadow", "sha256": _sha256(evil)},
            files={"file": ("shadow", evil, "text/plain")},
        )
        assert r.status_code == 422

    def test_sha256_mismatch_rejected(self, client, task_id, auth_headers):
        """SHA-256 mismatch → 422 with 'mismatch' in error."""
        r = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=auth_headers,
            data={"rel_path": "bad.txt", "sha256": "0" * 64},
            files={"file": ("bad.txt", b"real content", "text/plain")},
        )
        assert r.status_code == 422
        assert "mismatch" in r.json()["error"].lower()

    def test_invalid_bearer_rejected(self, client, task_id):
        """Invalid bearer token → 401."""
        r = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers={"Authorization": "Bearer wrong-key"},
            data={"rel_path": "x.txt", "sha256": _sha256(b"x")},
            files={"file": ("x.txt", b"x", "text/plain")},
        )
        assert r.status_code == 401

    def test_missing_bearer_rejected(self, client, task_id):
        """Missing bearer token → 401."""
        r = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            data={"rel_path": "x.txt", "sha256": _sha256(b"x")},
            files={"file": ("x.txt", b"x", "text/plain")},
        )
        assert r.status_code == 401

    def test_list_artifacts(self, client, task_id):
        """List artifacts → includes both versions."""
        r = client.get(f"/tasks/{task_id}/artifacts")
        assert r.status_code == 200
        artifacts = r.json()["artifacts"]
        assert len(artifacts) >= 2
        versions = [a["version"] for a in artifacts if a["rel_path"] == "src/main.py"]
        assert 1 in versions
        assert 2 in versions

    def test_download_artifact(self, client, task_id):
        """Download artifact → 200, proper Content-Disposition."""
        aid = getattr(self.__class__, "_art_id", None)
        if not aid:
            pytest.skip("artifact not ingested")
        r = client.get(f"/tasks/{task_id}/artifacts/{aid}")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")


# ══════════════════════════════════════════════════════════════════════
# PDF Extraction
# ══════════════════════════════════════════════════════════════════════

class TestPDFExtraction:
    """PDF upload with text extraction via pymupdf."""

    @pytest.fixture(autouse=True)
    def _require_pymupdf(self):
        pytest.importorskip("pymupdf")

    def test_pdf_upload_and_extraction(self, client, task_id):
        """PDF upload → 200, text extracted with page markers."""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Smoke test PDF content.\nPage 1.", fontsize=12)
        page2 = doc.new_page()
        page2.insert_text((72, 72), "Page 2: extraction verification.", fontsize=12)
        pdf_bytes = doc.tobytes()
        doc.close()

        r = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("smoke.pdf", pdf_bytes, "application/pdf")},
        )
        assert r.status_code == 200
        d = r.json()
        assert d["sha256"] == _sha256(pdf_bytes)
        assert d["extracted_chars"] > 0

        # Verify text content via /text endpoint
        r2 = client.get(f"/tasks/{task_id}/files/{d['file_id']}/text")
        assert r2.status_code == 200
        text = r2.json()["extracted_text"]
        assert "Smoke test PDF" in text
        assert "[page 1]" in text
        assert "[page 2]" in text


# ══════════════════════════════════════════════════════════════════════
# Filesystem Verification
# ══════════════════════════════════════════════════════════════════════

class TestFilesystemVerification:
    """Verify files actually land on disk with expected structure."""

    def test_uploads_on_disk(self, pipeline_env):
        """Uploaded files and sidecar .extracted.txt exist on disk."""
        uploads = pipeline_env["uploads"]
        all_files = []
        for root, _, fnames in os.walk(uploads):
            for fn in fnames:
                all_files.append(os.path.relpath(os.path.join(root, fn), uploads))

        assert any("smoke-test.txt" in f for f in all_files), \
            f"smoke-test.txt not found in {all_files}"
        assert any(".extracted.txt" in f for f in all_files), \
            f"No sidecar .extracted.txt in {all_files}"

    def test_artifacts_on_disk(self, pipeline_env):
        """Artifact files and .bmas-versions/ archive exist on disk."""
        output = pipeline_env["output"]
        all_files = []
        for root, _, fnames in os.walk(output):
            for fn in fnames:
                all_files.append(os.path.relpath(os.path.join(root, fn), output))

        assert any("main.py" in f for f in all_files), \
            f"main.py not found in {all_files}"
        assert any(".bmas-versions" in f for f in all_files), \
            f".bmas-versions/ archive missing in {all_files}"
