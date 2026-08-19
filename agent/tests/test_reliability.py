"""Reliability tests for agent execution and trace delivery."""

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api_server


@pytest.fixture(autouse=True)
def isolated_activation_cache(tmp_path, monkeypatch):
    """Keep durable activation results isolated between tests."""
    monkeypatch.setattr(api_server, "ACTIVATION_CACHE_DIR", tmp_path / "activations")
    api_server._activation_inflight.clear()
    api_server._activation_results.clear()
    api_server._activation_result_fingerprints.clear()


class FakeResponse:
    """Small HTTP response test double."""

    def __init__(self, status_code=200, data=None, text="", headers=None):
        self.status_code = status_code
        self._data = data or {}
        self.text = text
        self.headers = headers or {}
        self.content = (
            text.encode("utf-8")
            if text
            else json.dumps(self._data).encode("utf-8")
        )

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeStream:
    """SSE stream test double."""

    def __init__(self, lines=None, delay=None):
        self.lines = lines or []
        self.delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        if self.delay is not None:
            await asyncio.sleep(self.delay)
        for line in self.lines:
            yield line


class FakeRunsClient:
    """Hermes client test double with captured calls."""

    def __init__(self, stream, poll=None, submit=None):
        self.event_stream = stream
        self.poll = poll or FakeResponse(200, {"status": "completed"})
        self.submit = list(submit or [])
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        if url.endswith("/v1/runs"):
            if self.submit:
                return self.submit.pop(0)
            return FakeResponse(200, {"run_id": "run-1"})
        return FakeResponse(202)

    async def get(self, url, headers=None, timeout=None):
        return self.poll

    def stream(self, method, url, headers=None, timeout=None):
        return self.event_stream


def run_api(client, **overrides):
    """Run the API function with stable default arguments."""
    args = {
        "description": "Do the task",
        "role_prompt": None,
        "context": None,
        "task_id": "task-1",
        "turn_id": "turn-1",
        "role": "planner",
        "model": "selected-model",
        "request_id": "request-1",
        "session_id": "actor-session-9",
        "timeout": 2,
    }
    args.update(overrides)

    async def scenario():
        original = api_server.httpx.AsyncClient
        api_server.httpx.AsyncClient = lambda *unused_args, **unused_kwargs: client
        try:
            return await api_server._run_via_api(**args)
        finally:
            api_server.httpx.AsyncClient = original

    return asyncio.run(scenario())


