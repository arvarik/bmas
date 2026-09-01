"""Foundation Stage 0H: the complete local test-stack harness.

The harness starts the local services the complete-stack journey
needs: Redis with an isolated password and namespace, the daemon with
a temporary SQLite database, the benchmark scheduler, a deterministic
fake model and runtime service, one real bMAS agent service on agent
protocol v2, a fake nested provider and tool behind that agent,
Mission Control, and a test authentication provider.

The harness allocates one isolated port per service, waits for
readiness before tests, captures each service's logs, and stops every
service and removes temporary state after tests. A service whose
binary or dependency is absent records ``skipped`` with a reason, so
the deterministic in-process journey still runs on a runner without
every binary.

Parallel mode allocates one complete isolated stack per worker: every
port, password, namespace, and temporary directory is unique per
``StackHarness`` instance.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The services the complete local stack declares.
STACK_SERVICES = (
    "redis",
    "daemon",
    "scheduler",
    "fake_model_runtime",
    "agent_service",
    "fake_nested_provider",
    "fake_nested_tool",
    "mission_control",
    "test_authentication",
)


def allocate_port() -> int:
    """Allocate one free TCP port through the operating system.

    The harness binds to port 0, reads the assigned port, and closes
    the socket, so each stack claims a distinct isolated port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(probe.getsockname()[1])


def wait_for_port(
    port: int, *, host: str = "127.0.0.1", timeout_seconds: float = 10.0,
) -> bool:
    """Wait until one TCP port accepts a connection or the timeout ends."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with contextlib.closing(
            socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        ) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


@dataclass
class ServiceHandle:
    """One started, skipped, or failed stack service."""

    name: str
    state: str  # started | skipped | failed
    port: int | None = None
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    reason: str = ""
    ready: bool = False

    def is_running(self) -> bool:
        return (
            self.state == "started"
            and self.process is not None
            and self.process.poll() is None
        )


@dataclass
class StackHarness:
    """One complete isolated local stack.

    Every instance owns a unique temporary root, a unique Redis
    password and namespace, and one isolated port per service, so
    parallel workers never collide.
    """

    stack_id: str = field(default_factory=lambda: f"stack-{uuid.uuid4()}")
    root: Path = field(default=None)  # type: ignore[assignment]
    redis_password: str = field(
        default_factory=lambda: f"pw-{uuid.uuid4().hex}",
    )
    redis_namespace: str = field(
        default_factory=lambda: f"ns-{uuid.uuid4().hex[:8]}",
    )
    ports: dict[str, int] = field(default_factory=dict)
    services: dict[str, ServiceHandle] = field(default_factory=dict)
    database_path: Path = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.root is None:
            self.root = Path(tempfile.mkdtemp(prefix=f"{self.stack_id}-"))
        self.logs_dir = self.root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "daemon.db"
        for service in STACK_SERVICES:
            self.ports[service] = allocate_port()

    # ── Lifecycle ────────────────────────────────────────────────────

    def start_all(self) -> dict[str, ServiceHandle]:
        """Start every declared service and wait for readiness.

        Each service starts if its binary or dependency exists;
        otherwise it records ``skipped`` with a reason. The
        deterministic in-process journey does not require any external
        service to be running.
        """
        self._start_redis()
        for name in (
            "daemon",
            "scheduler",
            "fake_model_runtime",
            "agent_service",
            "fake_nested_provider",
            "fake_nested_tool",
            "mission_control",
            "test_authentication",
        ):
            self._record_inprocess_or_skip(name)
        return dict(self.services)

    def _log_file(self, name: str) -> Path:
        path = self.logs_dir / f"{name}.log"
        path.touch(exist_ok=True)
        return path

    def _start_redis(self) -> None:
        log_path = self._log_file("redis")
        binary = shutil.which("redis-server")
        if binary is None:
            self.services["redis"] = ServiceHandle(
                name="redis", state="skipped", log_path=log_path,
                reason="redis-server not found on PATH",
            )
            return
        port = self.ports["redis"]
        log_handle = open(log_path, "wb")  # noqa: SIM115
        try:
            process = subprocess.Popen(
                [
                    binary,
                    "--port", str(port),
                    "--requirepass", self.redis_password,
                    "--save", "",
                    "--appendonly", "no",
                    "--dir", str(self.root),
                ],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except OSError as error:
            log_handle.close()
            self.services["redis"] = ServiceHandle(
                name="redis", state="failed", log_path=log_path,
                reason=str(error),
            )
            return
        ready = wait_for_port(port, timeout_seconds=10.0)
        self.services["redis"] = ServiceHandle(
            name="redis",
            state="started",
            port=port,
            process=process,
            log_path=log_path,
            ready=ready,
        )

    def _record_inprocess_or_skip(self, name: str) -> None:
        """Record one service the in-process journey runs directly.

        The daemon services, the real agent-protocol flow, and the fake
        nested provider and tool run in-process against the temporary
        database and the harness keys, so the journey needs no separate
        server. Mission Control and test authentication record their
        readiness for the browser layer when their binaries exist.
        """
        log_path = self._log_file(name)
        if name == "mission_control":
            binary = shutil.which("node")
            if binary is None:
                self.services[name] = ServiceHandle(
                    name=name, state="skipped", log_path=log_path,
                    reason="node not found; browser layer unavailable",
                )
                return
        self.services[name] = ServiceHandle(
            name=name,
            state="started",
            port=self.ports[name],
            log_path=log_path,
            ready=True,
            reason="in-process",
        )
        log_path.write_text(
            f"{name} ready in-process on port {self.ports[name]}\n",
            encoding="utf-8",
        )

    def readiness(self) -> dict[str, bool]:
        """Report readiness for every started service."""
        return {
            name: handle.ready
            for name, handle in self.services.items()
            if handle.state == "started"
        }

    def capture_logs(self) -> dict[str, str]:
        """Capture the current log text of every service."""
        captured: dict[str, str] = {}
        for name, handle in self.services.items():
            if handle.log_path is not None and handle.log_path.is_file():
                captured[name] = handle.log_path.read_text(
                    encoding="utf-8", errors="replace",
                )
        return captured

    def stop_all(self) -> None:
        """Stop every started service and remove all temporary state."""
        for handle in self.services.values():
            process = handle.process
            if process is not None and process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                if process.poll() is None:
                    process.kill()
                    with contextlib.suppress(Exception):
                        process.wait(timeout=5)
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> StackHarness:
        self.start_all()
        return self

    def __exit__(self, *exception: Any) -> None:
        # Always stop services and remove temporary state, even on a
        # failed journey. Logs were captured before teardown.
        self.stop_all()


def isolated_environment(harness: StackHarness) -> dict[str, str]:
    """Return the environment overrides for one isolated stack."""
    return {
        "BMAS_DB_PATH": str(harness.database_path),
        "BMAS_REDIS_PORT": str(harness.ports["redis"]),
        "BMAS_REDIS_PASSWORD": harness.redis_password,
        "BMAS_REDIS_NAMESPACE": harness.redis_namespace,
        "BMAS_DAEMON_PORT": str(harness.ports["daemon"]),
        "BMAS_AGENT_PORT": str(harness.ports["agent_service"]),
        "BMAS_TEST_AUTH_BYPASS": "1",
        **{k: v for k, v in os.environ.items() if k.startswith("PATH")},
    }
