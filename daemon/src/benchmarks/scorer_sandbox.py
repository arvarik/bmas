"""The scorer sandbox boundary: WASI contract and native microVM spec.

The module implements the documented sandbox contract exactly. The
WASI boundary grants only the scorer-input, scorer-result,
logical-time, and deterministic-random interfaces; it never links
clock, random, socket, path, environment, device, or process
interfaces, and component validation rejects every undeclared
import. Fuel is the deterministic compute limit, NaN values
canonicalize, relaxed SIMD stays disabled, and memory and table
growth stop at deterministic limits. A host deadline is only a
last-resort safety kill: it records ``sandbox_wall_time_kill`` and
never enters a byte-identical replay claim, while an earlier host
allocation failure records an infrastructure failure instead of a
scorer result. Approved native scorers that cannot compile to
WebAssembly run only under one immutable ``NativeScorerSandboxSpec``
inside a pinned microVM; a normal container is never the only
isolation boundary. Every pinned value travels in metadata, never
inside an identifier.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any

from benchmarks.provenance import canonical_json, content_checksum

BOUNDARY_ENGINE = "bmas-deterministic-boundary"
BOUNDARY_ENGINE_VERSION = "1"

# The only interfaces the WASI host grants. The custom interfaces
# supply one fixed logical time and one specified random byte stream.
GRANTED_INTERFACES = (
    "bmas:scorer-input",
    "bmas:scorer-result",
    "bmas:logical-time",
    "bmas:deterministic-random",
)

# Interfaces the host never links, whatever the component requests.
PROHIBITED_INTERFACE_PREFIXES = (
    "wasi:sockets",
    "wasi:filesystem",
    "wasi:cli/environment",
    "wasi:clocks",
    "wasi:random",
    "wasi:io/devices",
    "wasi:process",
)

# Terminal classes that stay deterministic. Only these can support a
# byte-identical scorer replay claim.
REPLAY_ELIGIBLE_CLASSES = frozenset({
    "completed",
    "fuel_exhausted",
    "memory_limit",
    "table_limit",
    "output_limit",
    "invalid_output",
    "trap",
})

NONDETERMINISTIC_CLASSES = frozenset({
    "sandbox_wall_time_kill",
    "infrastructure_failure",
})

_CANONICAL_NAN = struct.unpack("<d", struct.pack("<Q", 0x7FF8000000000000))[0]


class SandboxPolicyError(ValueError):
    """The component or specification violates the sandbox contract."""


class CapabilityDenied(RuntimeError):
    """The guest requested one undeclared capability."""


class _FuelExhausted(RuntimeError):
    pass


class _LimitExceeded(RuntimeError):
    def __init__(self, terminal_class: str) -> None:
        super().__init__(terminal_class)
        self.terminal_class = terminal_class


class _HostAllocationFailure(RuntimeError):
    pass


def canonicalize_nan(value: float) -> float:
    """Replace every NaN payload with the one canonical NaN value."""
    if isinstance(value, float) and math.isnan(value):
        return _CANONICAL_NAN
    return value


def _canonical_result_value(value: Any) -> Any:
    if isinstance(value, float):
        canonical = canonicalize_nan(value)
        if math.isnan(canonical):
            # Canonical JSON has no NaN literal, so the canonical NaN
            # serializes as one fixed marker string.
            return "nan:canonical"
        return canonical
    if isinstance(value, dict):
        return {
            str(key): _canonical_result_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonical_result_value(item) for item in value]
    return value


# ── The WASI scorer policy and runtime pins ──────────────────────────


@dataclass(frozen=True)
class WasiScorerPolicy:
    """Every pinned value for one WASI scorer version."""

    component_digest: str
    wit_digest: str
    wasi_version: str
    compiler_digest: str
    dependency_lock_digest: str
    output_schema: dict[str, Any]
    fuel_limit: int = 1_000_000
    memory_limit_bytes: int = 16_777_216
    table_limit_entries: int = 1_024
    output_limit_bytes: int = 65_536
    wall_time_limit_seconds: float = 30.0
    logical_time: str = "2026-01-01T00:00:00Z"
    time_zone: str = "UTC"
    locale: str = "C"
    random_algorithm: str = "sha-256-counter"
    random_seed: int = 0
    nan_canonicalization: bool = True
    relaxed_simd: str = "disabled"
    growth_policy: str = "deterministic_store_limiter"
    retry_limit: int = 0
    retry_reasons: tuple[str, ...] = ()

    def output_schema_digest(self) -> str:
        return content_checksum(self.output_schema)

    def policy_digest(self) -> str:
        return content_checksum({
            "component_digest": self.component_digest,
            "wit_digest": self.wit_digest,
            "wasi_version": self.wasi_version,
            "compiler_digest": self.compiler_digest,
            "dependency_lock_digest": self.dependency_lock_digest,
            "output_schema_digest": self.output_schema_digest(),
            "limits": {
                "fuel": self.fuel_limit,
                "memory_bytes": self.memory_limit_bytes,
                "table_entries": self.table_limit_entries,
                "output_bytes": self.output_limit_bytes,
                "wall_time_seconds": self.wall_time_limit_seconds,
            },
            "logical_time": self.logical_time,
            "time_zone": self.time_zone,
            "locale": self.locale,
            "random": {
                "algorithm": self.random_algorithm,
                "seed": self.random_seed,
            },
            "nan_canonicalization": self.nan_canonicalization,
            "relaxed_simd": self.relaxed_simd,
            "growth_policy": self.growth_policy,
            "retry": {
                "limit": self.retry_limit,
                "reasons": sorted(self.retry_reasons),
            },
            "granted_interfaces": list(GRANTED_INTERFACES),
        })


def runtime_digest() -> str:
    """Digest the pinned boundary engine and its configuration."""
    return content_checksum({
        "engine": BOUNDARY_ENGINE,
        "engine_version": BOUNDARY_ENGINE_VERSION,
        "nan_canonicalization": True,
        "relaxed_simd": "disabled",
        "fuel_accounting": "deterministic",
        "growth_policy": "deterministic_store_limiter",
    })


def validate_component(
    component: dict[str, Any], policy: WasiScorerPolicy,
) -> None:
    """Reject one component that imports an undeclared capability."""
    digest = str(component.get("component_digest") or "")
    if digest != policy.component_digest:
        raise SandboxPolicyError(
            "The component digest does not match the pinned policy"
        )
    for imported in component.get("imports") or []:
        name = str(imported)
        if name in GRANTED_INTERFACES:
            continue
        if any(
            name.startswith(prefix)
            for prefix in PROHIBITED_INTERFACE_PREFIXES
        ):
            raise SandboxPolicyError(
                f"The component imports the prohibited interface {name}"
            )
        raise SandboxPolicyError(
            f"The component imports the undeclared interface {name}"
        )


# ── The boundary host: the only capabilities a scorer can reach ──────


class BoundaryHost:
    """The host functions granted to one sandboxed scorer.

    Every host call charges deterministic fuel. The scorer reads its
    input, writes one bounded result, reads one fixed logical time,
    and draws from one seeded deterministic random stream. No other
    capability exists.
    """

    def __init__(
        self,
        policy: WasiScorerPolicy,
        evidence_input: dict[str, Any],
        *,
        clock: Any = None,
        allocation_failure: bool = False,
    ) -> None:
        self._policy = policy
        self._input = evidence_input
        self._clock = clock
        self._allocation_failure = allocation_failure
        self.fuel_used = 0
        self.memory_allocated = 0
        self.table_entries = 0
        self._random_counter = 0
        self.result_payload: bytes | None = None
        self._started = self._now()

    def _now(self) -> float:
        return float(self._clock()) if self._clock else 0.0

    def _check_deadline(self) -> None:
        if self._clock is None:
            return
        elapsed = self._now() - self._started
        if elapsed > self._policy.wall_time_limit_seconds:
            raise _LimitExceeded("sandbox_wall_time_kill")

    def consume_fuel(self, amount: int = 1) -> None:
        self._check_deadline()
        self.fuel_used += max(int(amount), 1)
        if self.fuel_used > self._policy.fuel_limit:
            raise _FuelExhausted

    def read_input(self) -> dict[str, Any]:
        self.consume_fuel()
        return json.loads(canonical_json(self._input))

    def logical_time(self) -> str:
        self.consume_fuel()
        return self._policy.logical_time

    def random_bytes(self, count: int) -> bytes:
        self.consume_fuel()
        output = b""
        while len(output) < count:
            block = hashlib.sha256(
                b"bmas-deterministic-random"
                + self._policy.random_seed.to_bytes(8, "big")
                + self._random_counter.to_bytes(8, "big"),
            ).digest()
            self._random_counter += 1
            output += block
        return output[:count]

    def allocate_memory(self, size_bytes: int) -> None:
        self.consume_fuel()
        if self._allocation_failure:
            # The host failed before the deterministic limit; this is
            # an infrastructure failure, never a scorer result.
            raise _HostAllocationFailure
        self.memory_allocated += int(size_bytes)
        if self.memory_allocated > self._policy.memory_limit_bytes:
            raise _LimitExceeded("memory_limit")

    def grow_table(self, entries: int) -> None:
        self.consume_fuel()
        self.table_entries += int(entries)
        if self.table_entries > self._policy.table_limit_entries:
            raise _LimitExceeded("table_limit")

    def write_result(self, result: Any) -> None:
        self.consume_fuel()
        payload = canonical_json(
            _canonical_result_value(result),
        ).encode("utf-8")
        if len(payload) > self._policy.output_limit_bytes:
            raise _LimitExceeded("output_limit")
        self.result_payload = payload

    def __getattr__(self, name: str) -> Any:
        raise CapabilityDenied(
            f"The sandbox grants no capability named {name}"
        )


def _resource_report(host: BoundaryHost) -> dict[str, Any]:
    return {
        "fuel_used": host.fuel_used,
        "memory_allocated_bytes": host.memory_allocated,
        "table_entries": host.table_entries,
        "output_bytes": len(host.result_payload or b""),
    }


def execute_component(
    *,
    component: dict[str, Any],
    policy: WasiScorerPolicy,
    evidence_input: dict[str, Any],
    guest: Any,
    clock: Any = None,
    allocation_failure: bool = False,
) -> dict[str, Any]:
    """Execute one validated scorer inside the boundary.

    The guest receives only the boundary host. The execution result
    records the terminal class, the canonical output bytes and their
    digest, the resource report, and whether the terminal class
    supports a byte-identical replay claim.
    """
    validate_component(component, policy)
    host = BoundaryHost(
        policy, evidence_input,
        clock=clock, allocation_failure=allocation_failure,
    )
    terminal_class = "completed"
    error: str | None = None
    try:
        guest(host)
    except _FuelExhausted:
        terminal_class = "fuel_exhausted"
        error = "The scorer exhausted its deterministic fuel"
    except _HostAllocationFailure:
        terminal_class = "infrastructure_failure"
        error = "The host failed an allocation before the limit"
    except _LimitExceeded as breach:
        terminal_class = breach.terminal_class
        error = f"The scorer breached the {breach.terminal_class} limit"
    except CapabilityDenied as denied:
        terminal_class = "trap"
        error = str(denied)
    except Exception as trap:  # noqa: BLE001 — a guest fault is a scorer trap.
        terminal_class = "trap"
        error = str(trap)[:500]

    raw_output_digest: str | None = None
    canonical_output: bytes | None = None
    result_digest: str | None = None
    if terminal_class == "completed":
        if host.result_payload is None:
            terminal_class = "invalid_output"
            error = "The scorer wrote no result"
        else:
            raw_output_digest = hashlib.sha256(
                host.result_payload,
            ).hexdigest()
            validation_error = _validate_output(
                host.result_payload, policy.output_schema,
            )
            if validation_error:
                # The raw output quarantines by digest; the invalid
                # bytes never become a score.
                terminal_class = "invalid_output"
                error = validation_error
            else:
                canonical_output = host.result_payload
                result_digest = raw_output_digest

    return {
        "terminal_class": terminal_class,
        "error": error,
        "canonical_output": (
            canonical_output.decode("utf-8") if canonical_output else None
        ),
        "result_digest": result_digest,
        "quarantined_output_digest": (
            raw_output_digest if terminal_class == "invalid_output"
            else None
        ),
        "replay_eligible": terminal_class in REPLAY_ELIGIBLE_CLASSES,
        "resources": _resource_report(host),
        "pins": {
            "policy_digest": policy.policy_digest(),
            "runtime_digest": runtime_digest(),
            "component_digest": policy.component_digest,
            "wit_digest": policy.wit_digest,
            "compiler_digest": policy.compiler_digest,
            "dependency_lock_digest": policy.dependency_lock_digest,
            "output_schema_digest": policy.output_schema_digest(),
            "logical_time": policy.logical_time,
            "time_zone": policy.time_zone,
            "locale": policy.locale,
            "random_algorithm": policy.random_algorithm,
            "random_seed": policy.random_seed,
        },
    }


def canonical_result_value(value: Any) -> Any:
    """Canonicalize NaN payloads inside one scorer result value."""
    return _canonical_result_value(value)


def outcome_from_payload(
    *,
    policy: WasiScorerPolicy | None,
    terminal_class: str,
    error: str | None,
    payload: bytes | None,
    resources: dict[str, Any],
    runtime_digest: str,
    output_schema: dict[str, Any] | None = None,
    pins: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the boundary outcome record every backend returns.

    A completed execution validates its payload against the output
    schema; an invalid payload quarantines by digest and never becomes
    a score. The pins come from the policy for a WASI component and
    from the native specification for a microVM.
    """
    schema = output_schema if output_schema is not None else (
        policy.output_schema if policy is not None else {}
    )
    raw_output_digest: str | None = None
    canonical_output: bytes | None = None
    result_digest: str | None = None
    if terminal_class == "completed":
        if payload is None:
            terminal_class = "invalid_output"
            error = "The scorer wrote no result"
        else:
            raw_output_digest = hashlib.sha256(payload).hexdigest()
            validation_error = _validate_output(payload, schema)
            if validation_error:
                terminal_class = "invalid_output"
                error = validation_error
            else:
                canonical_output = payload
                result_digest = raw_output_digest
    resolved_pins: dict[str, Any] = dict(pins or {})
    if policy is not None:
        resolved_pins = {
            "policy_digest": policy.policy_digest(),
            "component_digest": policy.component_digest,
            "wit_digest": policy.wit_digest,
            "compiler_digest": policy.compiler_digest,
            "dependency_lock_digest": policy.dependency_lock_digest,
            "output_schema_digest": policy.output_schema_digest(),
            "logical_time": policy.logical_time,
            "time_zone": policy.time_zone,
            "locale": policy.locale,
            "random_algorithm": policy.random_algorithm,
            "random_seed": policy.random_seed,
            **resolved_pins,
        }
    resolved_pins["runtime_digest"] = runtime_digest
    return {
        "terminal_class": terminal_class,
        "error": error,
        "canonical_output": (
            canonical_output.decode("utf-8") if canonical_output else None
        ),
        "result_digest": result_digest,
        "quarantined_output_digest": (
            raw_output_digest if terminal_class == "invalid_output"
            else None
        ),
        "replay_eligible": terminal_class in REPLAY_ELIGIBLE_CLASSES,
        "resources": dict(resources),
        "pins": resolved_pins,
    }