def test_runs_api_uses_daemon_session_and_selected_model(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    completed = FakeStream([
        "event: run.completed",
        'data: {"output":"done","usage":{"input_tokens":2,"output_tokens":1}}',
        "",
    ])
    client = FakeRunsClient(completed)

    status, output, usage, _, run_id = run_api(client)

    assert status == api_server.TaskStatus.completed
    assert output == "done"
    assert usage["model"] == "selected-model"
    assert run_id == "run-1"
    assert client.posts[0]["json"]["session_id"] == "actor-session-9"
    assert client.posts[0]["json"]["model"] == "selected-model"


def test_runs_api_uses_stable_task_actor_memory_scope(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    monkeypatch.setattr(api_server, "NODE_ID", "node-a")
    clients = []

    for session_id in ("task-session-1", "task-session-2"):
        completed = FakeStream([
            "event: run.completed",
            'data: {"output":"done"}',
            "",
        ])
        client = FakeRunsClient(completed)
        run_api(client, session_id=session_id, profile="planner")
        clients.append(client)

    first_headers = clients[0].posts[0]["headers"]
    second_headers = clients[1].posts[0]["headers"]
    assert first_headers["X-Hermes-Session-Key"] == "bmas:task-session-1"
    assert second_headers["X-Hermes-Session-Key"] == "bmas:task-session-2"
    assert second_headers["X-Hermes-Session-Key"] != first_headers[
        "X-Hermes-Session-Key"
    ]
    assert clients[0].posts[0]["json"]["session_id"] == "task-session-1"
    assert clients[1].posts[0]["json"]["session_id"] == "task-session-2"


def test_session_key_hashes_invalid_or_oversized_scope():
    first = api_server._stable_hermes_session_key("x" * 300)
    second = api_server._stable_hermes_session_key("x" * 300)
    controlled = api_server._stable_hermes_session_key("node\nscope")

    assert first == second
    assert first.startswith("bmas-sha256:")
    assert controlled.startswith("bmas-sha256:")
    assert len(first) <= 256


def test_runs_api_retries_explicit_capacity_response(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    monkeypatch.setattr(api_server, "HERMES_429_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(api_server, "HERMES_429_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr(api_server, "HERMES_429_RETRY_MAX_SECONDS", 0)
    client = FakeRunsClient(
        FakeStream([
            "event: run.completed",
            'data: {"output":"done"}',
            "",
        ]),
        submit=[
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(202, {"run_id": "run-after-backoff"}),
        ],
    )

    status, output, _, _, run_id = run_api(client)

    run_posts = [call for call in client.posts if call["url"].endswith("/v1/runs")]
    assert status == api_server.TaskStatus.completed
    assert output == "done"
    assert run_id == "run-after-backoff"
    assert len(run_posts) == 3


def test_runs_api_propagates_exhausted_capacity(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    monkeypatch.setattr(api_server, "HERMES_429_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(api_server, "HERMES_429_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr(api_server, "HERMES_429_RETRY_MAX_SECONDS", 0)
    client = FakeRunsClient(
        FakeStream([]),
        submit=[
            FakeResponse(429, headers={"Retry-After": "3"}),
            FakeResponse(429, headers={"Retry-After": "3"}),
        ],
    )

    with pytest.raises(api_server.HermesCapacityError) as raised:
        run_api(client)

    assert raised.value.retry_after_seconds == 0
    assert raised.value.retry_after_header == "3"


def test_runs_api_returns_capacity_before_backoff_uses_task_deadline(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    monkeypatch.setattr(api_server, "HERMES_429_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(api_server, "HERMES_429_RETRY_BASE_SECONDS", 0.5)
    monkeypatch.setattr(api_server, "HERMES_429_RETRY_MAX_SECONDS", 5)
    client = FakeRunsClient(
        FakeStream([]),
        submit=[FakeResponse(429, headers={"Retry-After": "5"})],
    )

    with pytest.raises(api_server.HermesCapacityError) as raised:
        run_api(client, timeout=2)

    run_posts = [call for call in client.posts if call["url"].endswith("/v1/runs")]
    assert len(run_posts) == 1
    assert raised.value.retry_after_seconds == 5
    assert raised.value.retry_after_header == "5"


def test_capacity_error_releases_unsubmitted_activation():
    key = "task-capacity:activation-1"
    fingerprint = "fingerprint"
    assert api_server._claim_activation(
        key, fingerprint, "task-capacity", "turn-1"
    )

    async def factory():
        raise api_server.HermesCapacityError(2)

    with pytest.raises(api_server.HermesCapacityError):
        asyncio.run(
            api_server._run_durable_activation(key, fingerprint, factory)
        )

    assert api_server._load_activation_record(key, time.time()) is None


def test_capacity_error_handler_preserves_retry_after():
    response = asyncio.run(
        api_server.hermes_capacity_error_handler(
            SimpleNamespace(), api_server.HermesCapacityError(2.1)
        )
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3"


def test_sse_end_without_terminal_does_not_report_completed(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    client = FakeRunsClient(FakeStream([]), poll=FakeResponse(404))

    status, output, _, _, _ = run_api(client)

    assert status == api_server.TaskStatus.failed
    assert "lost the run" in output


def test_total_timeout_stops_remote_run(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    client = FakeRunsClient(FakeStream(delay=1))

    status, output, _, _, _ = run_api(client, timeout=0.01)

    assert status == api_server.TaskStatus.timeout
    assert "timed out" in output
    assert any(call["url"].endswith("/run-1/stop") for call in client.posts)


def test_cli_fallback_preserves_selected_model(monkeypatch):
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"done", b""

    async def fake_subprocess(*args, **kwargs):
        captured["args"] = args
        return FakeProcess()

    monkeypatch.setattr(api_server.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)

    result = asyncio.run(api_server._run_hermes(
        description="Do the task",
        role_prompt=None,
        context=None,
        timeout=30,
        request_id="request-1",
        model="daemon-model",
    ))

    model_index = captured["args"].index("--model") + 1
    assert captured["args"][model_index] == "daemon-model"
    assert result[0] == api_server.TaskStatus.completed


def test_execute_authentication_is_optional_and_constant(monkeypatch):
    request = SimpleNamespace(headers={})
    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "")
    api_server._authorize_execute(request)

    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "secret")
    request.headers = {"Authorization": "Bearer secret"}
    api_server._authorize_execute(request)
    request.headers = {"X-BMAS-Execute-Key": "secret"}
    api_server._authorize_execute(request)


def test_execute_authentication_rejects_bad_key(monkeypatch):
    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "secret")
    request = SimpleNamespace(headers={"Authorization": "Bearer wrong"})

    try:
        api_server._authorize_execute(request)
    except api_server.HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("The execute endpoint accepted an invalid key")


def _complete_capabilities():
    return {
        "object": "hermes.api_server.capabilities",
        "features": {
            "run_submission": True,
            "run_status": True,
            "run_events_sse": True,
            "run_stop": True,
        },
        "endpoints": {
            "runs": {"method": "POST", "path": "/v1/runs"},
            "run_status": {
                "method": "GET",
                "path": "/v1/runs/{run_id}",
            },
            "run_events": {
                "method": "GET",
                "path": "/v1/runs/{run_id}/events",
            },
            "run_stop": {
                "method": "POST",
                "path": "/v1/runs/{run_id}/stop",
            },
        },
    }


def test_detailed_health_requires_capabilities_and_upstream_readiness(
    monkeypatch,
):
    calls = []

    class HealthClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers=None):
            calls.append({"url": url, "headers": headers})
            if url.endswith("/health/readiness"):
                return FakeResponse(200, {"status": "ready"})
            if url.endswith("/v1/capabilities"):
                return FakeResponse(200, _complete_capabilities())
            if url.endswith("/health/detailed"):
                return FakeResponse(200, {
                    "status": "ready",
                    "version": "0.20.4",
                    "readiness": {"status": "ready", "disk": "ready"},
                })
            raise AssertionError(f"Unexpected health URL: {url}")

    client = HealthClient()
    monkeypatch.setattr(api_server.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(api_server, "HERMES_GATEWAY_URL", "http://hermes")
    monkeypatch.setattr(api_server, "HERMES_GATEWAY_KEY", "gateway-key")
    monkeypatch.setattr(api_server, "EXECUTION_BACKEND", "hermes")
    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "")

    response = asyncio.run(api_server.detailed_health(SimpleNamespace(headers={})))

    assert response.ready is True
    assert response.runs_api_ready is True
    assert response.hermes_status == "ready"
    assert response.hermes_version == "0.20.4"
    assert response.capabilities == _complete_capabilities()
    assert response.upstream_readiness["disk"] == "ready"
    assert response.missing_required_features == []
    assert response.missing_required_endpoints == []
    hermes_calls = [call for call in calls if "hermes" in call["url"]]
    assert all(
        call["headers"] == {"Authorization": "Bearer gateway-key"}
        for call in hermes_calls
    )


def test_detailed_health_rejects_incomplete_capability_contract(monkeypatch):
    capabilities = _complete_capabilities()
    capabilities["features"].pop("run_stop")
    capabilities["endpoints"].pop("run_stop")

    class HealthClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers=None):
            if url.endswith("/health/readiness"):
                return FakeResponse(200)
            if url.endswith("/v1/capabilities"):
                return FakeResponse(200, capabilities)
            return FakeResponse(200, {
                "status": "ready",
                "readiness": {"status": "ready"},
            })

    monkeypatch.setattr(
        api_server.httpx, "AsyncClient", lambda **_kwargs: HealthClient()
    )
    monkeypatch.setattr(api_server, "HERMES_GATEWAY_URL", "http://hermes")
    monkeypatch.setattr(api_server, "EXECUTION_BACKEND", "hermes")
    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "")

    response = asyncio.run(api_server.detailed_health(SimpleNamespace(headers={})))

    assert response.ready is False
    assert response.runs_api_available is True
    assert response.runs_api_ready is False
    assert response.missing_required_features == ["run_stop"]
    assert response.missing_required_endpoints == ["run_stop"]


def test_detailed_health_rejects_invalid_execute_key(monkeypatch):
    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "execute-key")

    with pytest.raises(api_server.HTTPException) as exc_info:
        asyncio.run(api_server.detailed_health(SimpleNamespace(headers={})))

    assert exc_info.value.status_code == 401


def test_hermes_proxy_preserves_query_status_and_auth(monkeypatch):
    calls = []

    class ProxyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return FakeResponse(
                429,
                {"error": "busy"},
                headers={
                    "Retry-After": "4",
                    "Content-Type": "application/json",
                },
            )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = api_server.Request({
        "type": "http",
        "method": "GET",
        "path": "/v1/skills",
        "query_string": b"category=web&category=system",
        "headers": [(b"authorization", b"Bearer execute-key")],
    }, receive)
    monkeypatch.setattr(
        api_server.httpx, "AsyncClient", lambda **_kwargs: ProxyClient()
    )
    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "execute-key")
    monkeypatch.setattr(api_server, "HERMES_GATEWAY_URL", "http://hermes")
    monkeypatch.setattr(api_server, "HERMES_GATEWAY_KEY", "gateway-key")

    response = asyncio.run(api_server.proxy_hermes_skills(request))

    assert response.status_code == 429
    assert response.headers["retry-after"] == "4"
    assert calls == [{
        "method": "GET",
        "url": "http://hermes/v1/skills",
        "params": [("category", "web"), ("category", "system")],
        "content": None,
        "headers": {"Authorization": "Bearer gateway-key"},
    }]


def test_hermes_proxy_forwards_approval_body_to_encoded_run(monkeypatch):
    calls = []

    class ProxyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return FakeResponse(200, {"resolved": 1})

    body = b'{"choice":"session"}'

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = api_server.Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/runs/run%2Fone/approval",
        "query_string": b"all=true",
        "headers": [
            (b"x-bmas-execute-key", b"execute-key"),
            (b"content-type", b"application/json"),
        ],
    }, receive)
    monkeypatch.setattr(
        api_server.httpx, "AsyncClient", lambda **_kwargs: ProxyClient()
    )
    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "execute-key")
    monkeypatch.setattr(api_server, "HERMES_GATEWAY_URL", "http://hermes")
    monkeypatch.setattr(api_server, "HERMES_GATEWAY_KEY", "gateway-key")

    response = asyncio.run(
        api_server.proxy_hermes_run_approval("run/one", request)
    )

    assert response.status_code == 200
    assert calls == [{
        "method": "POST",
        "url": "http://hermes/v1/runs/run%2Fone/approval",
        "params": [("all", "true")],
        "content": body,
        "headers": {
            "Authorization": "Bearer gateway-key",
            "Content-Type": "application/json",
        },
    }]


