"""Real isolation backends behind the scorer boundary contract.

The boundary contract in ``scorer_sandbox`` names what every untrusted
scorer boundary must do. This module supplies the two real boundaries
behind it.

The Wasmtime component runner executes one WebAssembly component
through the pinned Wasmtime runtime. The linker defines only the
granted ``bmas:scorer`` interfaces, so a component that imports a
WASI clock, random, filesystem, socket, or process interface fails
before instantiation. Fuel is the deterministic compute limit, the
store limiter bounds memory and table growth, NaN canonicalization is
on, relaxed SIMD is off, and epoch interruption is the last-resort
wall-time kill that classifies as ``sandbox_wall_time_kill``.

The Firecracker runner drives one pinned microVM through the jailer
and the Firecracker REST API over a Unix socket. It verifies the
kernel, root filesystem, and virtual machine monitor digests before
boot, configures no network device, mounts the evidence read-only,
bounds the vCPUs, memory, and CPU quota, and talks to the guest
scorer only through an authenticated vsock request channel. The
guest never sees a host path, a credential, a device, or a real
clock.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from benchmarks import scorer_sandbox
from benchmarks.provenance import canonical_json, content_checksum

try:  # The pinned Wasmtime runtime ships inside the wasmtime-py wheel.
    import wasmtime as _wasmtime
    from wasmtime import component as _component
except ImportError:  # pragma: no cover - exercised on hosts without Wasmtime
    _wasmtime = None  # type: ignore[assignment]
    _component = None  # type: ignore[assignment]

WASMTIME_ENGINE = "wasmtime"
FIRECRACKER_ENGINE = "firecracker"
SCORER_PACKAGE = "bmas:scorer"
SCORER_WORLD_VERSION = "0.1.0"
LOGICAL_TIME_INTERFACE = f"{SCORER_PACKAGE}/logical-time@{SCORER_WORLD_VERSION}"
RANDOM_INTERFACE = (
    f"{SCORER_PACKAGE}/deterministic-random@{SCORER_WORLD_VERSION}"
)
EXPORTED_FUNCTION = "score"
EPOCH_TICK_SECONDS = 0.01

# The WIT world every scorer component targets. Its digest pins the
# interface in every score record.
SCORER_WIT = f"""package {SCORER_PACKAGE}@{SCORER_WORLD_VERSION};

interface logical-time {{
  /// Seconds since the Unix epoch of the pinned logical time.
  now: func() -> u64;
}}

interface deterministic-random {{
  /// The next unsigned 64-bit word of the seeded deterministic stream.
  next-word: func() -> u64;
}}

