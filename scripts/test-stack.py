#!/usr/bin/env python3
"""The complete local test-stack controller.

The controller starts the complete local stack in order: a temporary
Redis with a test-only password, the daemon with a temporary SQLite
database and artifact directory and the benchmark scheduler under its
lifecycle, the deterministic fake nested provider, the real agent
service pointed at that provider, and Mission Control with test
authentication. It allocates every port from one reserved test range,
generates distinct test-only API, node, and execution credentials,
writes the selected ports and paths to one generated environment
file, waits for one real readiness endpoint per service, captures one
structured log per component, and on teardown sends cancellation to
the daemon, stops every process in reverse order, verifies that no
process or port survives, and deletes the temporary data. A teardown
that leaves a process, a bound port, or a temporary secret fails.

Commands:

    python3 scripts/test-stack.py start --env-file <path> [--keep-on-failure]
    python3 scripts/test-stack.py stop  --env-file <path>
    python3 scripts/test-stack.py status --env-file <path>
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESERVED_PORT_RANGE = (43000, 43999)
SERVICE_ORDER = (
    "redis",
    "fake_provider",
    "daemon",
    "agent",
    "mission_control",
)
READINESS_TIMEOUT_SECONDS = 180.0


class StackError(RuntimeError):
    """The stack failed to start, ready, or tear down cleanly."""


# ── Ports and credentials ────────────────────────────────────────────


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def allocate_ports(names: tuple[str, ...]) -> dict[str, int]:
    """Allocate one free port per service from the reserved range."""
    chosen: dict[str, int] = {}
    candidate = RESERVED_PORT_RANGE[0] + (os.getpid() * 7) % 600
    for name in names:
        while True:
            if candidate > RESERVED_PORT_RANGE[1]:
                candidate = RESERVED_PORT_RANGE[0]
            if candidate not in chosen.values() and _port_free(candidate):
                chosen[name] = candidate
                candidate += 1
                break
            candidate += 1
    return chosen


def test_credentials() -> dict[str, str]:
    """Generate distinct test-only credentials; never the dev ones."""
    return {
        "redis_password": "test-redis-" + secrets.token_hex(12),
        "api_key": "test-api-" + secrets.token_hex(12),
        "node_key": "test-node-" + secrets.token_hex(12),
        "execute_key": "test-execute-" + secrets.token_hex(12),
        "provider_key": "test-provider-" + secrets.token_hex(12),
    }


# ── Configuration generation ─────────────────────────────────────────


def write_daemon_config(root: Path, ports: dict[str, int]) -> Path:
    """Derive one test configuration from the published example."""
    import yaml

    example = yaml.safe_load((ROOT / "bmas.example.yaml").read_text())
    example["control_plane"]["host"] = "127.0.0.1"
    example["control_plane"]["ports"].update({
        "redis": ports["redis"],
        "litellm": ports["fake_provider"],
        "triage": ports["fake_provider"],
        "daemon": ports["daemon"],
        "dashboard": ports["mission_control"],
    })
    example["nodes"] = [{
        "name": "test-agent",
        "host": "127.0.0.1",
        "port": ports["agent"],
        "role": "starter",
        "color": "#5eead4",
    }]
    example.setdefault("triage", {})["enabled"] = False
    for model in (example.get("models") or {}).values():
        model["provider"] = "openai"
        model["model"] = "fake-model"
        model["api_key_env"] = "FAKE_PROVIDER_KEY"
    storage = example.setdefault("storage", {})
    storage["user_media_dir"] = str(root / "uploads")
    storage["artifacts_dir"] = str(root / "artifacts")
    (root / "uploads").mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    path = root / "bmas.test.yaml"
    path.write_text(yaml.safe_dump(example, sort_keys=False))
    return path


# ── Process control ──────────────────────────────────────────────────


def _spawn(
    name: str, argv: list[str], *, cwd: Path, env: dict[str, str],
    log_path: Path,
) -> subprocess.Popen:
    log_handle = open(log_path, "ab")  # noqa: SIM115
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdout=log_handle, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()
    return process


def _wait_http(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                if 200 <= response.status < 300:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def _wait_ready_json(url: str, *, timeout: float) -> bool:
    """Wait until one readiness document reports every check ready."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                last = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                last = json.loads(error.read().decode("utf-8"))
            except ValueError:
                last = {}
        except (urllib.error.URLError, OSError, ValueError):
            last = {}
        checks = last.get("checks") or []
        if checks and all(check.get("ready") for check in checks):
            return True
        time.sleep(1.0)
    sys.stderr.write("readiness never completed: " + json.dumps(last) + "\n")
    return False