def test_hermes_proxy_routes_cover_control_and_session_contract():
    route_contracts = {
        (method, route.path)
        for route in api_server.app.routes
        for method in (route.methods or set())
    }
    expected = {
        ("GET", "/v1/capabilities"),
        ("GET", "/v1/skills"),
        ("GET", "/v1/toolsets"),
        ("GET", "/api/sessions"),
        ("GET", "/api/sessions/{session_id}"),
        ("GET", "/api/sessions/{session_id}/messages"),
        ("POST", "/api/sessions/{session_id}/fork"),
        ("POST", "/v1/runs/{run_id}/approval"),
        ("POST", "/v1/runs/{run_id}/steer"),
    }
    assert expected.issubset(route_contracts)


def test_duplicate_activation_runs_once():
    calls = 0

    async def scenario():
        nonlocal calls
        api_server._activation_lock = asyncio.Lock()
        api_server._activation_inflight.clear()
        api_server._activation_results.clear()

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return api_server.TaskResponse(
                task_id="task-1",
                status=api_server.TaskStatus.completed,
                result="done",
                node_id="node-1",
                request_id="request-1",
                duration_ms=10,
                timestamp="2026-08-18T00:00:00+00:00",
            )

        first, second = await asyncio.gather(
            api_server._execute_idempotent("task-1:activation-1", factory),
            api_server._execute_idempotent("task-1:activation-1", factory),
        )
        cached = await api_server._execute_idempotent(
            "task-1:activation-1", factory
        )
        return first, second, cached

    first, second, cached = asyncio.run(scenario())
    assert calls == 1
    assert first.result == second.result == cached.result == "done"


