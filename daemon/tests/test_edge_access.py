"""Authentication and object access at the daemon edge.

With an operator key configured, every request outside the public
health surface needs the operator key or the node key, the read routes
check object access for the resolved principal, the Recovery Center
and the runtime-pair capability records answer over HTTP, and the
Foundation admission writer consults its gates and the live
qualification records.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient

import database as db
import edge_access
from access_control import AccessDeniedError, ObjectRef, Principal, check_access
from core import foundation_gates


@pytest.fixture
def keyed_client(tmp_path, monkeypatch):
    """A daemon app with the edge middleware and the routed read surfaces."""
    import asyncio

    from fastapi import FastAPI

    from routes import capabilities, recovery, tasks

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "edge.db"))
    asyncio.run(db.init_db())
    monkeypatch.setattr(edge_access, "operator_key", lambda: "operator-secret")
    monkeypatch.setattr(edge_access, "node_key", lambda: "node-secret")
    application = FastAPI()
    application.middleware("http")(edge_access.enforce_edge_access)

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    application.include_router(tasks.router)
    application.include_router(capabilities.router)
    application.include_router(recovery.router)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def test_public_paths_stay_open_and_everything_else_needs_a_key(keyed_client):
    assert keyed_client.get("/health").status_code == 200
    unauthenticated = keyed_client.get("/tasks/task-missing")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
    wrong = keyed_client.get("/tasks/task-missing", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401
    operator = keyed_client.get("/tasks/task-missing", headers={"Authorization": "Bearer operator-secret"})
    assert operator.status_code == 404
    header = keyed_client.get("/tasks/task-missing", headers={"X-BMAS-API-Key": "operator-secret"})
    assert header.status_code == 404
    node = keyed_client.get("/tasks/task-missing", headers={"Authorization": "Bearer node-secret"})
    assert node.status_code == 404
    listing = keyed_client.get("/capabilities/runtime-pairs", headers={"Authorization": "Bearer operator-secret"})
    assert listing.status_code == 200
    ids = {(r["runtime_key"]["runtime_id"], r["runtime_key"]["runtime_contract_version"]) for r in listing.json()["records"]}
    assert ("reference", "1") in ids and ("classic", "2") in ids
    assert {"runtime_id": "classic", "runtime_contract_version": "2"} in listing.json()["planned"]


def test_recovery_center_answers_over_http(keyed_client):
    headers = {"Authorization": "Bearer operator-secret"}
    queues = keyed_client.get("/recovery-center/queues", headers=headers)
    assert queues.status_code == 200, queues.text
    body = queues.json()
    assert set(body["queues"]) == set(body["counts"])
    assert "unknown_effects" in body["queues"]
    assert "reconcile_by_lookup" in body["actions"]
    one = keyed_client.get("/recovery-center/queues/dead_letters", headers=headers)
    assert one.status_code == 200 and one.json()["count"] == 0
    assert keyed_client.get("/recovery-center/queues/not-a-queue", headers=headers).status_code == 404
    unknown = keyed_client.post("/recovery-center/actions/erase_everything", json={"run_id": "run-x"}, headers=headers)
    assert unknown.status_code == 404
    missing = keyed_client.post("/recovery-center/actions/pause_new_work", json={"run_id": "run-x", "arguments": {}}, headers=headers)
    assert missing.status_code in (409, 422), missing.text
    assert keyed_client.get("/recovery-center/queues").status_code == 401


def test_object_access_denies_another_tenant_and_the_viewer_write():
    viewer = Principal(principal_id="v", tenant_id="tenant-default", roles=("read_only_viewer",))
    assert check_access(viewer, "read", ObjectRef(kind="task", tenant_id="tenant-default", object_id="task-a"))["authorized"]
    with pytest.raises(AccessDeniedError) as denied:
        check_access(viewer, "read", ObjectRef(kind="task", tenant_id="tenant-other", object_id="task-a"))
    assert denied.value.reason == "tenant_boundary"
    with pytest.raises(AccessDeniedError):
        check_access(viewer, "write", ObjectRef(kind="task", tenant_id="tenant-default", object_id="task-a"))
    # Outside a request the local operator reads; a foreign tenant object still denies.
    assert edge_access.authorize_read("task", "task-a")["authorized"]
    with pytest.raises(HTTPException) as failure:
        edge_access.authorize_read("task", "task-a", tenant_id="tenant-other")
    assert failure.value.status_code == 403


@pytest_asyncio.fixture
async def admission_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "admission.db"))
    await db.init_db()


@pytest.mark.asyncio
async def test_the_admission_writer_consults_its_gates_and_live_qualifications(admission_db, monkeypatch):
    from test_run_admission import (
        BASE_TIME,
        QUALIFICATIONS,
        STORAGE_READY,
        TASK_ID,
        build_request,
    )

    import config
    import run_admission

    await db.create_task_with_meta(
        TASK_ID, "admit", "admit", "classic", {}, runtime_contract_version="1",
    )
    request = build_request()
    readers = frozenset({"reader.checkpoint"})
    monkeypatch.setattr(config, "FOUNDATION_GATES", {}, raising=False)
    with pytest.raises(foundation_gates.WriterDisabledError, match="run_context"):
        await run_admission.admit_run(
            request, available_reader_ids=readers,
            qualification_fixture=QUALIFICATIONS, storage_report=STORAGE_READY,
            database_time=BASE_TIME,
        )
    monkeypatch.setattr(config, "FOUNDATION_GATES", {name: True for name in foundation_gates.PLANNED_WRITER_GATES}, raising=False)
    # Without a fixture the writer reads the live records, which are absent.
    with pytest.raises(run_admission.AdmissionPrerequisiteError, match="missing"):
        await run_admission.admit_run(
            request, available_reader_ids=readers,
            storage_report=STORAGE_READY, database_time=BASE_TIME,
        )
    live = await run_admission.live_qualifications(request.required_qualification_ids)
    assert live == {}