def _validate_output(
    payload: bytes, output_schema: dict[str, Any],
) -> str | None:
    try:
        text = payload.decode("utf-8", errors="strict")
        document = json.loads(
            text, object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError) as parse_error:
        return f"invalid output encoding or JSON: {parse_error}"[:500]
    import jsonschema

    validator = jsonschema.Draft202012Validator(output_schema)
    failure = next(iter(validator.iter_errors(document)), None)
    if failure is not None:
        return f"output violates the scorer schema: {failure.message}"[:500]
    return None


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate output key: {key!r}")
        result[key] = value
    return result


def execute_with_retry(
    *,
    component: dict[str, Any],
    policy: WasiScorerPolicy,
    evidence_input: dict[str, Any],
    guest: Any,
    clock: Any = None,
) -> dict[str, Any]:
    """Apply only the declared retry policy to one execution."""
    attempts = 0
    while True:
        outcome = execute_component(
            component=component,
            policy=policy,
            evidence_input=evidence_input,
            guest=guest,
            clock=clock,
        )
        attempts += 1
        outcome["execution_count"] = attempts
        if outcome["terminal_class"] == "completed":
            return outcome
        if outcome["terminal_class"] not in policy.retry_reasons:
            return outcome
        if attempts > policy.retry_limit:
            return outcome