def _wait_redis(port: int, password: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(f"AUTH {password}\r\nPING\r\n".encode())
                if b"+PONG" in sock.recv(256):
                    return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_pid(pid: int) -> bool:
    """Stop one process group; return True when it is gone."""
    for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            os.kill(pid, sig)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _alive(pid):
                return True
            time.sleep(0.2)
    return not _alive(pid)


# ── Start ────────────────────────────────────────────────────────────


def start(env_file: Path, *, keep_on_failure: bool) -> dict:
    root = Path(tempfile.mkdtemp(prefix="bmas-test-stack-"))
    logs = root / "logs"
    logs.mkdir()
    ports = allocate_ports(SERVICE_ORDER)
    credentials = test_credentials()
    config_path = write_daemon_config(root, ports)
    secrets_path = root / "credentials.json"
    secrets_path.write_text(json.dumps(credentials))
    state: dict = {
        "stack_id": root.name,
        "root": str(root),
        "ports": ports,
        "config_path": str(config_path),
        "database_path": str(root / "daemon.db"),
        "log_dir": str(logs),
        "credentials_path": str(secrets_path),
        "api_key": credentials["api_key"],
        "urls": {
            "daemon": f"http://127.0.0.1:{ports['daemon']}",
            "agent": f"http://127.0.0.1:{ports['agent']}",
            "fake_provider": f"http://127.0.0.1:{ports['fake_provider']}",
            "mission_control": f"http://127.0.0.1:{ports['mission_control']}",
            "redis": f"redis://127.0.0.1:{ports['redis']}/0",
        },
        "processes": {},
        "readiness": {},
        "started_at": time.time(),
    }
    python = sys.executable
    base_env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("BMAS_", "REDIS_", "LITELLM_"))
    }
    base_env["PATH"] = os.environ.get("PATH", "")
    try:
        redis_binary = shutil.which("redis-server")
        if redis_binary is None:
            raise StackError("redis-server is required and was not found")
        process = _spawn("redis", [
            redis_binary, "--port", str(ports["redis"]), "--requirepass",
            credentials["redis_password"], "--save", "", "--appendonly", "no",
            "--dir", str(root), "--bind", "127.0.0.1",
        ], cwd=root, env=base_env, log_path=logs / "redis.log")
        state["processes"]["redis"] = process.pid
        if not _wait_redis(ports["redis"], credentials["redis_password"],
                           timeout=20):
            raise StackError("redis never answered PING with the test password")
        state["readiness"]["redis"] = True

        process = _spawn("fake_provider", [
            python, str(ROOT / "scripts/fake-provider.py"), "--port",
            str(ports["fake_provider"]), "--api-key", credentials["provider_key"],
        ], cwd=root, env=base_env, log_path=logs / "fake_provider.log")
        state["processes"]["fake_provider"] = process.pid
        if not _wait_http(f"{state['urls']['fake_provider']}/health", timeout=20):
            raise StackError("the fake provider never became ready")
        state["readiness"]["fake_provider"] = True

        daemon_env = {
            **base_env,
            "BMAS_CONFIG": str(config_path),
            "BMAS_DB_PATH": state["database_path"],
            "REDIS_PASSWORD": credentials["redis_password"],
            "LITELLM_MASTER_KEY": credentials["provider_key"],
            # The model-backed judge reads the gateway URL from the
            # environment, so calibration reaches the fake provider.
            "BMAS_LITELLM_URL": f"{state['urls']['fake_provider']}/v1",
            "FAKE_PROVIDER_KEY": credentials["provider_key"],
            "BMAS_NODE_KEY": credentials["node_key"],
            "BMAS_API_KEY": credentials["api_key"],
            "BMAS_EXECUTE_KEY": credentials["execute_key"],
            "PYTHONUNBUFFERED": "1",
        }
        process = _spawn("daemon", [
            python, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
            "--port", str(ports["daemon"]), "--log-level", "info",
        ], cwd=ROOT / "daemon" / "src", env=daemon_env, log_path=logs / "daemon.log")
        state["processes"]["daemon"] = process.pid
        if not _wait_http(f"{state['urls']['daemon']}/health",
                          timeout=READINESS_TIMEOUT_SECONDS):
            raise StackError("the daemon never became healthy")
        state["readiness"]["daemon_health"] = True

        agent_env = {
            **base_env,
            "LITELLM_URL": f"{state['urls']['fake_provider']}/v1",
            "LITELLM_API_KEY": credentials["provider_key"],
            "LITELLM_MODEL": "fake-model",
            "BMAS_EXECUTION_BACKEND": "litellm",
            "HERMES_GATEWAY_URL": state["urls"]["fake_provider"],
            "HERMES_GATEWAY_KEY": credentials["provider_key"],
            "DAEMON_INGEST_URL": state["urls"]["daemon"],
            "BMAS_NODE_KEY": credentials["node_key"],
            "BMAS_EXECUTE_KEY": credentials["execute_key"],
            "NODE_ID": "test-agent",
            "BMAS_OUTPUTS_ROOT": str(root / "agent-outputs"),
            "TRACE_SPOOL_DIR": str(root / "agent-spool"),
            "PYTHONUNBUFFERED": "1",
        }
        process = _spawn("agent", [
            python, "-m", "uvicorn", "api_server:app", "--host", "127.0.0.1",
            "--port", str(ports["agent"]), "--log-level", "info",
        ], cwd=ROOT / "agent", env=agent_env, log_path=logs / "agent.log")
        state["processes"]["agent"] = process.pid
        if not _wait_http(f"{state['urls']['agent']}/health", timeout=120):
            raise StackError("the agent service never became healthy")
        state["readiness"]["agent"] = True
        # Daemon readiness is one real check over Redis, SQLite, the
        # model gateway, and every execution agent; it never reads a
        # mocked route.
        if not _wait_ready_json(f"{state['urls']['daemon']}/readiness",
                                timeout=90):
            raise StackError("the daemon never reported every check ready")
        state["readiness"]["daemon"] = True

        npm = shutil.which("npm")
        if npm is None:
            raise StackError("npm is required for Mission Control")
        control_env = {
            **base_env,
            "BMAS_CONFIG": str(config_path),
            "REDIS_PASSWORD": credentials["redis_password"],
            "BMAS_API_KEY": credentials["api_key"],
            "BMAS_EXECUTE_KEY": credentials["execute_key"],
            "BMAS_DAEMON_URL": state["urls"]["daemon"],
            "PORT": str(ports["mission_control"]),
        }
        process = _spawn("mission_control", [
            npm, "run", "dev", "--", "--hostname", "127.0.0.1", "--port",
            str(ports["mission_control"]),
        ], cwd=ROOT / "mission-control", env=control_env,
            log_path=logs / "mission_control.log")
        state["processes"]["mission_control"] = process.pid
        if not _wait_http(f"{state['urls']['mission_control']}/api/readiness",
                          timeout=READINESS_TIMEOUT_SECONDS):
            raise StackError("Mission Control never proxied daemon readiness")
        state["readiness"]["mission_control"] = True
    except Exception as error:
        state["error"] = str(error)
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(json.dumps(state, indent=2, sort_keys=True))
        if not keep_on_failure:
            stop(env_file, strict=False)
        raise
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(json.dumps(state, indent=2, sort_keys=True))
    return state


