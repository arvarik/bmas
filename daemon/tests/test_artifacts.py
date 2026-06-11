# /opt/bmas/daemon/tests/test_artifacts.py
"""
Tests for the artifact ingest pipeline (routes/artifacts.py).

Uses FastAPI TestClient with an in-memory SQLite database.
conftest.py injects a fake config module so routes/artifacts.py can be
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
    artifacts_dir = str(tmp_path / "output")
    os.makedirs(artifacts_dir, exist_ok=True)

    import config
    monkeypatch.setattr(config, "STORAGE_ENABLED", True)
    monkeypatch.setattr(config, "STORAGE_ARTIFACTS_DIR", artifacts_dir)
    monkeypatch.setattr(config, "STORAGE_MAX_TASK_OUTPUT_MB", 1)
    monkeypatch.setattr(config, "BMAS_NODE_KEY", "test-node-key")

    import routes.artifacts as ra
    monkeypatch.setattr(ra, "STORAGE_ENABLED", True)
    monkeypatch.setattr(ra, "STORAGE_ARTIFACTS_DIR", artifacts_dir)
    monkeypatch.setattr(ra, "_MAX_TASK_OUTPUT_BYTES", 1 * 1024 * 1024)
    monkeypatch.setattr(ra, "BMAS_NODE_KEY", "test-node-key")

    return artifacts_dir


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
    tid = "task-artifact1"
    asyncio.get_event_loop().run_until_complete(
        _create_test_task(tid)
    )
    return tid


async def _create_test_task(tid):
    import database as db
    await db.create_task(tid, "Test artifact task", "Build something")


@pytest.fixture
def client(db_path, mock_config):
    """FastAPI test client."""
    from fastapi.testclient import TestClient
    from routes.artifacts import router
    from fastapi import FastAPI

    test_app = FastAPI()
    test_app.include_router(router)
    return TestClient(test_app)


def _auth_headers():
    return {"Authorization": "Bearer test-node-key"}


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


class TestArtifactIngest:
    def test_ingest_valid(self, client, task_id):
        """Valid artifact ingest should succeed."""
        content = b"print('hello world')"
        sha = _sha256(content)

        response = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=_auth_headers(),
            data={"rel_path": "src/main.py", "sha256": sha},
            files={"file": ("main.py", content, "text/x-python")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["rel_path"] == "src/main.py"
        assert data["version"] == 1
        assert data["sha256"] == sha

    def test_path_traversal_rejected(self, client, task_id):
        """rel_path with .. should be rejected."""
        content = b"evil content"
        sha = _sha256(content)

        response = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=_auth_headers(),
            data={"rel_path": "../../evil.txt", "sha256": sha},
            files={"file": ("evil.txt", content, "text/plain")},
        )
        assert response.status_code == 422
        assert "traversal" in response.json()["error"].lower() or "rejected" in response.json()["error"].lower()

    def test_absolute_path_rejected(self, client, task_id):
        """Absolute rel_path should be rejected."""
        content = b"evil content"
        sha = _sha256(content)

        response = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=_auth_headers(),
            data={"rel_path": "/etc/passwd", "sha256": sha},
            files={"file": ("passwd", content, "text/plain")},
        )
        # Absolute paths get stripped to relative by our normalization
        # The validate_path_traversal will catch it
        assert response.status_code == 422

    def test_quota_exceeded(self, client, task_id):
        """Exceeding task output quota should return 413."""
        # Upload a file that's larger than the 1MB limit
        content = b"x" * (2 * 1024 * 1024)  # 2MB > 1MB
        sha = _sha256(content)

        response = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=_auth_headers(),
            data={"rel_path": "big.bin", "sha256": sha},
            files={"file": ("big.bin", content, "application/octet-stream")},
        )
        assert response.status_code == 413
        assert "quota" in response.json()["error"].lower()

    def test_versioning(self, client, task_id):
        """Re-syncing the same rel_path should bump the version."""
        content_v1 = b"version 1"
        sha_v1 = _sha256(content_v1)

        # First upload
        r1 = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=_auth_headers(),
            data={"rel_path": "readme.md", "sha256": sha_v1},
            files={"file": ("readme.md", content_v1, "text/markdown")},
        )
        assert r1.status_code == 200
        assert r1.json()["version"] == 1

        # Second upload (same path, different content)
        content_v2 = b"version 2 - updated"
        sha_v2 = _sha256(content_v2)

        r2 = client.post(
            f"/ingest/artifacts/{task_id}/turn-2",
            headers=_auth_headers(),
            data={"rel_path": "readme.md", "sha256": sha_v2},
            files={"file": ("readme.md", content_v2, "text/markdown")},
        )
        assert r2.status_code == 200
        assert r2.json()["version"] == 2

    def test_sha256_mismatch(self, client, task_id):
        """Mismatched sha256 should be rejected."""
        content = b"hello"
        wrong_sha = "0" * 64

        response = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=_auth_headers(),
            data={"rel_path": "file.txt", "sha256": wrong_sha},
            files={"file": ("file.txt", content, "text/plain")},
        )
        assert response.status_code == 422
        assert "mismatch" in response.json()["error"].lower()

    def test_invalid_bearer_rejected(self, client, task_id):
        """Invalid bearer token should be rejected."""
        content = b"hello"
        sha = _sha256(content)

        response = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers={"Authorization": "Bearer wrong-key"},
            data={"rel_path": "file.txt", "sha256": sha},
            files={"file": ("file.txt", content, "text/plain")},
        )
        assert response.status_code == 401

    def test_missing_bearer_rejected(self, client, task_id):
        """Missing bearer token should be rejected on ingest."""
        content = b"hello"
        sha = _sha256(content)

        response = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            data={"rel_path": "file.txt", "sha256": sha},
            files={"file": ("file.txt", content, "text/plain")},
        )
        assert response.status_code == 401

    def test_list_artifacts(self, client, task_id):
        """List artifacts after ingest."""
        content = b"print('hi')"
        sha = _sha256(content)

        client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=_auth_headers(),
            data={"rel_path": "app.py", "sha256": sha},
            files={"file": ("app.py", content, "text/x-python")},
        )

        response = client.get(f"/tasks/{task_id}/artifacts")
        assert response.status_code == 200
        data = response.json()
        assert len(data["artifacts"]) == 1
        assert data["artifacts"][0]["rel_path"] == "app.py"

    def test_empty_file_rejected(self, client, task_id):
        """Empty artifact file should be rejected."""
        sha = _sha256(b"")

        response = client.post(
            f"/ingest/artifacts/{task_id}/turn-1",
            headers=_auth_headers(),
            data={"rel_path": "empty.txt", "sha256": sha},
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert response.status_code == 422


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