# ── The native microVM specification ─────────────────────────────────


REQUIRED_NATIVE_PINS = (
    "image_digest",
    "scorer_binary_digest",
    "dependency_manifest_digest",
    "filesystem_image_digest",
    "guest_kernel_digest",
    "firmware_digest",
    "virtual_machine_monitor_digest",
    "runtime_digest",
    "host_classes",
    "architecture",
    "cpu_feature_mask",
    "equivalence_fixture_digest",
    "input_mounts",
    "scratch_layout",
    "limits",
    "network_policy",
    "device_policy",
    "process_limit",
    "credential_policy",
    "logical_clock",
    "time_zone",
    "locale",
    "real_clock_policy",
    "random",
    "retry",
    "invalid_output_policy",
    "output_schema_digest",
    "digest_algorithms",
)

_REQUIRED_NATIVE_LIMITS = (
    "file_count",
    "disk_bytes",
    "output_bytes",
    "vcpu_count",
    "cpu_quota",
    "memory_bytes",
    "wall_time_seconds",
)


def native_sandbox_spec(**overrides: Any) -> dict[str, Any]:
    """Build one complete NativeScorerSandboxSpec.

    The contract version travels in the metadata map, never inside an
    identifier. Every override replaces one documented pin.
    """
    zero = "0" * 64
    spec: dict[str, Any] = {
        "metadata": {
            "specification": "NativeScorerSandboxSpec",
            "contract_version": 1,
        },
        "image_digest": zero,
        "scorer_binary_digest": zero,
        "dependency_manifest_digest": zero,
        "filesystem_image_digest": zero,
        "guest_kernel_digest": zero,
        "firmware_digest": zero,
        "virtual_machine_monitor_digest": zero,
        "runtime_digest": zero,
        "host_classes": ["host-class-a"],
        "architecture": "x86_64",
        "cpu_feature_mask": "sse4.2,avx2",
        "equivalence_fixture_digest": zero,
        "input_mounts": [{"path": "/evidence", "mode": "read_only"}],
        "scratch_layout": {"path": "/scratch", "max_bytes": 1_048_576},
        "limits": {
            "file_count": 64,
            "disk_bytes": 8_388_608,
            "output_bytes": 65_536,
            "vcpu_count": 1,
            "cpu_quota": 1.0,
            "memory_bytes": 268_435_456,
            "wall_time_seconds": 60,
        },
        "network_policy": "deny",
        "device_policy": "deny",
        "process_limit": 1,
        "credential_policy": "deny",
        "logical_clock": "2026-01-01T00:00:00Z",
        "time_zone": "UTC",
        "locale": "C",
        "real_clock_policy": "deny",
        "random": {
            "algorithm": "sha-256-counter",
            "seed": 0,
            "byte_stream_digest": zero,
            "host_random_policy": "deny",
        },
        "retry": {"count": 0, "reasons": []},
        "invalid_output_policy": "scorer_failure_with_quarantine",
        "output_schema_digest": zero,
        "digest_algorithms": {
            "request": "sha-256",
            "policy": "sha-256",
            "resource_report": "sha-256",
            "canonical_output": "sha-256",
            "error": "sha-256",
            "artifact": "sha-256",
        },
    }
    spec.update(overrides)
    return spec


