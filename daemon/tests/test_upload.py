# /opt/bmas/daemon/tests/test_upload.py
"""
Tests for the file upload pipeline (routes/files.py).

Uses FastAPI TestClient with an in-memory SQLite database.
conftest.py injects a fake config module so routes/files.py can be
imported without triggering config.py's sys.exit(1).
"""

import os
import sys
import asyncio

import pytest

# conftest.py already injected fake config and added src to path


@pytest.fixture(autouse=True)
def mock_config(monkeypatch, tmp_path):
    """Override storage config to use temp directories."""
    upload_dir = str(tmp_path / "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    # Patch the fake config module values
    import config
    monkeypatch.setattr(config, "STORAGE_ENABLED", True)
    monkeypatch.setattr(config, "STORAGE_USER_MEDIA_DIR", upload_dir)
    monkeypatch.setattr(config, "STORAGE_MAX_UPLOAD_MB", 1)  # 1MB for tests
    monkeypatch.setattr(config, "STORAGE_ALLOWED_TYPES", {"pdf", "txt", "md", "csv", "json", "png"})
    monkeypatch.setattr(config, "STORAGE_PDF_EXTRACTION", "pymupdf")
    monkeypatch.setattr(config, "STORAGE_EXTRACTION_MAX_CHARS", 1000)
    monkeypatch.setattr(config, "BMAS_NODE_KEY", "")

    # Also patch the module-level computed values in routes.files
    # (these were captured at import time from config)
    import routes.files as rf
    monkeypatch.setattr(rf, "STORAGE_USER_MEDIA_DIR", upload_dir)
    monkeypatch.setattr(rf, "_MAX_UPLOAD_BYTES", 1 * 1024 * 1024)
    monkeypatch.setattr(rf, "STORAGE_ENABLED", True)
    monkeypatch.setattr(rf, "STORAGE_ALLOWED_TYPES", {"pdf", "txt", "md", "csv", "json", "png"})
    monkeypatch.setattr(rf, "STORAGE_PDF_EXTRACTION", "pymupdf")
    monkeypatch.setattr(rf, "STORAGE_EXTRACTION_MAX_CHARS", 1000)
    monkeypatch.setattr(rf, "BMAS_NODE_KEY", "")


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Use a temp SQLite database."""
    path = str(tmp_path / "test.db")
    monkeypatch.setattr("database.DB_PATH", path)
    asyncio.get_event_loop().run_until_complete(
        _init_test_db(path)
    )
    return path


async def _init_test_db(path):
    import database as db
    db.DB_PATH = path
    await db.init_db()


@pytest.fixture
def task_id(db_path):
    """Create a test task and return its ID."""
    tid = "task-test123"
    asyncio.get_event_loop().run_until_complete(
        _create_test_task(tid)
    )
    return tid


async def _create_test_task(tid):
    import database as db
    await db.create_task(tid, "Test task", "This is a test task")


@pytest.fixture
def client(db_path, mock_config):
    """FastAPI test client."""
    from fastapi.testclient import TestClient
    from routes.files import router
    from fastapi import FastAPI

    test_app = FastAPI()
    test_app.include_router(router)
    return TestClient(test_app)


class TestUploadValidation:
    def test_upload_valid_txt(self, client, task_id, monkeypatch):
        """Upload a valid text file."""
        content = b"Hello, this is a test file."
        response = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("test.txt", content, "text/plain")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "test.txt"
        assert data["bytes"] == len(content)
        assert data["sha256"]
        assert data["extracted_chars"] > 0

    def test_upload_exceeds_size_limit(self, client, task_id):
        """File larger than max_upload_mb should be rejected with 413."""
        content = b"x" * (2 * 1024 * 1024)  # 2MB > 1MB limit
        response = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("big.txt", content, "text/plain")},
        )
        assert response.status_code == 413
        assert "too large" in response.json()["error"].lower()

    def test_upload_disallowed_type(self, client, task_id):
        """File with disallowed extension should be rejected."""
        response = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("malware.exe", b"MZ", "application/octet-stream")},
        )
        assert response.status_code == 422
        assert "not allowed" in response.json()["error"].lower()

    def test_upload_sanitizes_filename(self, client, task_id):
        """Filename with traversal should be sanitized."""
        response = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("../../etc/passwd.txt", b"test", "text/plain")},
        )
        assert response.status_code == 200
        data = response.json()
        assert ".." not in data["filename"]
        assert "/" not in data["filename"]

    def test_upload_empty_file(self, client, task_id):
        """Empty files should be rejected."""
        response = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert response.status_code == 422

    def test_upload_nonexistent_task(self, client):
        """Upload to nonexistent task should 404."""
        response = client.post(
            "/tasks/task-nonexistent/files",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 404

    def test_list_files(self, client, task_id):
        """List files after upload."""
        # Upload a file first
        client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )

        response = client.get(f"/tasks/{task_id}/files")
        assert response.status_code == 200
        data = response.json()
        assert len(data["files"]) == 1
        assert data["files"][0]["name"] == "test.txt"


class TestExtractionCaps:
    def test_text_extraction_within_cap(self, client, task_id):
        """Extracted text should respect extraction_max_chars."""
        content = b"x" * 500  # Within 1000 char cap
        response = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("data.txt", content, "text/plain")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["extracted_chars"] == 500

    def test_text_extraction_truncates(self, client, task_id):
        """Text longer than cap should be truncated."""
        content = b"y" * 2000  # Exceeds 1000 char cap
        response = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("long.txt", content, "text/plain")},
        )
        assert response.status_code == 200
        data = response.json()
        # Should be truncated to around max_chars + marker
        assert data["extracted_chars"] < 2000


class TestFileTextEndpoint:
    """Tests for the /text endpoint (B5 fix: sidecar extracted text)."""

    def test_get_extracted_text(self, client, task_id):
        """Extracted text should be retrievable via /text endpoint."""
        content = b"This is extracted content."
        # Upload
        resp = client.post(
            f"/tasks/{task_id}/files",
            files={"file": ("sample.txt", content, "text/plain")},
        )
        assert resp.status_code == 200
        file_id = resp.json()["file_id"]

        # Get text
        text_resp = client.get(f"/tasks/{task_id}/files/{file_id}/text")
        assert text_resp.status_code == 200
        data = text_resp.json()
        assert data["extracted_text"] == "This is extracted content."
        assert data["extracted_chars"] > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