def test_activation_result_survives_memory_reset():
    calls = 0

    async def scenario():
        nonlocal calls
        api_server._activation_lock = asyncio.Lock()

        async def factory():
            nonlocal calls
            calls += 1
            return api_server.TaskResponse(
                task_id="task-durable",
                status=api_server.TaskStatus.completed,
                result="durable",
                node_id="node-1",
                request_id="request-1",
                duration_ms=10,
                timestamp="2026-08-18T00:00:00+00:00",
            )

        first = await api_server._execute_idempotent(
            "task-durable:activation-1", factory,
        )
        api_server._activation_results.clear()
        second = await api_server._execute_idempotent(
            "task-durable:activation-1", factory,
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert calls == 1
    assert first.result == second.result == "durable"


def test_task_cancel_endpoint_stops_matching_activations(monkeypatch):
    monkeypatch.setattr(api_server, "BMAS_EXECUTE_KEY", "")

    async def scenario():
        api_server._activation_lock = asyncio.Lock()
        blocker = asyncio.Event()
        matching = asyncio.create_task(blocker.wait())
        unrelated = asyncio.create_task(blocker.wait())
        api_server._activation_inflight.update({
            "task-1:activation-a": matching,
            "task-2:activation-b": unrelated,
        })
        result = await api_server.cancel_task_activations(
            "task-1", SimpleNamespace(headers={}),
        )
        assert matching.cancelled()
        assert not unrelated.done()
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)
        return result

    result = asyncio.run(scenario())
    assert result == {"task_id": "task-1", "cancelled": 1}


def test_disconnected_waiter_does_not_cancel_shared_activation():
    calls = 0

    async def scenario():
        nonlocal calls
        api_server._activation_lock = asyncio.Lock()
        api_server._activation_inflight.clear()
        api_server._activation_results.clear()
        started = asyncio.Event()
        release = asyncio.Event()

        async def factory():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return api_server.TaskResponse(
                task_id="task-1",
                status=api_server.TaskStatus.completed,
                result="done",
                node_id="node-1",
                request_id="request-1",
                duration_ms=10,
                timestamp="2026-08-18T00:00:00+00:00",
            )

        waiter = asyncio.create_task(
            api_server._execute_idempotent("task-1:activation-1", factory)
        )
        await started.wait()
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass

        retry = asyncio.create_task(
            api_server._execute_idempotent("task-1:activation-1", factory)
        )
        release.set()
        return await retry

    response = asyncio.run(scenario())
    assert calls == 1
    assert response.result == "done"