def validate_native_spec(spec: dict[str, Any]) -> None:
    """Reject publication of one specification missing any pin."""
    metadata = spec.get("metadata") or {}
    if metadata.get("specification") != "NativeScorerSandboxSpec":
        raise SandboxPolicyError(
            "The specification names NativeScorerSandboxSpec in metadata"
        )
    if not isinstance(metadata.get("contract_version"), int):
        raise SandboxPolicyError(
            "The specification pins its contract version in metadata"
        )
    missing = [
        pin for pin in REQUIRED_NATIVE_PINS
        if spec.get(pin) in (None, "", [], {})
    ]
    if missing:
        raise SandboxPolicyError(
            "The specification is missing required pins: "
            + ", ".join(sorted(missing))
        )
    limits = spec.get("limits") or {}
    missing_limits = [
        name for name in _REQUIRED_NATIVE_LIMITS
        if limits.get(name) in (None, "")
    ]
    if missing_limits:
        raise SandboxPolicyError(
            "The specification is missing required limits: "
            + ", ".join(sorted(missing_limits))
        )
    for policy_name in (
        "network_policy", "device_policy", "credential_policy",
        "real_clock_policy",
    ):
        if spec.get(policy_name) != "deny":
            raise SandboxPolicyError(
                f"The specification must deny {policy_name}"
            )
    if (spec.get("random") or {}).get("host_random_policy") != "deny":
        raise SandboxPolicyError(
            "The specification must deny host randomness"
        )


