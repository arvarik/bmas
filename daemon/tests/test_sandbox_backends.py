"""The real isolation backends behind the scorer boundary contract.

The Wasmtime runner executes the reference components: the exact-match
scorer completes with pinned digests, the fuel bomb exhausts its
deterministic fuel, the memory bomb hits the store limiter, a runaway
loop dies at the epoch wall-time kill, the WASI clock import rejects
before instantiation, and a NaN result canonicalizes. The Firecracker
runner verifies every artifact digest before boot, configures no
network device, authenticates the vsock request channel, maps guest
replies onto the microVM terminal classes, and tears the jailer
down. Firecracker itself needs KVM, so a fake jailer answers the API
socket and the vsock socket exactly the way Firecracker does.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import UnixStreamServer

import pytest
from test_scorer_sandbox import scoring_db  # noqa: F401 - pytest fixture

from benchmarks import sandbox_backends as backends
from benchmarks import score_execution, scorer_plugins, scorer_sandbox
from benchmarks.provenance import canonical_json

COMPONENTS = (
    Path(__file__).resolve().parents[2]
    / "conformance" / "reference_scorer" / "components"
)

pytestmark = pytest.mark.skipif(
    not backends.wasmtime_available(),
    reason="the Wasmtime backend needs the wasmtime distribution",
)


def _artifact(name: str) -> backends.ComponentArtifact:
    return backends.ComponentArtifact.from_path(COMPONENTS / f"{name}.wat")


def _policy(artifact, **limits):
    return score_execution.component_policy_for(
        artifact, {"seed": 3, "limits": limits},
    )


def _evidence(expected: str, actual: str) -> dict:
    return {
        "evidence": {"reference_answer": expected, "final_output": actual},
        "configuration": {},
    }


# ── The Wasmtime component runner ────────────────────────────────────


def test_exact_match_component_scores_through_wasmtime():
    artifact = _artifact("exact-match")
    runner = backends.WasmtimeComponentRunner()
    policy = _policy(artifact)
    passed = runner.execute(
        artifact=artifact, policy=policy, evidence_input=_evidence("42", "42"),
    )
    failed = runner.execute(
        artifact=artifact, policy=policy, evidence_input=_evidence("42", "41"),
    )
    assert passed["terminal_class"] == "completed"
    assert passed["replay_eligible"] is True
    output = json.loads(passed["canonical_output"])
    assert output["passed"] is True
    assert output["dimensions"] == [
        {"category": None, "name": "score", "value": 1.0},
    ]
    assert json.loads(failed["canonical_output"])["passed"] is False
    assert passed["resources"]["fuel_used"] > 0
    assert passed["resources"]["host_calls"] == 2
    pins = passed["pins"]
    assert pins["component_digest"] == artifact.digest
    assert pins["wit_digest"] == backends.wit_digest()
    assert pins["runtime_digest"] == backends.wasmtime_runtime_digest()
    assert len(pins["runtime_digest"]) == 64
    assert pins["random_algorithm"] == "sha-256-counter"


def test_equal_inputs_produce_equal_bytes_and_digests():
    artifact = _artifact("exact-match")
    runner = backends.WasmtimeComponentRunner()
    policy = _policy(artifact)
    first = runner.execute(
        artifact=artifact, policy=policy,
        evidence_input=_evidence("héllo", "héllo"),
    )
    second = runner.execute(
        artifact=artifact, policy=policy,
        evidence_input=_evidence("héllo", "héllo"),
    )
    assert first["canonical_output"] == second["canonical_output"]
    assert first["result_digest"] == second["result_digest"]
    assert first["resources"]["fuel_used"] == second["resources"]["fuel_used"]


def test_fuel_bomb_exhausts_deterministic_fuel():
    artifact = _artifact("fuel-bomb")
    outcome = backends.WasmtimeComponentRunner().execute(
        artifact=artifact, policy=_policy(artifact, fuel_limit=50_000),
        evidence_input=_evidence("a", "a"),
    )
    assert outcome["terminal_class"] == "fuel_exhausted"
    assert outcome["resources"]["fuel_used"] == 50_000
    assert outcome["replay_eligible"] is True
    assert outcome["canonical_output"] is None


def test_wall_time_kill_is_the_last_resort_and_not_replayable():
    artifact = _artifact("fuel-bomb")
    outcome = backends.WasmtimeComponentRunner(
        epoch_tick_seconds=0.005,
    ).execute(
        artifact=artifact,
        policy=_policy(
            artifact, fuel_limit=10**12, wall_time_limit_seconds=0.1,
        ),
        evidence_input=_evidence("a", "a"),
    )
    assert outcome["terminal_class"] == "sandbox_wall_time_kill"
    assert outcome["replay_eligible"] is False


def test_memory_bomb_stops_at_the_store_limiter():
    artifact = _artifact("memory-bomb")
    outcome = backends.WasmtimeComponentRunner().execute(
        artifact=artifact,
        policy=_policy(artifact, memory_limit_bytes=4 * 65536),
        evidence_input=_evidence("a", "a"),
    )
    assert outcome["terminal_class"] == "memory_limit"
    assert outcome["replay_eligible"] is True


def test_prohibited_clock_import_rejects_before_instantiation():
    artifact = _artifact("clock-import")
    manifest = backends.component_manifest(artifact)
    assert manifest["imports"] == ["wasi:clocks/wall-clock@0.2.0"]
    with pytest.raises(
        scorer_sandbox.SandboxPolicyError, match="prohibited interface",
    ):
        backends.WasmtimeComponentRunner().execute(
            artifact=artifact, policy=_policy(artifact),
            evidence_input=_evidence("a", "a"),
        )


def test_granted_host_interfaces_map_onto_boundary_names():
    manifest = backends.component_manifest(_artifact("exact-match"))
    assert manifest["imports"] == [
        "bmas:logical-time", "bmas:deterministic-random",
    ]
    assert set(manifest["imports"]) <= set(scorer_sandbox.GRANTED_INTERFACES)
    assert backends.logical_time_seconds("2026-01-01T00:00:00Z") == (
        1767225600
    )


def test_nan_result_canonicalizes_to_one_marker():
    artifact = _artifact("nan-result")
    outcome = backends.WasmtimeComponentRunner().execute(
        artifact=artifact, policy=_policy(artifact),
        evidence_input=_evidence("a", "a"),
    )
    assert outcome["terminal_class"] == "completed"
    assert '"value":"nan:canonical"' in outcome["canonical_output"]


def test_component_digest_mismatch_rejects():
    artifact = _artifact("exact-match")
    other = _artifact("nan-result")
    with pytest.raises(
        scorer_sandbox.SandboxPolicyError, match="digest does not match",
    ):
        backends.WasmtimeComponentRunner().execute(
            artifact=artifact, policy=_policy(other),
            evidence_input=_evidence("a", "a"),
        )


def test_runtime_digest_pins_the_wasmtime_configuration():
    digest = backends.wasmtime_runtime_digest()
    assert len(digest) == 64
    assert backends.wasmtime_version()
    assert backends.wit_digest() == hashlib.sha256(
        backends.SCORER_WIT.encode("utf-8"),
    ).hexdigest()


@pytest.mark.asyncio
async def test_score_attempt_records_the_wasi_component_boundary(scoring_db):  # noqa: F811
    from benchmarks import evaluation_records

    result = await score_execution.score_attempt(
        attempt_id=scoring_db,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="wasi_component",
        configuration={"seed": 1},
        extra_evidence={"final_output": "42", "reference_answer": "42"},
        component=_artifact("exact-match"),
    )
    assert result["status"] == "scored"
    assert result["terminal_class"] == "completed"
    stored = await evaluation_records.get_record(
        "score-record", result["score_id"],
    )
    sandbox = stored["record"]["sandbox"]
    assert sandbox["boundary"] == "wasi_component"
    assert sandbox["runtime_digest"] == backends.wasmtime_runtime_digest()
    assert sandbox["component_digest"] == _artifact("exact-match").digest
    assert sandbox["fuel_used"] > 0
    assert stored["record"]["passed"] is True


def test_component_plugin_never_runs_in_process():
    plugin = scorer_plugins.plugin_for(
        "wasi_component", component=_artifact("exact-match"),
    )
    assert plugin.trust_class == "sandboxed_wasi"
    assert score_execution.boundary_for(plugin) == "wasi_component"
    with pytest.raises(scorer_plugins.ScorerPluginError, match="Wasmtime"):
        plugin.score({}, {})
    with pytest.raises(scorer_plugins.ScorerPluginError, match="component"):
        scorer_plugins.plugin_for("wasi_component")
    assert score_execution.boundary_for(
        scorer_plugins.plugin_for("deterministic"),
    ) == "trusted_service"


# ── A fake Firecracker: the API socket and the vsock socket ──────────


class _FakeFirecracker:
    """Answer the Firecracker REST API and the vsock socket like the VMM."""

    def __init__(self, *, api_socket: str, vsock_path: str, guest) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.started = False
        self.stopped = False
        self._guest = guest
        self._api_socket = api_socket
        self._vsock_path = vsock_path
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                return json.loads(raw) if raw else None

            def _reply(self, status: int, body=None) -> None:
                payload = json.dumps(body or {}).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except BrokenPipeError:
                    return

            def do_GET(self):
                fake.calls.append(("GET", self.path, None))
                self._reply(200, {"id": "fake", "state": "Running"})

            def do_PUT(self):
                body = self._body()
                fake.calls.append(("PUT", self.path, body))
                if self.path.startswith("/network-interfaces"):
                    self._reply(400, {"fault_message": "no network"})
                    return
                if self.path == "/actions" and body == {
                    "action_type": "InstanceStart",
                }:
                    fake.started = True
                    fake._serve_vsock()
                if self.path == "/actions" and body == {
                    "action_type": "SendCtrlAltDel",
                }:
                    fake.stopped = True
                self._reply(204)

        class ApiServer(UnixStreamServer, HTTPServer):
            allow_reuse_address = True

        self._server = ApiServer(api_socket, Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()
        self._vsock_thread: threading.Thread | None = None

    def _serve_vsock(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self._vsock_path)
        listener.listen(1)
        listener.settimeout(5.0)

        def serve() -> None:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                handshake = b""
                while not handshake.endswith(b"\n"):
                    handshake += connection.recv(1)
                assert handshake == b"CONNECT 5200\n"
                connection.sendall(b"OK 5200\n")
                header = connection.recv(4)
                (length,) = struct.unpack("!I", header)
                request = json.loads(connection.recv(length).decode())
                reply = self._guest(request)
                if reply is None:
                    return
                payload = json.dumps(reply).encode()
                connection.sendall(struct.pack("!I", len(payload)) + payload)
            listener.close()

        self._vsock_thread = threading.Thread(target=serve, daemon=True)
        self._vsock_thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _pinned(tmp_path: Path, **overrides):
    binary = tmp_path / "firecracker"
    jailer = tmp_path / "jailer"
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    for path, content in ((binary, b"vmm"), (jailer, b"jailer"),
                          (kernel, b"kernel"), (rootfs, b"rootfs")):
        path.write_bytes(content)
    host = backends.FirecrackerHost(
        firecracker_binary=str(binary), jailer_binary=str(jailer),
        kernel_image=str(kernel), rootfs_image=str(rootfs),
        chroot_base=str(tmp_path / "jail"),
    )
    limits = overrides.pop("limits", None) or {
        "file_count": 4, "disk_bytes": 4096, "output_bytes": 4096,
        "vcpu_count": 1, "cpu_quota": 1.0,
        "memory_bytes": 64 * 1024 * 1024, "wall_time_seconds": 2,
    }
    spec = scorer_sandbox.native_sandbox_spec(
        **host.digests(), limits=limits, process_limit=2, **overrides,
    )
    return host, spec


def _launcher(fakes: list, guest):
    def launch(argv, *, api_socket, vsock_path):
        assert argv[0].endswith("jailer")
        assert "--new-pid-ns" in argv
        assert "--api-sock" in argv
        fake = _FakeFirecracker(
            api_socket=api_socket, vsock_path=vsock_path, guest=guest,
        )
        fakes.append(fake)
        return fake

    return launch


def _completed_guest(request):
    assert request["token"] == "token-a"
    assert request["evidence_mount"] == "/evidence"
    output = canonical_json({
        "status": "scored",
        "dimensions": [{"name": "native", "value": 1.0, "category": None}],
        "passed": True,
        "explanation": "native scorer",
    }).encode()
    return {
        "status": "completed",
        "output": base64.b64encode(output).decode(),
        "resource_report": {"files_written": 1, "disk_bytes": 10,
                            "processes": 1, "memory_bytes": 1024,
                            "cpu_seconds": 0.01},
        "denials": [],
    }


def test_firecracker_boots_pinned_artifacts_without_a_network(tmp_path):
    host, spec = _pinned(tmp_path)
    fakes: list = []
    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a",
        launcher=_launcher(fakes, _completed_guest),
    )
    outcome = runner.execute(
        evidence_input={"evidence": {"final_output": "42"}},
        request_token="token-a",
        output_schema=score_execution.SCORER_OUTPUT_SCHEMA,
        scratch_root=str(tmp_path / "scratch"),
    )
    assert outcome["terminal_class"] == "completed", outcome["error"]
    assert json.loads(outcome["canonical_output"])["passed"] is True
    assert outcome["pins"]["runtime_digest"] == runner.runtime_digest()
    assert outcome["pins"]["guest_kernel_digest"] == spec["guest_kernel_digest"]
    assert outcome["host_class"] == spec["host_classes"][0]
    fake = fakes[0]
    paths = [path for _method, path, _body in fake.calls]
    assert "/machine-config" in paths
    assert "/boot-source" in paths
    assert "/drives/rootfs" in paths and "/drives/evidence" in paths
    assert "/vsock" in paths
    assert not any(path.startswith("/network-interfaces") for path in paths)
    assert not any(path.startswith("/mmds") for path in paths)
    drives = {
        body["drive_id"]: body for _m, path, body in fake.calls
        if path.startswith("/drives/")
    }
    assert drives["rootfs"]["is_read_only"] is True
    assert drives["evidence"]["is_read_only"] is True
    boot = next(body for _m, path, body in fake.calls if path == "/boot-source")
    assert "ip=none" in boot["boot_args"]
    assert fake.started and fake.stopped
    assert not (tmp_path / "scratch").exists() or not any(
        (tmp_path / "scratch").iterdir()
    )


def test_firecracker_refuses_a_tampered_kernel(tmp_path):
    host, spec = _pinned(tmp_path)
    Path(host.kernel_image).write_bytes(b"tampered")
    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a",
        launcher=_launcher([], _completed_guest),
    )
    with pytest.raises(
        scorer_sandbox.SandboxPolicyError, match="guest_kernel_digest",
    ):
        runner.execute(
            evidence_input={}, request_token="token-a",
            output_schema=score_execution.SCORER_OUTPUT_SCHEMA,
            scratch_root=str(tmp_path / "scratch"),
        )


def test_firecracker_request_channel_requires_the_token(tmp_path):
    host, spec = _pinned(tmp_path)
    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a",
        launcher=_launcher([], _completed_guest),
    )
    with pytest.raises(
        scorer_sandbox.SandboxPolicyError, match="unauthenticated",
    ):
        runner.execute(
            evidence_input={}, request_token="token-b",
            output_schema=score_execution.SCORER_OUTPUT_SCHEMA,
            scratch_root=str(tmp_path / "scratch"),
        )


@pytest.mark.parametrize(
    ("reply", "terminal_class"),
    [
        ({"status": "denied", "denials": ["network"]}, "capability_denied"),
        ({"status": "completed", "output": "not base64!"}, "invalid_output"),
        ({"status": "completed", "resource_report": {"files_written": 9},
          "output": base64.b64encode(b"{}").decode()}, "file_count_limit"),
        ({"status": "completed", "resource_report": {"memory_bytes": 10**9},
          "output": base64.b64encode(b"{}").decode()},
         "memory_bytes_limit"),
        ({"status": "crashed", "error": "segfault"}, "trap"),
    ],
)
def test_firecracker_maps_guest_replies_onto_terminal_classes(
    tmp_path, reply, terminal_class,
):
    host, spec = _pinned(tmp_path)
    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a",
        launcher=_launcher([], lambda _request: reply),
    )
    outcome = runner.execute(
        evidence_input={}, request_token="token-a",
        output_schema=score_execution.SCORER_OUTPUT_SCHEMA,
        scratch_root=str(tmp_path / "scratch"),
    )
    assert outcome["terminal_class"] == terminal_class, outcome["error"]
    assert outcome["canonical_output"] is None


def test_firecracker_wall_time_limit_kills_a_silent_guest(tmp_path):
    host, spec = _pinned(tmp_path, limits={
        "file_count": 4, "disk_bytes": 4096, "output_bytes": 4096,
        "vcpu_count": 1, "cpu_quota": 1.0,
        "memory_bytes": 64 * 1024 * 1024, "wall_time_seconds": 0.2,
    })
    fakes: list = []

    def silent(_request):
        import time

        time.sleep(0.6)
        return None

    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a", launcher=_launcher(fakes, silent),
    )
    outcome = runner.execute(
        evidence_input={}, request_token="token-a",
        output_schema=score_execution.SCORER_OUTPUT_SCHEMA,
        scratch_root=str(tmp_path / "scratch"),
    )
    assert outcome["terminal_class"] == "wall_time_limit"
    assert fakes[0].stopped


def test_firecracker_output_limit_and_schema_validation(tmp_path):
    host, spec = _pinned(tmp_path)
    huge = base64.b64encode(b"x" * 5000).decode()
    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a",
        launcher=_launcher([], lambda _r: {"status": "completed",
                                           "output": huge}),
    )
    outcome = runner.execute(
        evidence_input={}, request_token="token-a",
        output_schema=score_execution.SCORER_OUTPUT_SCHEMA,
        scratch_root=str(tmp_path / "scratch"),
    )
    assert outcome["terminal_class"] == "output_bytes_limit"
    invalid = base64.b64encode(b'{"status":"scored"}').decode()
    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a",
        launcher=_launcher([], lambda _r: {"status": "completed",
                                           "output": invalid}),
    )
    outcome = runner.execute(
        evidence_input={}, request_token="token-a",
        output_schema=score_execution.SCORER_OUTPUT_SCHEMA,
        scratch_root=str(tmp_path / "scratch"),
    )
    assert outcome["terminal_class"] == "invalid_output"
    assert outcome["quarantined_output_digest"] == hashlib.sha256(
        b'{"status":"scored"}',
    ).hexdigest()


def test_jailer_argv_applies_cgroups_and_drops_privileges(tmp_path):
    host, _spec = _pinned(tmp_path)
    argv = backends.jailer_argv(
        host, vm_id="scorer-1", api_socket="/run/api.sock",
        cpu_quota=0.5, memory_bytes=1024,
    )
    assert argv[:3] == [host.jailer_binary, "--id", "scorer-1"]
    assert "--exec-file" in argv and "--chroot-base-dir" in argv
    assert "--cgroup" in argv
    assert "cpu.max=50000 100000" in argv
    assert "memory.max=1024" in argv
    assert argv[argv.index("--uid") + 1] == "65534"
    assert argv[-2:] == ["--api-sock", "/run/api.sock"]


@pytest.mark.asyncio
async def test_score_attempt_records_the_native_microvm_boundary(
    scoring_db, tmp_path,  # noqa: F811
):
    from benchmarks import evaluation_records

    host, spec = _pinned(tmp_path)
    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a",
        launcher=_launcher([], _completed_guest),
    )
    result = await score_execution.score_attempt(
        attempt_id=scoring_db,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="native_microvm",
        configuration={"request_token": "token-a"},
        extra_evidence={"final_output": "42"},
        microvm=runner,
    )
    assert result["status"] == "scored", result["outcome"]["error"]
    stored = await evaluation_records.get_record(
        "score-record", result["score_id"],
    )
    sandbox = stored["record"]["sandbox"]
    assert sandbox["boundary"] == "native_microvm"
    assert sandbox["policy_digest"] == scorer_sandbox.native_policy_digest(spec)
    assert sandbox["runtime_digest"] == runner.runtime_digest()


def test_unix_socket_paths_stay_short_enough_for_the_kernel(tmp_path):
    """A deep scratch root never pushes a socket past the path limit."""
    deep = tmp_path.joinpath(*(["deep-directory-name"] * 6))
    host, spec = _pinned(tmp_path)
    runner = backends.FirecrackerRunner(
        spec, host, channel_token="token-a",
        launcher=_launcher([], _completed_guest),
    )
    outcome = runner.execute(
        evidence_input={}, request_token="token-a",
        output_schema=score_execution.SCORER_OUTPUT_SCHEMA,
        scratch_root=str(deep),
    )
    assert outcome["terminal_class"] == "completed", outcome["error"]
    assert not any(deep.iterdir()) if deep.exists() else True