def test_execute_passes_session_model_and_timeout(monkeypatch):
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return api_server.TaskStatus.completed, "done", None, 0, "run-1"

    async def scenario():
        monkeypatch.setattr(api_server, "HERMES_GATEWAY_URL", "http://hermes")
        monkeypatch.setattr(api_server, "_run_via_api", fake_run)
        request = api_server.TaskRequest(
            task_id="task-1",
            description="Do the task",
            turn_id="turn-1",
            session_id="daemon-session",
            model="daemon-model",
            timeout=37,
        )
        return await api_server._execute_task_once(request, "request-1", "turn-1")

    response = asyncio.run(scenario())
    assert response.status == api_server.TaskStatus.completed
    assert captured["session_id"] == "daemon-session"
    assert captured["model"] == "daemon-model"
    assert captured["timeout"] == 37


def test_execute_uses_direct_litellm_backend(monkeypatch):
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return api_server.TaskStatus.completed, "done", None, 1, None

    async def scenario():
        monkeypatch.setattr(api_server, "EXECUTION_BACKEND", "litellm")
        monkeypatch.setattr(api_server, "_run_via_litellm", fake_run)
        request = api_server.TaskRequest(
            task_id="task-1",
            description="Do the task",
            turn_id="turn-1",
            model="starter-model",
            timeout=37,
        )
        return await api_server._execute_task_once(
            request, "request-1", "turn-1"
        )

    response = asyncio.run(scenario())
    assert response.status == api_server.TaskStatus.completed
    assert captured["model"] == "starter-model"
    assert captured["timeout"] == 37


def test_context_session_remains_backward_compatible(monkeypatch):
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return api_server.TaskStatus.completed, "done", None, 0, "run-1"

    async def scenario():
        monkeypatch.setattr(api_server, "HERMES_GATEWAY_URL", "http://hermes")
        monkeypatch.setattr(api_server, "_run_via_api", fake_run)
        request = api_server.TaskRequest(
            task_id="task-1",
            description="Do the task",
            context={"session_id": "legacy-session"},
        )
        return await api_server._execute_task_once(request, "request-1", "turn-1")

    asyncio.run(scenario())
    assert captured["session_id"] == "legacy-session"


def test_failed_trace_batch_spools_and_retries(monkeypatch, tmp_path):
    class TraceClient:
        def __init__(self, status_code):
            self.status_code = status_code
            self.calls = []

        async def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append(json)
            return FakeResponse(self.status_code, text="failed")

    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", "http://daemon")
    monkeypatch.setattr(api_server, "BMAS_NODE_KEY", "node-key")
    monkeypatch.setattr(api_server, "TRACE_FLUSH_RETRIES", 1)
    monkeypatch.setattr(api_server, "TRACE_SPOOL_DIR", tmp_path)

    trace = {"task_id": "task-1", "turn_id": "turn-1", "seq": 1}
    failed_client = TraceClient(503)
    failed = api_server.TraceEmitter(failed_client, "task-1", "turn-1")
    asyncio.run(failed.emit(trace))
    asyncio.run(failed.flush_all())

    assert failed.buffer == []
    assert len(list(tmp_path.glob("*.json"))) == 1

    accepted_client = TraceClient(200)
    accepted = api_server.TraceEmitter(accepted_client, "task-2", "turn-2")
    asyncio.run(accepted.emit({"task_id": "task-2", "turn_id": "turn-2", "seq": 1}))
    asyncio.run(accepted.flush_all())

    assert len(accepted_client.calls) == 2
    assert accepted_client.calls[0] == [trace]
    assert not list(tmp_path.glob("*.json"))