def native_policy_digest(spec: dict[str, Any]) -> str:
    """Digest one specification; any changed pin changes the digest."""
    return content_checksum(spec)


# ── The microVM guest boundary ───────────────────────────────────────


@dataclass
class MicroVmExecution:
    """One guest execution under the pinned specification."""

    spec: dict[str, Any]
    request_token: str
    files_written: int = 0
    disk_bytes: int = 0
    processes: int = 1
    memory_bytes: int = 0
    cpu_seconds: float = 0.0
    wall_seconds: float = 0.0
    output: bytes | None = None
    denials: list[str] = field(default_factory=list)


class MicroVmBoundary:
    """The virtual machine monitor policy for one native scorer.

    The guest reaches immutable evidence through one declared
    read-only mount, one bounded scratch filesystem, and one result
    channel over an authenticated request. No network, host mount,
    device, credential, real-clock, or host-random interface exists.
    """

    def __init__(self, spec: dict[str, Any], *, channel_token: str) -> None:
        validate_native_spec(spec)
        self._spec = spec
        self._token = channel_token

    def start(self, request_token: str) -> MicroVmExecution:
        if request_token != self._token:
            raise SandboxPolicyError(
                "The request channel rejects an unauthenticated request"
            )
        return MicroVmExecution(spec=self._spec, request_token=request_token)

    # Prohibited capabilities: every attempt denies inside the guest.

    def open_network(self, execution: MicroVmExecution) -> None:
        execution.denials.append("network")
        raise CapabilityDenied("The microVM has no network interface")

    def mount_host_path(self, execution: MicroVmExecution) -> None:
        execution.denials.append("host_mount")
        raise CapabilityDenied("The microVM has no host mount interface")

    def open_device(self, execution: MicroVmExecution) -> None:
        execution.denials.append("device")
        raise CapabilityDenied("The microVM has no device interface")

    def read_credential(self, execution: MicroVmExecution) -> None:
        execution.denials.append("credential")
        raise CapabilityDenied("The microVM holds no credential")

    def read_real_clock(self, execution: MicroVmExecution) -> None:
        execution.denials.append("real_clock")
        raise CapabilityDenied(
            "The guest reads only the pinned logical clock"
        )

    def read_host_random(self, execution: MicroVmExecution) -> None:
        execution.denials.append("host_random")
        raise CapabilityDenied(
            "The guest reads only the supplied random stream"
        )

    # Granted capabilities under the pinned limits.

    def read_evidence(
        self, execution: MicroVmExecution, path: str,
    ) -> str:
        mounts = {
            str(mount["path"]) for mount in self._spec["input_mounts"]
        }
        if not any(path.startswith(mount) for mount in mounts):
            execution.denials.append("undeclared_mount")
            raise CapabilityDenied(
                f"The path {path} is outside the declared read-only mount"
            )
        return path

    def logical_clock(self, execution: MicroVmExecution) -> str:
        del execution
        return str(self._spec["logical_clock"])

    def random_bytes(
        self, execution: MicroVmExecution, count: int,
    ) -> bytes:
        del execution
        seed = int(self._spec["random"]["seed"])
        output = b""
        counter = 0
        while len(output) < count:
            output += hashlib.sha256(
                b"bmas-native-random"
                + seed.to_bytes(8, "big")
                + counter.to_bytes(8, "big"),
            ).digest()
            counter += 1
        return output[:count]

    def write_scratch(
        self, execution: MicroVmExecution, size_bytes: int,
    ) -> None:
        limits = self._spec["limits"]
        execution.files_written += 1
        execution.disk_bytes += int(size_bytes)
        if execution.files_written > int(limits["file_count"]):
            raise _LimitExceeded("file_count_limit")
        if execution.disk_bytes > int(limits["disk_bytes"]):
            raise _LimitExceeded("disk_bytes_limit")

    def spawn_process(self, execution: MicroVmExecution) -> None:
        execution.processes += 1
        if execution.processes > int(self._spec["process_limit"]):
            raise _LimitExceeded("process_limit")

    def charge_cpu(
        self, execution: MicroVmExecution, seconds: float,
    ) -> None:
        limits = self._spec["limits"]
        execution.cpu_seconds += float(seconds)
        quota_seconds = float(limits["cpu_quota"]) * float(
            limits["wall_time_seconds"],
        )
        if execution.cpu_seconds > quota_seconds:
            raise _LimitExceeded("cpu_quota_limit")

    def allocate_memory(
        self, execution: MicroVmExecution, size_bytes: int,
    ) -> None:
        execution.memory_bytes += int(size_bytes)
        if execution.memory_bytes > int(
            self._spec["limits"]["memory_bytes"],
        ):
            raise _LimitExceeded("memory_bytes_limit")

    def advance_wall_clock(
        self, execution: MicroVmExecution, seconds: float,
    ) -> None:
        execution.wall_seconds += float(seconds)
        if execution.wall_seconds > float(
            self._spec["limits"]["wall_time_seconds"],
        ):
            raise _LimitExceeded("wall_time_limit")

    def write_output(
        self, execution: MicroVmExecution, payload: bytes,
    ) -> None:
        if len(payload) > int(self._spec["limits"]["output_bytes"]):
            raise _LimitExceeded("output_bytes_limit")
        execution.output = payload

    def run(self, execution: MicroVmExecution, guest: Any) -> dict[str, Any]:
        """Run one guest and report the declared failure class."""
        terminal_class = "completed"
        error: str | None = None
        try:
            guest(self, execution)
        except _LimitExceeded as breach:
            terminal_class = breach.terminal_class
            error = f"The guest breached the {breach.terminal_class}"
        except CapabilityDenied as denied:
            terminal_class = "capability_denied"
            error = str(denied)
        return {
            "terminal_class": terminal_class,
            "error": error,
            "denials": list(execution.denials),
            "resource_report": {
                "files_written": execution.files_written,
                "disk_bytes": execution.disk_bytes,
                "processes": execution.processes,
                "memory_bytes": execution.memory_bytes,
                "cpu_seconds": execution.cpu_seconds,
                "wall_seconds": execution.wall_seconds,
                "output_bytes": len(execution.output or b""),
            },
            "policy_digest": native_policy_digest(self._spec),
        }