world scorer {{
  import logical-time;
  import deterministic-random;
  export score: func(expected: string, actual: string) -> tuple<f64, bool>;
}}
"""

# The host interfaces a component may import, in addition to the
# marshalled input and result the host itself supplies.
HOST_INTERFACES = (LOGICAL_TIME_INTERFACE, RANDOM_INTERFACE)

_TRAP_CLASSES = {
    "OUT_OF_FUEL": "fuel_exhausted",
    "INTERRUPT": "sandbox_wall_time_kill",
}


class SandboxBackendError(RuntimeError):
    """The backend cannot run on this host or with this configuration."""


def wit_digest() -> str:
    """Digest the pinned WIT world text."""
    return hashlib.sha256(SCORER_WIT.encode("utf-8")).hexdigest()


def wasmtime_version() -> str | None:
    try:
        return metadata.version("wasmtime")
    except metadata.PackageNotFoundError:
        return None


def wasmtime_available() -> bool:
    return _wasmtime is not None and _component is not None


def wasmtime_runtime_digest() -> str:
    """Digest the pinned Wasmtime runtime and its configuration."""
    return content_checksum({
        "engine": WASMTIME_ENGINE,
        "engine_version": wasmtime_version() or "absent",
        "component_model": True,
        "nan_canonicalization": True,
        "relaxed_simd": "disabled",
        "fuel_accounting": "deterministic",
        "growth_policy": "deterministic_store_limiter",
        "wall_time_kill": "epoch_interruption",
    })


# ── Component artifacts ──────────────────────────────────────────────


@dataclass(frozen=True)
class ComponentArtifact:
    """One compiled component with its content digest."""

    wasm: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.wasm).hexdigest()

    @classmethod
    def from_wat(cls, text: str) -> ComponentArtifact:
        """Compile one component from its text format."""
        _require_wasmtime()
        return cls(bytes(_wasmtime.wat2wasm(text)))

    @classmethod
    def from_path(cls, path: str | Path) -> ComponentArtifact:
        source = Path(path)
        if source.suffix == ".wat":
            return cls.from_wat(source.read_text(encoding="utf-8"))
        return cls(source.read_bytes())


def _require_wasmtime() -> None:
    if not wasmtime_available():
        raise SandboxBackendError(
            "The Wasmtime runtime is absent; install the pinned wasmtime "
            "distribution from daemon/requirements.txt"
        )


def _engine() -> Any:
    config = _wasmtime.Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    config.wasm_component_model = True
    config.cranelift_nan_canonicalization = True
    config.wasm_relaxed_simd = False
    config.wasm_threads = False
    return _wasmtime.Engine(config)


def component_imports(artifact: ComponentArtifact) -> list[str]:
    """List every interface one component imports."""
    _require_wasmtime()
    engine = _engine()
    compiled = _component.Component(engine, artifact.wasm)
    listed = compiled.type.imports(engine)
    names: list[str] = []
    entries = listed.items() if hasattr(listed, "items") else listed
    for entry in entries:
        name = entry[0] if isinstance(entry, (tuple, list)) else entry
        names.append(str(name))
    return names


def component_manifest(artifact: ComponentArtifact) -> dict[str, Any]:
    """Describe one component the way the boundary validation reads it.

    The granted host interfaces map onto the boundary's granted
    interface names; every other import keeps its own name so the
    prohibited-prefix check sees it.
    """
    imports: list[str] = []
    for name in component_imports(artifact):
        if name == LOGICAL_TIME_INTERFACE:
            imports.append("bmas:logical-time")
        elif name == RANDOM_INTERFACE:
            imports.append("bmas:deterministic-random")
        else:
            imports.append(name)
    return {"component_digest": artifact.digest, "imports": imports}


# ── The Wasmtime component runner ────────────────────────────────────


class _EpochTicker:
    """Advance the engine epoch on a fixed wall-clock tick."""

    def __init__(self, engine: Any, tick_seconds: float) -> None:
        self._engine = engine
        self._tick = tick_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._tick):
            self._engine.increment_epoch()

    def __enter__(self) -> _EpochTicker:
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)


class WasmtimeComponentRunner:
    """Execute one scorer component inside the pinned Wasmtime runtime."""

    engine_name = WASMTIME_ENGINE

    def __init__(self, *, epoch_tick_seconds: float = EPOCH_TICK_SECONDS) -> None:
        _require_wasmtime()
        self._tick = float(epoch_tick_seconds)

    def execute(
        self,
        *,
        artifact: ComponentArtifact,
        policy: scorer_sandbox.WasiScorerPolicy,
        evidence_input: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one validated component and return the boundary outcome."""
        manifest = component_manifest(artifact)
        scorer_sandbox.validate_component(manifest, policy)
        host = scorer_sandbox.BoundaryHost(policy, evidence_input)
        engine = _engine()
        store = _wasmtime.Store(engine)
        store.set_fuel(int(policy.fuel_limit))
        store.set_limits(
            memory_size=int(policy.memory_limit_bytes),
            table_elements=int(policy.table_limit_entries),
            instances=1, tables=4, memories=1,
        )
        ticks = max(1, int(policy.wall_time_limit_seconds / self._tick))
        store.set_epoch_deadline(ticks)
        linker = _component.Linker(engine)
        self._link_host(linker, host)
        expected, actual = _marshal_input(evidence_input)
        terminal_class = "completed"
        error: str | None = None
        result: tuple[float, bool] | None = None
        try:
            compiled = _component.Component(engine, artifact.wasm)
            with _EpochTicker(engine, self._tick):
                instance = linker.instantiate(store, compiled)
                function = instance.get_func(store, EXPORTED_FUNCTION)
                if function is None:
                    raise SandboxBackendError(
                        f"The component exports no {EXPORTED_FUNCTION!r}"
                    )
                raw = function(store, expected, actual)
                with contextlib.suppress(Exception):
                    # A component without a post-return raises here.
                    function.post_return(store)
            result = _lift_result(raw)
        except _wasmtime.Trap as trap:
            terminal_class = _classify_trap(trap)
            error = str(trap).splitlines()[0][:500]
        except scorer_sandbox.CapabilityDenied as denied:
            terminal_class = "trap"
            error = str(denied)
        except _wasmtime.WasmtimeError as failure:
            terminal_class = _classify_error(str(failure))
            error = str(failure).splitlines()[0][:500]
        except SandboxBackendError as failure:
            terminal_class = "invalid_output"
            error = str(failure)
        fuel_used = int(policy.fuel_limit) - int(store.get_fuel())
        payload: bytes | None = None
        if terminal_class == "completed" and result is not None:
            value, passed = result
            payload = canonical_json(
                scorer_sandbox.canonical_result_value({
                    "status": "scored",
                    "dimensions": [
                        {"name": "score", "value": value, "category": None},
                    ],
                    "passed": bool(passed),
                    "explanation": "component score",
                }),
            ).encode("utf-8")
            if len(payload) > policy.output_limit_bytes:
                terminal_class = "output_limit"
                error = "The scorer breached the output_limit limit"
                payload = None
        return scorer_sandbox.outcome_from_payload(
            policy=policy,
            terminal_class=terminal_class,
            error=error,
            payload=payload,
            resources={
                "fuel_used": fuel_used,
                "memory_limit_bytes": int(policy.memory_limit_bytes),
                "memory_allocated_bytes": None,
                "table_entries": None,
                "output_bytes": len(payload or b""),
                "host_calls": host.fuel_used,
            },
            runtime_digest=wasmtime_runtime_digest(),
        )

    @staticmethod
    def _link_host(linker: Any, host: scorer_sandbox.BoundaryHost) -> None:
        # Each child linker instance locks its parent until it closes.
        root = linker.root()
        clock = root.add_instance(LOGICAL_TIME_INTERFACE)
        clock.add_func(
            "now",
            lambda _store: logical_time_seconds(host.logical_time()),
        )
        clock.close()
        random_source = root.add_instance(RANDOM_INTERFACE)
        random_source.add_func(
            "next-word",
            lambda _store: int.from_bytes(host.random_bytes(8), "big"),
        )
        random_source.close()
        root.close()