# ── Stop ─────────────────────────────────────────────────────────────


def _cancel_active_work(state: dict) -> None:
    """Send cancellation to the daemon before any process stops."""
    url = state["urls"]["daemon"] + "/benchmarks/runs?limit=50"
    headers = {"Authorization": f"Bearer {state['api_key']}"}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=5,
        ) as response:
            runs = json.loads(response.read().decode("utf-8")).get("runs") or []
    except (urllib.error.URLError, OSError, ValueError):
        return
    for run in runs:
        if str(run.get("status")) not in ("queued", "running", "paused"):
            continue
        cancel = urllib.request.Request(
            f"{state['urls']['daemon']}/benchmarks/runs/{run['id']}/cancel",
            headers=headers, method="POST",
        )
        with contextlib.suppress(urllib.error.URLError, OSError):
            urllib.request.urlopen(cancel, timeout=5).close()


def stop(env_file: Path, *, strict: bool = True) -> dict:
    state = json.loads(env_file.read_text())
    _cancel_active_work(state)
    survivors: list[str] = []
    for name in reversed(SERVICE_ORDER):
        pid = state.get("processes", {}).get(name)
        if pid is None:
            continue
        if not _stop_pid(int(pid)):
            survivors.append(name)
    bound = [
        name for name, port in state["ports"].items()
        if name in state.get("processes", {}) and not _port_free(int(port))
    ]
    # The development server regenerates its route types on every
    # start; a partial file left by a stopped server would corrupt a
    # later type check, so the generated development types go away
    # with the stack.
    if "mission_control" in state.get("processes", {}):
        shutil.rmtree(ROOT / "mission-control" / ".next" / "dev",
                      ignore_errors=True)
    root = Path(state["root"])
    # Keep every component log and the database beside the environment
    # file as artifacts before the temporary root disappears.
    artifacts = env_file.parent / "artifacts"
    if root.exists():
        artifacts.mkdir(parents=True, exist_ok=True)
        for log_path in sorted((root / "logs").glob("*.log")):
            shutil.copy2(log_path, artifacts / log_path.name)
        database = Path(state["database_path"])
        if database.is_file():
            shutil.copy2(database, artifacts / database.name)
        state["artifacts_dir"] = str(artifacts)
    leftover_secrets = []
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            leftover_secrets = [
                str(path) for path in root.rglob("credentials.json")
            ]
    report = {
        "stopped": True,
        "survivors": survivors,
        "bound_ports": bound,
        "leftover_secrets": leftover_secrets,
    }
    state["teardown"] = report
    env_file.write_text(json.dumps(state, indent=2, sort_keys=True))
    if strict and (survivors or bound or leftover_secrets):
        raise StackError(f"teardown left state behind: {report}")
    return report


def status(env_file: Path) -> dict:
    state = json.loads(env_file.read_text())
    return {
        name: _alive(int(pid))
        for name, pid in state.get("processes", {}).items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "stop", "status"])
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--keep-on-failure", action="store_true")
    args = parser.parse_args()
    env_file = Path(args.env_file)
    try:
        if args.command == "start":
            state = start(env_file, keep_on_failure=args.keep_on_failure)
            print(json.dumps({"ports": state["ports"], "urls": state["urls"],
                              "env_file": str(env_file)}, sort_keys=True))
        elif args.command == "stop":
            print(json.dumps(stop(env_file), sort_keys=True))
        else:
            print(json.dumps(status(env_file), sort_keys=True))
    except StackError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