# ── Host class equivalence qualification ─────────────────────────────


def qualify_host_classes(
    fixture_outcomes: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Qualify host classes through equal equivalence fixtures.

    Every declared host class must produce equal canonical output
    bytes, result digests, failure classes, and limit behavior. On a
    mismatch, the scorer version restricts to one qualified host
    class and claims no byte-identical replay on any other class.
    """
    if not fixture_outcomes:
        raise SandboxPolicyError(
            "Qualification requires at least one host class"
        )
    signatures = {
        host_class: content_checksum([
            {
                "canonical_output": outcome.get("canonical_output"),
                "result_digest": outcome.get("result_digest"),
                "terminal_class": outcome.get("terminal_class"),
                "resources": outcome.get("resources"),
            }
            for outcome in outcomes
        ])
        for host_class, outcomes in sorted(fixture_outcomes.items())
    }
    reference_class = sorted(signatures)[0]
    matching = sorted(
        host_class
        for host_class, signature in signatures.items()
        if signature == signatures[reference_class]
    )
    if len(matching) == len(signatures):
        return {
            "qualified_host_classes": matching,
            "restricted": False,
            "byte_identical_replay_classes": matching,
        }
    return {
        "qualified_host_classes": [reference_class],
        "restricted": True,
        "byte_identical_replay_classes": [reference_class],
        "unqualified_host_classes": sorted(
            set(signatures) - {reference_class},
        ),
    }