def logical_time_seconds(logical_time: str) -> int:
    """Convert the pinned logical time into whole seconds since the epoch."""
    from datetime import UTC, datetime

    text = str(logical_time)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _marshal_input(evidence_input: dict[str, Any]) -> tuple[str, str]:
    evidence = evidence_input.get("evidence") or {}
    expected = evidence.get("reference_answer")
    actual = evidence.get("final_output")
    return (
        "" if expected is None else str(expected),
        "" if actual is None else str(actual),
    )


def _lift_result(raw: Any) -> tuple[float, bool]:
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        return float(raw[0]), bool(raw[1])
    if isinstance(raw, (int, float)):
        return float(raw), float(raw) >= 1.0
    raise SandboxBackendError(
        "The component returned no (value, passed) result"
    )


def _classify_trap(trap: Any) -> str:
    code = getattr(trap, "trap_code", None)
    name = getattr(code, "name", None) or str(code or "")
    if str(name) in _TRAP_CLASSES:
        return _TRAP_CLASSES[str(name)]
    return _classify_error(str(trap))


def _classify_error(message: str) -> str:
    """Map one Wasmtime trap or error message onto a terminal class.

    A component trap arrives as one error whose message names the
    trap: exhausted fuel, an epoch interrupt, or an out-of-bounds
    memory or table access after the store limiter refused growth.
    """
    lowered = message.lower()
    if "fuel" in lowered:
        return "fuel_exhausted"
    if "interrupt" in lowered:
        return "sandbox_wall_time_kill"
    if "memory" in lowered and (
        "out of bounds" in lowered or "limit" in lowered
        or "exceed" in lowered
    ):
        return "memory_limit"
    if "table" in lowered and (
        "out of bounds" in lowered or "limit" in lowered
        or "exceed" in lowered
    ):
        return "table_limit"
    return "trap"