def test_terminal_state_precedes_telemetry_failure(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    original_emit = api_server.TraceEmitter.emit

    async def fail_final_trace(self, trace):
        if trace.get("type") == "final":
            raise RuntimeError("telemetry failed")
        await original_emit(self, trace)

    monkeypatch.setattr(api_server.TraceEmitter, "emit", fail_final_trace)
    client = FakeRunsClient(FakeStream([
        "event: run.completed",
        'data: {"output":"done","usage":{"input_tokens":2}}',
        "",
    ]))

    status, output, _, _, _ = run_api(client)
    assert status == api_server.TaskStatus.completed
    assert output == "done"


def test_final_trace_has_model_and_context_round(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    captured = []

    async def capture(self, trace):
        captured.append(trace)

    monkeypatch.setattr(api_server.TraceEmitter, "emit", capture)
    client = FakeRunsClient(FakeStream([
        "event: run.completed",
        'data: {"output":"done","usage":{"input_tokens":2,"output_tokens":1}}',
        "",
    ]))

    status, _, _, _, _ = run_api(client, context={"round": 7})
    assert status == api_server.TaskStatus.completed
    assert captured[0]["data"]["round"] == 7
    assert all(trace["run_id"] == "run-1" for trace in captured)
    assert all(trace["data"]["run_id"] == "run-1" for trace in captured)
    final_trace = next(trace for trace in captured if trace["type"] == "final")
    assert final_trace["data"]["usage"]["model"] == "selected-model"


def test_restart_refuses_duplicate_running_activation():
    key = "task-restart:activation-1"
    assert api_server._claim_activation(
        key, "fingerprint", "task-restart", "activation-1"
    )
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        raise AssertionError("The duplicate factory must not run")

    async def scenario():
        api_server._activation_lock = asyncio.Lock()
        try:
            await api_server._execute_idempotent(
                key,
                factory,
                fingerprint="fingerprint",
                task_id="task-restart",
                turn_id="activation-1",
            )
        except api_server.HTTPException as exc:
            return exc
        raise AssertionError("The running activation was not rejected")

    error = asyncio.run(scenario())
    assert error.status_code == 409
    assert calls == 0


def test_restart_reconciles_recorded_hermes_run(monkeypatch):
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", None)
    client = FakeRunsClient(
        FakeStream([]),
        poll=FakeResponse(200, {
            "status": "completed",
            "output": "recovered",
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }),
    )

    status, output, usage, _, run_id = run_api(
        client,
        resume_run_id="remote-run-9",
    )

    assert status == api_server.TaskStatus.completed
    assert output == "recovered"
    assert usage["model"] == "selected-model"
    assert run_id == "remote-run-9"
    assert not any(call["url"].endswith("/v1/runs") for call in client.posts)


def test_idempotent_retry_persists_reconciled_response():
    key = "task-resume:activation-1"
    assert api_server._claim_activation(
        key, "fingerprint", "task-resume", "activation-1"
    )
    assert api_server._persist_activation_state(
        key,
        "running",
        "fingerprint",
        run_id="remote-run-1",
    )
    fresh_calls = 0
    resume_calls = 0

    async def factory():
        nonlocal fresh_calls
        fresh_calls += 1
        raise AssertionError("A recorded run must not start again")

    async def resume_factory(record):
        nonlocal resume_calls
        resume_calls += 1
        assert record["run_id"] == "remote-run-1"
        return api_server.TaskResponse(
            task_id="task-resume",
            status=api_server.TaskStatus.completed,
            result="recovered",
            node_id="node-1",
            request_id="request-1",
            duration_ms=1,
            timestamp="2026-08-18T00:00:00+00:00",
            turn_id="activation-1",
            run_id="remote-run-1",
        )

    response = asyncio.run(api_server._execute_idempotent(
        key,
        factory,
        resume_factory=resume_factory,
        fingerprint="fingerprint",
        task_id="task-resume",
        turn_id="activation-1",
    ))
    record = api_server._load_activation_record(key, time.time())

    assert response.result == "recovered"
    assert fresh_calls == 0
    assert resume_calls == 1
    assert record["state"] == "completed"
    assert record["response"]["result"] == "recovered"


def test_cancelled_state_rejects_late_run_and_completion():
    key = "task-race:activation-1"
    assert api_server._claim_activation(
        key, "fingerprint", "task-race", "activation-1"
    )
    assert api_server._persist_activation_state(
        key,
        "cancelled",
        "fingerprint",
        error="cancelled",
    )
    assert not api_server._persist_activation_state(
        key,
        "running",
        "fingerprint",
        run_id="late-run",
    )
    late_response = api_server.TaskResponse(
        task_id="task-race",
        status=api_server.TaskStatus.completed,
        result="late result",
        node_id="node-1",
        request_id="request-1",
        duration_ms=1,
        timestamp="2026-08-18T00:00:00+00:00",
        turn_id="activation-1",
        run_id="late-run",
    )
    assert not api_server._persist_activation_result(
        key, late_response, "fingerprint"
    )
    record = api_server._load_activation_record(key, time.time())

    assert record["state"] == "cancelled"
    assert record.get("run_id") is None
    assert record.get("response") is None


def test_uncertain_activation_without_run_id_enters_quarantine(monkeypatch):
    key = "task-unknown:activation-1"
    monkeypatch.setattr(api_server, "ACTIVATION_UNCERTAIN_TTL_SECONDS", 1)
    assert api_server._claim_activation(
        key, "fingerprint", "task-unknown", "activation-1"
    )
    assert api_server._persist_activation_state(
        key,
        "uncertain",
        "fingerprint",
        error="agent stopped before run submission",
    )
    old = time.time() - 2
    os.utime(api_server._activation_cache_path(key), (old, old))

    record = api_server._load_activation_record(key, time.time())
    assert record["state"] == "quarantined"


def test_cancelled_activation_does_not_restart():
    key = "task-cancelled:activation-1"
    assert api_server._claim_activation(
        key, "fingerprint", "task-cancelled", "activation-1"
    )
    api_server._persist_activation_state(
        key,
        "cancelled",
        "fingerprint",
        error="cancelled",
    )
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        raise AssertionError("A cancelled activation must not restart")

    response = asyncio.run(api_server._execute_idempotent(
        key,
        factory,
        fingerprint="fingerprint",
        task_id="task-cancelled",
        turn_id="activation-1",
    ))
    assert response.status == api_server.TaskStatus.failed
    assert "cancelled" in response.result
    assert calls == 0


def test_corrupt_activation_record_fails_closed():
    key = "task-corrupt:activation-1"
    path = api_server._activation_cache_path(key)
    path.parent.mkdir(parents=True)
    path.write_text("not-json")

    async def factory():
        raise AssertionError("A corrupt record must not start a duplicate")

    async def scenario():
        api_server._activation_lock = asyncio.Lock()
        try:
            await api_server._execute_idempotent(
                key, factory, task_id="task-corrupt", turn_id="activation-1"
            )
        except api_server.HTTPException as exc:
            return exc
        raise AssertionError("The corrupt activation record was ignored")

    error = asyncio.run(scenario())
    assert error.status_code == 409
    assert api_server._load_activation_record(key, api_server.time.time())["state"] == "uncertain"


def test_corrupt_trace_spool_does_not_block_valid_batch(monkeypatch, tmp_path):
    class TraceClient:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append(json)
            return FakeResponse(200)

    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", "http://daemon")
    monkeypatch.setattr(api_server, "BMAS_NODE_KEY", "node-key")
    monkeypatch.setattr(api_server, "TRACE_FLUSH_RETRIES", 1)
    monkeypatch.setattr(api_server, "TRACE_SPOOL_DIR", tmp_path)
    (tmp_path / "001.json").write_text("not-json")
    valid = [{"type": "final", "seq": 1}]
    api_server._atomic_write_json(tmp_path / "002.json", {
        "task_id": "task-1",
        "turn_id": "turn-1",
        "traces": valid,
    })
    client = TraceClient()
    emitter = api_server.TraceEmitter(client, "task-2", "turn-2")

    asyncio.run(emitter._drain_spool())
    assert client.calls == [valid]
    assert (tmp_path / "001.bad").exists()
    assert not (tmp_path / "002.json").exists()


def test_trace_spool_bound_prefers_terminal_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(api_server, "TRACE_SPOOL_DIR", tmp_path)
    monkeypatch.setattr(api_server, "TRACE_SPOOL_MAX_FILES", 1)
    monkeypatch.setattr(api_server, "TRACE_SPOOL_MAX_BYTES", 1024 * 1024)
    emitter = api_server.TraceEmitter(SimpleNamespace(), "task-1", "turn-1")

    assert emitter._spool("task-1", "turn-1", [{"type": "reasoning"}])
    assert emitter._spool("task-1", "turn-1", [{"type": "final"}])
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["traces"][0]["type"] == "final"
    assert not emitter._spool(
        "task-1", "turn-1", [{"type": "reasoning", "data": {"text": "later"}}]
    )
    remaining = next(tmp_path.glob("*.json"))
    assert json.loads(remaining.read_text())["traces"][0]["type"] == "final"


def test_terminal_activation_cache_has_file_bound(monkeypatch):
    monkeypatch.setattr(api_server, "ACTIVATION_CACHE_MAX_ENTRIES", 1)
    monkeypatch.setattr(api_server, "ACTIVATION_CACHE_MAX_BYTES", 1024 * 1024)

    def response(task_id):
        return api_server.TaskResponse(
            task_id=task_id,
            status=api_server.TaskStatus.completed,
            result="done",
            node_id="node-1",
            request_id="request-1",
            duration_ms=1,
            timestamp="2026-08-18T00:00:00+00:00",
        )

    first_key = "task-1:a"
    api_server._persist_activation_result(first_key, response("task-1"))
    old = time.time() - 2
    os.utime(api_server._activation_cache_path(first_key), (old, old))
    monkeypatch.setattr(api_server, "ACTIVATION_CACHE_TTL_SECONDS", 1)
    api_server._persist_activation_result("task-2:b", response("task-2"))
    files = list(api_server.ACTIVATION_CACHE_DIR.glob("*.json"))
    assert len(files) == 1
    assert api_server._load_activation_result("task-2:b", api_server.time.time())


def test_fresh_terminal_activation_uses_admission_backpressure(monkeypatch):
    monkeypatch.setattr(api_server, "ACTIVATION_CACHE_MAX_ENTRIES", 1)
    monkeypatch.setattr(api_server, "ACTIVATION_CACHE_MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(api_server, "ACTIVATION_CACHE_TTL_SECONDS", 3600)

    def response(task_id):
        return api_server.TaskResponse(
            task_id=task_id,
            status=api_server.TaskStatus.completed,
            result="done",
            node_id="node-1",
            request_id="request-1",
            duration_ms=1,
            timestamp="2026-08-18T00:00:00+00:00",
        )

    assert api_server._persist_activation_result(
        "task-protected:a", response("task-protected")
    )
    with pytest.raises(RuntimeError, match="capacity"):
        api_server._persist_activation_result(
            "task-rejected:b", response("task-rejected")
        )
    assert api_server._load_activation_result(
        "task-protected:a", time.time()
    ).result == "done"
    assert not api_server._activation_cache_path("task-rejected:b").exists()


def test_large_result_returns_full_but_cache_stays_bounded(monkeypatch):
    monkeypatch.setattr(api_server, "ACTIVATION_CACHE_MAX_BYTES", 700)
    output = "x" * 100_000

    async def factory():
        return api_server.TaskResponse(
            task_id="task-large",
            status=api_server.TaskStatus.completed,
            result=output,
            node_id="node-1",
            request_id="request-1",
            duration_ms=1,
            timestamp="2026-08-18T00:00:00+00:00",
            turn_id="activation-1",
        )

    async def scenario():
        api_server._activation_lock = asyncio.Lock()
        return await api_server._execute_idempotent(
            "task-large:activation-1",
            factory,
            fingerprint="fingerprint",
            task_id="task-large",
            turn_id="activation-1",
        )

    response = asyncio.run(scenario())
    assert response.result == output
    assert "task-large:activation-1" not in api_server._activation_results
    cache_file = next(api_server.ACTIVATION_CACHE_DIR.glob("*.json"))
    assert cache_file.stat().st_size <= 700
    assert json.loads(cache_file.read_text())["response"] is None


def test_telemetry_event_and_log_fields_are_bounded(monkeypatch):
    monkeypatch.setattr(api_server, "TRACE_EVENT_MAX_BYTES", 1024)
    monkeypatch.setattr(api_server, "LOG_RECORD_MAX_BYTES", 1024)
    trace = api_server._bound_trace({
        "type": "reasoning",
        "task_id": "t" * 100_000,
        "turn_id": "u" * 100_000,
        "data": {"text": "x" * 100_000},
    })
    log = api_server._bound_log_record({
        "message": "m" * 100_000,
        "agent_role": "r" * 100_000,
        "fields": {"text": "x" * 100_000},
    })
    assert trace["data"]["truncated"] is True
    assert log["fields"]["truncated"] is True
    assert api_server._json_size(trace) <= 1024
    assert api_server._json_size(log) <= 1024


def test_text_model_prompt_excludes_binary_attachment_metadata():
    context = api_server._context_for_prompt({
        "board": {"entries": []},
        "attachments": [{"file_id": "file-1", "text_preview": "secret"}],
    })

    assert context == {"board": {"entries": []}}


def test_artifact_sync_sends_the_agent_role(monkeypatch, tmp_path):
    calls = []

    class SyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return FakeResponse(data={"version": 1})

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / "report.txt").write_text("verified")
    monkeypatch.setattr(api_server.httpx, "AsyncClient", SyncClient)
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", "http://daemon")

    asyncio.run(api_server._sync_artifacts(
        task_id="task-1",
        turn_id="turn-1",
        author="planner",
        outputs_dir=output_dir,
        request_id="request-1",
    ))

    assert calls[0]["data"]["author"] == "planner"


def test_cli_deadline_includes_attachment_staging(monkeypatch):
    subprocess_called = False

    async def slow_stage(**kwargs):
        await asyncio.sleep(1)

    async def unexpected_subprocess(*args, **kwargs):
        nonlocal subprocess_called
        subprocess_called = True
        raise AssertionError("Subprocess started after the deadline")

    monkeypatch.setattr(api_server, "_stage_attachments", slow_stage)
    monkeypatch.setattr(api_server.asyncio, "create_subprocess_exec", unexpected_subprocess)
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", "http://daemon")
    monkeypatch.setattr(api_server, "BMAS_NODE_KEY", "node-key")

    result = asyncio.run(api_server._run_hermes(
        description="Do the task",
        role_prompt=None,
        context={"attachments": [{"file_id": "file-1"}]},
        timeout=0.01,
        request_id="request-1",
        task_id="task-1",
        turn_id="turn-1",
    ))
    assert result[0] == api_server.TaskStatus.timeout
    assert subprocess_called is False


def test_cli_spools_bounded_trace_without_truncating_result(monkeypatch, tmp_path):
    output = "x" * 100_000

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return output.encode(), b""

    async def fake_subprocess(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(api_server.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(api_server, "DAEMON_INGEST_URL", "http://daemon")
    monkeypatch.setattr(api_server, "BMAS_NODE_KEY", "node-key")
    monkeypatch.setattr(api_server, "TRACE_SPOOL_DIR", tmp_path)
    monkeypatch.setattr(api_server, "TRACE_EVENT_MAX_BYTES", 1024)

    result = asyncio.run(api_server._run_hermes(
        description="Do the task",
        role_prompt=None,
        context={"round": 4},
        timeout=30,
        request_id="request-1",
        task_id="task-1",
        turn_id="turn-1",
    ))
    assert result[1] == output
    spool = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert spool["traces"][0]["data"]["round"] == 4
    final_trace = next(trace for trace in spool["traces"] if trace["type"] == "final")
    assert final_trace["data"]["truncated"] is True