# ── The Firecracker API client ───────────────────────────────────────


class _UnixHttpConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self._socket_path)
        self.sock = connection


class FirecrackerApi:
    """The Firecracker REST API over its Unix socket."""

    def __init__(self, socket_path: str, *, timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        connection = _UnixHttpConnection(self.socket_path, self.timeout)
        try:
            payload = None if body is None else json.dumps(body).encode()
            headers = {"Accept": "application/json"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        decoded: Any = None
        if raw:
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except ValueError:
                decoded = raw.decode("utf-8", errors="replace")
        if response.status >= 400:
            raise SandboxBackendError(
                f"Firecracker rejected {method} {path}: "
                f"{response.status} {decoded}"
            )
        return response.status, decoded

    def wait_ready(self, *, deadline_seconds: float = 5.0) -> None:
        started = time.monotonic()
        while True:
            try:
                self.request("GET", "/")
                return
            except (OSError, SandboxBackendError):
                if time.monotonic() - started > deadline_seconds:
                    raise SandboxBackendError(
                        "The Firecracker API socket never answered"
                    ) from None
                time.sleep(0.05)

    def put_machine_config(self, *, vcpu_count: int, memory_mib: int) -> None:
        self.request("PUT", "/machine-config", {
            "vcpu_count": int(vcpu_count),
            "mem_size_mib": int(memory_mib),
            "smt": False,
        })

    def put_boot_source(self, *, kernel_image_path: str, boot_args: str) -> None:
        self.request("PUT", "/boot-source", {
            "kernel_image_path": kernel_image_path,
            "boot_args": boot_args,
        })

    def put_drive(
        self, drive_id: str, *, path_on_host: str, is_root_device: bool,
        is_read_only: bool,
    ) -> None:
        self.request("PUT", f"/drives/{drive_id}", {
            "drive_id": drive_id,
            "path_on_host": path_on_host,
            "is_root_device": bool(is_root_device),
            "is_read_only": bool(is_read_only),
        })

    def put_vsock(self, *, guest_cid: int, uds_path: str) -> None:
        self.request("PUT", "/vsock", {
            "guest_cid": int(guest_cid), "uds_path": uds_path,
        })

    def start_instance(self) -> None:
        self.request("PUT", "/actions", {"action_type": "InstanceStart"})

    def send_ctrl_alt_del(self) -> None:
        self.request("PUT", "/actions", {"action_type": "SendCtrlAltDel"})

    def describe(self) -> Any:
        return self.request("GET", "/")[1]


# ── The vsock request channel ────────────────────────────────────────


REQUEST_CHANNEL_PORT = 5200
_FRAME_HEADER = struct.Struct("!I")
MAX_FRAME_BYTES = 8 * 1024 * 1024


def _send_frame(connection: socket.socket, payload: bytes) -> None:
    connection.sendall(_FRAME_HEADER.pack(len(payload)) + payload)


def _read_exact(connection: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        piece = connection.recv(count - len(chunks))
        if not piece:
            raise SandboxBackendError("The request channel closed early")
        chunks.extend(piece)
    return bytes(chunks)


def _read_frame(connection: socket.socket, limit: int) -> bytes:
    (length,) = _FRAME_HEADER.unpack(_read_exact(connection, _FRAME_HEADER.size))
    if length > limit:
        raise SandboxBackendError(
            f"The guest response of {length} bytes exceeds the frame limit"
        )
    return _read_exact(connection, length)


def open_request_channel(
    uds_path: str, *, port: int = REQUEST_CHANNEL_PORT, timeout: float,
) -> socket.socket:
    """Open one host-initiated vsock connection through the Unix socket.

    Firecracker exposes guest vsock ports on the host through one Unix
    socket: the host writes ``CONNECT <port>`` and reads ``OK <port>``
    before the byte stream reaches the guest listener.
    """
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    connection.connect(uds_path)
    connection.sendall(f"CONNECT {int(port)}\n".encode("ascii"))
    greeting = bytearray()
    while not greeting.endswith(b"\n"):
        piece = connection.recv(1)
        if not piece:
            raise SandboxBackendError("The vsock handshake closed early")
        greeting.extend(piece)
        if len(greeting) > 64:
            raise SandboxBackendError("The vsock handshake is malformed")
    if not greeting.startswith(b"OK "):
        raise SandboxBackendError(
            f"The vsock handshake failed: {greeting.decode(errors='replace')!r}"
        )
    return connection


# ── The Firecracker runner ───────────────────────────────────────────


@dataclass(frozen=True)
class FirecrackerHost:
    """The pinned host artifacts one microVM boots from."""

    firecracker_binary: str
    jailer_binary: str
    kernel_image: str
    rootfs_image: str
    chroot_base: str
    uid: int = 65534
    gid: int = 65534

    def digests(self) -> dict[str, str]:
        return {
            "virtual_machine_monitor_digest": _file_digest(
                self.firecracker_binary,
            ),
            "guest_kernel_digest": _file_digest(self.kernel_image),
            "filesystem_image_digest": _file_digest(self.rootfs_image),
        }


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def jailer_argv(
    host: FirecrackerHost, *, vm_id: str, api_socket: str,
    cpu_quota: float | None, memory_bytes: int,
) -> list[str]:
    """Build the jailer command that starts one jailed Firecracker.

    The jailer applies the chroot, the namespaces, the cgroup limits,
    and the seccomp filter, then drops to the unprivileged user before
    it executes the virtual machine monitor.
    """
    argv = [
        host.jailer_binary,
        "--id", vm_id,
        "--exec-file", host.firecracker_binary,
        "--uid", str(int(host.uid)),
        "--gid", str(int(host.gid)),
        "--chroot-base-dir", host.chroot_base,
        "--new-pid-ns",
        "--cgroup", f"memory.max={int(memory_bytes)}",
    ]
    if cpu_quota:
        period = 100_000
        argv += ["--cgroup", f"cpu.max={int(cpu_quota * period)} {period}"]
    argv += ["--", "--api-sock", api_socket]
    return argv


class FirecrackerRunner:
    """Run one native scorer inside a pinned Firecracker microVM."""

    engine_name = FIRECRACKER_ENGINE

    def __init__(
        self,
        spec: dict[str, Any],
        host: FirecrackerHost,
        *,
        channel_token: str,
        launcher: Any = None,
        api_factory: Any = None,
        boot_deadline_seconds: float = 5.0,
    ) -> None:
        scorer_sandbox.validate_native_spec(spec)
        self.spec = spec
        self.host = host
        self._token = str(channel_token)
        self._launcher = launcher or self._spawn
        self._api_factory = api_factory or FirecrackerApi
        self._boot_deadline = float(boot_deadline_seconds)

    # -- pins ----------------------------------------------------------

    def verify_pins(self) -> dict[str, str]:
        """Compare every host artifact digest with the pinned spec."""
        measured = self.host.digests()
        for name, digest in measured.items():
            if str(self.spec[name]) != digest:
                raise scorer_sandbox.SandboxPolicyError(
                    f"The {name} {digest} does not match the pinned "
                    f"{self.spec[name]}"
                )
        return measured

    def runtime_digest(self) -> str:
        return content_checksum({
            "engine": FIRECRACKER_ENGINE,
            "virtual_machine_monitor_digest": self.spec[
                "virtual_machine_monitor_digest"
            ],
            "guest_kernel_digest": self.spec["guest_kernel_digest"],
            "filesystem_image_digest": self.spec["filesystem_image_digest"],
            "network_policy": self.spec["network_policy"],
            "device_policy": self.spec["device_policy"],
            "credential_policy": self.spec["credential_policy"],
            "real_clock_policy": self.spec["real_clock_policy"],
            "request_channel": "vsock-authenticated",
        })

    # -- lifecycle -----------------------------------------------------

    @staticmethod
    def _spawn(argv: list[str], **_: Any) -> Any:
        return subprocess.Popen(  # noqa: S603 - the jailer argv is built above
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )

    def execute(
        self,
        *,
        evidence_input: dict[str, Any],
        request_token: str,
        output_schema: dict[str, Any],
        scratch_root: str | None = None,
    ) -> dict[str, Any]:
        """Boot, score through the request channel, and tear down."""
        if request_token != self._token:
            raise scorer_sandbox.SandboxPolicyError(
                "The microVM request channel rejects an unauthenticated "
                "request token"
            )
        pins = self.verify_pins()
        limits = self.spec["limits"]
        vm_id = f"scorer-{uuid.uuid4().hex[:12]}"
        root = Path(scratch_root or self.host.chroot_base) / vm_id
        root.mkdir(parents=True, exist_ok=True)
        # Unix socket paths are limited to about one hundred bytes, so
        # the sockets live in a short runtime directory of their own.
        socket_dir = Path(tempfile.mkdtemp(prefix="fc-"))
        api_socket = str(socket_dir / "api.sock")
        vsock_path = str(socket_dir / "v.sock")
        evidence_image = root / "evidence.img"
        evidence_image.write_bytes(canonical_json(evidence_input).encode())
        denials: list[str] = []
        terminal_class = "completed"
        error: str | None = None
        output: bytes | None = None
        resource_report: dict[str, Any] = {
            "files_written": 0, "disk_bytes": 0, "processes": 1,
            "memory_bytes": 0, "cpu_seconds": 0.0, "wall_seconds": 0.0,
            "output_bytes": 0,
        }
        process = None
        started = time.monotonic()
        try:
            process = self._launcher(jailer_argv(
                self.host, vm_id=vm_id, api_socket=api_socket,
                cpu_quota=float(limits.get("cpu_quota") or 0) or None,
                memory_bytes=int(limits["memory_bytes"]),
            ), api_socket=api_socket, vsock_path=vsock_path)
            api = self._api_factory(api_socket)
            api.wait_ready(deadline_seconds=self._boot_deadline)
            api.put_machine_config(
                vcpu_count=int(limits["vcpu_count"]),
                memory_mib=max(1, int(limits["memory_bytes"]) // (1024 * 1024)),
            )
            api.put_boot_source(
                kernel_image_path=self.host.kernel_image,
                boot_args=(
                    "console=ttyS0 reboot=k panic=1 pci=off ip=none "
                    "random.trust_cpu=off bmas.logical_clock="
                    + str(self.spec["logical_clock"])
                ),
            )
            api.put_drive(
                "rootfs", path_on_host=self.host.rootfs_image,
                is_root_device=True, is_read_only=True,
            )
            api.put_drive(
                "evidence", path_on_host=str(evidence_image),
                is_root_device=False, is_read_only=True,
            )
            api.put_vsock(guest_cid=3, uds_path=vsock_path)
            api.start_instance()
            wall_limit = float(limits["wall_time_seconds"])
            channel = open_request_channel(vsock_path, timeout=wall_limit)
            try:
                _send_frame(channel, canonical_json({
                    "token": self._token,
                    "request": "score",
                    "evidence_mount": self.spec["input_mounts"][0]["path"],
                    "scratch": self.spec["scratch_layout"],
                    "limits": {
                        key: limits[key]
                        for key in scorer_sandbox._REQUIRED_NATIVE_LIMITS  # noqa: SLF001
                    },
                    "logical_clock": self.spec["logical_clock"],
                    "random": self.spec["random"],
                }).encode("utf-8"))
                reply = json.loads(
                    _read_frame(channel, MAX_FRAME_BYTES).decode("utf-8"),
                )
            except TimeoutError:
                terminal_class = "wall_time_limit"
                error = "The guest scorer exceeded the wall time limit"
                reply = {}
            finally:
                channel.close()
            resource_report["wall_seconds"] = round(
                time.monotonic() - started, 6,
            )
            if terminal_class == "completed":
                terminal_class, error, output, denials, reported = (
                    self._interpret_reply(
                        reply,
                        {**limits, "process_limit": self.spec["process_limit"]},
                    )
                )
                resource_report.update(reported)
                resource_report["wall_seconds"] = round(
                    time.monotonic() - started, 6,
                )
        except (OSError, SandboxBackendError) as failure:
            terminal_class = "infrastructure_failure"
            error = str(failure)[:500]
        finally:
            self._teardown(process, api_socket)
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(socket_dir, ignore_errors=True)
        payload: bytes | None = None
        if terminal_class == "completed" and output is not None:
            if len(output) > int(limits["output_bytes"]):
                terminal_class = "output_bytes_limit"
                error = "The guest breached the output_bytes limit"
            else:
                payload = output
        outcome = scorer_sandbox.outcome_from_payload(
            policy=None,
            terminal_class=terminal_class,
            error=error,
            payload=payload,
            resources={**resource_report, "fuel_used": 0},
            runtime_digest=self.runtime_digest(),
            output_schema=output_schema,
            pins={
                "policy_digest": scorer_sandbox.native_policy_digest(
                    self.spec,
                ),
                "component_digest": str(self.spec["scorer_binary_digest"]),
                "wit_digest": str(self.spec["dependency_manifest_digest"]),
                "compiler_digest": str(self.spec["image_digest"]),
                "dependency_lock_digest": str(
                    self.spec["dependency_manifest_digest"],
                ),
                "output_schema_digest": content_checksum(output_schema),
                "logical_time": str(self.spec["logical_clock"]),
                "time_zone": str(self.spec["time_zone"]),
                "locale": str(self.spec["locale"]),
                "random_algorithm": str(
                    (self.spec["random"] or {}).get("algorithm")
                    or "sha-256-counter"
                ),
                "random_seed": int((self.spec["random"] or {}).get("seed") or 0),
                **pins,
            },
        )
        outcome["denials"] = denials
        outcome["host_class"] = str((self.spec["host_classes"] or ["?"])[0])
        return outcome

    @staticmethod
    def _interpret_reply(
        reply: dict[str, Any], limits: dict[str, Any],
    ) -> tuple[str, str | None, bytes | None, list[str], dict[str, Any]]:
        denials = [str(item) for item in reply.get("denials") or []]
        reported = {
            key: reply.get("resource_report", {}).get(key, 0)
            for key in ("files_written", "disk_bytes", "processes",
                        "memory_bytes", "cpu_seconds")
        }
        status = str(reply.get("status") or "")
        if status == "denied" or (denials and status != "completed"):
            return "capability_denied", (
                "The guest reached a denied capability: "
                + ", ".join(denials)
            ), None, denials, reported
        if status != "completed":
            return "trap", str(reply.get("error") or status)[:500], None, (
                denials
            ), reported
        for key, terminal in (
            ("files_written", "file_count_limit"),
            ("disk_bytes", "disk_bytes_limit"),
            ("processes", "process_limit"),
            ("memory_bytes", "memory_bytes_limit"),
            ("cpu_seconds", "cpu_quota_limit"),
        ):
            limit_key = {
                "files_written": "file_count", "disk_bytes": "disk_bytes",
                "processes": "process_limit", "memory_bytes": "memory_bytes",
                "cpu_seconds": "cpu_quota",
            }[key]
            if float(reported[key]) > float(limits[limit_key]):
                return terminal, f"The guest breached the {key} limit", (
                    None
                ), denials, reported
        encoded = reply.get("output")
        if not isinstance(encoded, str):
            return "invalid_output", "The guest wrote no result", None, (
                denials
            ), reported
        try:
            output = base64.b64decode(encoded, validate=True)
        except ValueError:
            return "invalid_output", "The guest result is not base64", (
                None
            ), denials, reported
        return "completed", None, output, denials, reported

    def _teardown(self, process: Any, api_socket: str) -> None:
        if process is None:
            return
        with contextlib.suppress(OSError, SandboxBackendError):
            self._api_factory(api_socket).send_ctrl_alt_del()
        stop = getattr(process, "stop", None)
        if callable(stop):
            stop()
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except Exception:  # noqa: BLE001 - a stuck monitor is killed
            with contextlib.suppress(Exception):
                process.kill()
