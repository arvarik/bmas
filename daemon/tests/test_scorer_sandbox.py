"""Sandbox isolation, determinism, limits, and invalid-output tests.

Component validation rejects every undeclared import. Fuel, memory,
table, and output limits stop excess work deterministically, a host
deadline records a nondeterministic wall-time kill, an earlier host
allocation failure records an infrastructure failure, NaN payloads
canonicalize, logical time and random bytes repeat exactly, invalid
output quarantines by digest, the declared retry policy applies
exactly, the native specification rejects publication with any
missing pin, the microVM denies every prohibited capability and
enforces every limit, and equivalence qualification restricts an
unequal host class.
"""

from __future__ import annotations

import struct

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_scorer_spec

import database as db
from benchmarks import score_execution, scorer_plugins, scorer_sandbox
from benchmarks.score_execution import SCORER_OUTPUT_SCHEMA
from benchmarks.scorer_sandbox import (
    GRANTED_INTERFACES,
    CapabilityDenied,
    MicroVmBoundary,
    SandboxPolicyError,
    WasiScorerPolicy,
    execute_component,
    execute_with_retry,
    native_policy_digest,
    native_sandbox_spec,
    qualify_host_classes,
    validate_component,
    validate_native_spec,
)

ZERO = "0" * 64


def _policy(**overrides) -> WasiScorerPolicy:
    values = {
        "component_digest": ZERO,
        "wit_digest": ZERO,
        "wasi_version": "preview-2",
        "compiler_digest": ZERO,
        "dependency_lock_digest": ZERO,
        "output_schema": SCORER_OUTPUT_SCHEMA,
    }
    values.update(overrides)
    return WasiScorerPolicy(**values)


def _component(imports=None) -> dict:
    return {
        "component_digest": ZERO,
        "imports": list(imports or GRANTED_INTERFACES),
    }


def _valid_result() -> dict:
    return {
        "status": "scored",
        "dimensions": [{"name": "accuracy", "value": 1.0,
                        "category": None}],
        "passed": True,
        "explanation": "exact_match",
    }


def _writer_guest(result=None):
    def guest(host):
        host.read_input()
        host.write_result(result or _valid_result())
    return guest


# ── Component validation: every undeclared import rejects ────────────


@pytest.mark.parametrize("prohibited", [
    "wasi:sockets/tcp",
    "wasi:filesystem/preopens",
    "wasi:cli/environment",
    "wasi:clocks/wall-clock",
    "wasi:random/random",
    "wasi:io/devices/block",
    "wasi:process/spawn",
])
def test_validation_rejects_each_prohibited_import(prohibited):
    with pytest.raises(SandboxPolicyError, match="prohibited"):
        validate_component(_component([prohibited]), _policy())


def test_validation_rejects_any_undeclared_import():
    with pytest.raises(SandboxPolicyError, match="undeclared"):
        validate_component(
            _component(["vendor:telemetry/upload"]), _policy(),
        )


def test_validation_rejects_a_mismatched_component_digest():
    with pytest.raises(SandboxPolicyError, match="digest"):
        validate_component(
            {"component_digest": "1" * 64,
             "imports": list(GRANTED_INTERFACES)},
            _policy(),
        )


def test_granted_interfaces_pass_validation():
    validate_component(_component(), _policy())


def test_guest_reaching_an_unlinked_capability_traps():
    def adversary(host):
        host.open_network_connection()

    outcome = execute_component(
        component=_component(), policy=_policy(),
        evidence_input={}, guest=adversary,
    )
    assert outcome["terminal_class"] == "trap"
    assert "no capability" in outcome["error"]
    assert outcome["replay_eligible"] is True


# ── Deterministic limits ─────────────────────────────────────────────


def test_fuel_exhaustion_is_deterministic():
    def hungry(host):
        while True:
            host.consume_fuel(1000)

    outcomes = [
        execute_component(
            component=_component(), policy=_policy(fuel_limit=10_000),
            evidence_input={}, guest=hungry,
        )
        for _ in range(3)
    ]
    for outcome in outcomes:
        assert outcome["terminal_class"] == "fuel_exhausted"
        assert outcome["replay_eligible"] is True
        assert outcome["resources"]["fuel_used"] == (
            outcomes[0]["resources"]["fuel_used"]
        )


def test_memory_and_table_growth_stop_at_deterministic_limits():
    def memory_hog(host):
        while True:
            host.allocate_memory(1_048_576)

    memory = execute_component(
        component=_component(),
        policy=_policy(memory_limit_bytes=4_194_304),
        evidence_input={}, guest=memory_hog,
    )
    assert memory["terminal_class"] == "memory_limit"
    assert memory["replay_eligible"] is True
    assert memory["resources"]["memory_allocated_bytes"] == 5_242_880

    def table_hog(host):
        while True:
            host.grow_table(100)

    table = execute_component(
        component=_component(), policy=_policy(table_limit_entries=250),
        evidence_input={}, guest=table_hog,
    )
    assert table["terminal_class"] == "table_limit"
    assert table["replay_eligible"] is True


def test_output_limit_stops_excess_output():
    outcome = execute_component(
        component=_component(), policy=_policy(output_limit_bytes=64),
        evidence_input={},
        guest=_writer_guest({**_valid_result(),
                             "explanation": "x" * 500}),
    )
    assert outcome["terminal_class"] == "output_limit"
    assert outcome["replay_eligible"] is True


def test_wall_time_kill_is_nondeterministic():
    ticks = iter([0.0, 0.0, 100.0, 200.0, 300.0])

    def slow(host):
        for _ in range(10):
            host.consume_fuel()

    outcome = execute_component(
        component=_component(),
        policy=_policy(wall_time_limit_seconds=30.0),
        evidence_input={}, guest=slow,
        clock=lambda: next(ticks),
    )
    assert outcome["terminal_class"] == "sandbox_wall_time_kill"
    # A wall-time kill never enters a byte-identical replay claim.
    assert outcome["replay_eligible"] is False


def test_host_allocation_failure_is_infrastructure():
    def allocator(host):
        host.allocate_memory(1024)

    outcome = execute_component(
        component=_component(), policy=_policy(),
        evidence_input={}, guest=allocator,
        allocation_failure=True,
    )
    assert outcome["terminal_class"] == "infrastructure_failure"
    assert outcome["replay_eligible"] is False


# ── Determinism: NaN, logical time, random, locale ───────────────────


def test_nan_payloads_canonicalize_to_equal_bytes():
    quiet = struct.unpack("<d", struct.pack("<Q", 0x7FF8000000000000))[0]
    payload = struct.unpack("<d", struct.pack("<Q", 0x7FF8DEADBEEF0001))[0]
    outcomes = [
        execute_component(
            component=_component(), policy=_policy(),
            evidence_input={},
            guest=_writer_guest({**_valid_result(), "passed": None,
                                 "uncertainty": nan_value}),
        )
        for nan_value in (quiet, payload)
    ]
    assert outcomes[0]["terminal_class"] == "completed"
    # Two different NaN bit patterns render the same canonical bytes.
    assert outcomes[0]["canonical_output"] == (
        outcomes[1]["canonical_output"]
    )
    assert outcomes[0]["result_digest"] == outcomes[1]["result_digest"]
    assert "nan:canonical" in outcomes[0]["canonical_output"]


def test_relaxed_simd_stays_disabled_in_the_runtime_pins():
    assert scorer_sandbox.runtime_digest() == (
        scorer_sandbox.runtime_digest()
    )
    policy = _policy()
    assert policy.relaxed_simd == "disabled"
    assert policy.nan_canonicalization is True


def test_logical_time_and_random_bytes_repeat_exactly():
    captured = []

    def reader(host):
        captured.append({
            "time": host.logical_time(),
            "random": host.random_bytes(48).hex(),
        })
        host.write_result(_valid_result())

    for _ in range(3):
        outcome = execute_component(
            component=_component(), policy=_policy(random_seed=11),
            evidence_input={}, guest=reader,
        )
        assert outcome["terminal_class"] == "completed"
    assert captured[0]["time"] == "2026-01-01T00:00:00Z"
    assert captured[0] == captured[1] == captured[2]
    other_seed = []

    def other_reader(host):
        other_seed.append(host.random_bytes(48).hex())
        host.write_result(_valid_result())

    execute_component(
        component=_component(), policy=_policy(random_seed=12),
        evidence_input={}, guest=other_reader,
    )
    assert other_seed[0] != captured[0]["random"]


def test_boundary_output_is_locale_and_time_zone_independent(monkeypatch):
    import locale as locale_module
    import time as time_module

    def scorer_guest(host):
        bundle = host.read_input()
        result = scorer_plugins.DeterministicAnswerScorer().score(
            bundle["evidence"], bundle["configuration"],
        )
        host.write_result(result)

    def run():
        return execute_component(
            component=_component(), policy=_policy(),
            evidence_input={
                "evidence": {"final_output": "41.999",
                             "reference_answer": "42"},
                "configuration": {"comparison": "numeric_tolerance",
                                  "absolute_tolerance": 0.01},
            },
            guest=scorer_guest,
        )

    baseline = run()
    original = locale_module.setlocale(locale_module.LC_ALL)
    try:
        for name in ("C", "en_US.UTF-8", "de_DE.UTF-8"):
            try:
                locale_module.setlocale(locale_module.LC_ALL, name)
            except locale_module.Error:
                continue
            monkeypatch.setenv("TZ", "Pacific/Kiritimati")
            time_module.tzset()
            repeated = run()
            assert repeated["canonical_output"] == (
                baseline["canonical_output"]
            )
            assert repeated["result_digest"] == baseline["result_digest"]
    finally:
        locale_module.setlocale(locale_module.LC_ALL, original)
        monkeypatch.delenv("TZ", raising=False)
        time_module.tzset()


# ── Invalid output and retry policy ──────────────────────────────────


def test_schema_invalid_output_quarantines_by_digest():
    outcome = execute_component(
        component=_component(), policy=_policy(),
        evidence_input={},
        guest=_writer_guest({"status": "scored", "surprise": True}),
    )
    assert outcome["terminal_class"] == "invalid_output"
    assert outcome["replay_eligible"] is True
    assert len(outcome["quarantined_output_digest"]) == 64
    assert outcome["canonical_output"] is None
    assert "violates the scorer schema" in outcome["error"]


def test_missing_result_is_invalid_output():
    def silent(host):
        host.consume_fuel()

    outcome = execute_component(
        component=_component(), policy=_policy(),
        evidence_input={}, guest=silent,
    )
    assert outcome["terminal_class"] == "invalid_output"
    assert outcome["error"] == "The scorer wrote no result"


def test_retry_applies_only_declared_reasons_and_counts():
    calls = {"count": 0}

    def flaky(host):
        calls["count"] += 1
        while True:
            host.consume_fuel(1000)

    declared = execute_with_retry(
        component=_component(),
        policy=_policy(
            fuel_limit=1000,
            retry_limit=2,
            retry_reasons=("fuel_exhausted",),
        ),
        evidence_input={}, guest=flaky,
    )
    # One initial execution plus exactly two declared retries.
    assert calls["count"] == 3
    assert declared["execution_count"] == 3
    assert declared["terminal_class"] == "fuel_exhausted"

    calls["count"] = 0
    undeclared = execute_with_retry(
        component=_component(),
        policy=_policy(fuel_limit=1000, retry_limit=2,
                       retry_reasons=("trap",)),
        evidence_input={}, guest=flaky,
    )
    assert calls["count"] == 1
    assert undeclared["execution_count"] == 1


# ── Pins in the execution result ─────────────────────────────────────


def test_execution_records_every_pin():
    outcome = execute_component(
        component=_component(), policy=_policy(),
        evidence_input={}, guest=_writer_guest(),
    )
    pins = outcome["pins"]
    for name in (
        "policy_digest", "runtime_digest", "component_digest",
        "wit_digest", "compiler_digest", "dependency_lock_digest",
        "output_schema_digest",
    ):
        assert len(pins[name]) == 64, name
    assert pins["locale"] == "C"
    assert pins["time_zone"] == "UTC"
    assert pins["random_algorithm"] == "sha-256-counter"


def test_policy_digest_changes_with_each_pin():
    baseline = _policy().policy_digest()
    changed = {
        "component_digest": "1" * 64,
        "wit_digest": "2" * 64,
        "compiler_digest": "3" * 64,
        "dependency_lock_digest": "4" * 64,
        "fuel_limit": 5,
        "memory_limit_bytes": 6,
        "output_limit_bytes": 7,
        "logical_time": "2027-01-01T00:00:00Z",
        "locale": "en_US.UTF-8",
        "time_zone": "America/New_York",
        "random_seed": 8,
    }
    digests = {
        name: _policy(**{name: value}).policy_digest()
        for name, value in changed.items()
    }
    assert baseline not in digests.values()
    assert len(set(digests.values())) == len(digests)


# ── The native microVM specification ─────────────────────────────────


def test_complete_native_spec_validates():
    spec = native_sandbox_spec()
    validate_native_spec(spec)
    assert spec["metadata"]["contract_version"] == 1


@pytest.mark.parametrize(
    "pin", scorer_sandbox.REQUIRED_NATIVE_PINS,
)
def test_each_missing_native_pin_rejects_publication(pin):
    spec = native_sandbox_spec()
    del spec[pin]
    with pytest.raises(SandboxPolicyError, match="missing required"):
        validate_native_spec(spec)


def test_each_missing_native_limit_rejects_publication():
    for limit in ("file_count", "disk_bytes", "output_bytes",
                  "vcpu_count", "cpu_quota", "memory_bytes",
                  "wall_time_seconds"):
        spec = native_sandbox_spec()
        del spec["limits"][limit]
        with pytest.raises(SandboxPolicyError, match="missing required"):
            validate_native_spec(spec)


def test_native_spec_must_deny_every_ambient_capability():
    for policy_name in ("network_policy", "device_policy",
                        "credential_policy", "real_clock_policy"):
        spec = native_sandbox_spec(**{policy_name: "allow"})
        with pytest.raises(SandboxPolicyError, match="deny"):
            validate_native_spec(spec)
    spec = native_sandbox_spec()
    spec["random"]["host_random_policy"] = "allow"
    with pytest.raises(SandboxPolicyError, match="host randomness"):
        validate_native_spec(spec)


def test_each_changed_native_pin_changes_the_policy_digest():
    baseline = native_policy_digest(native_sandbox_spec())
    one = "1" * 64
    variants = [
        native_sandbox_spec(image_digest=one),
        native_sandbox_spec(scorer_binary_digest=one),
        native_sandbox_spec(dependency_manifest_digest=one),
        native_sandbox_spec(filesystem_image_digest=one),
        native_sandbox_spec(guest_kernel_digest=one),
        native_sandbox_spec(firmware_digest=one),
        native_sandbox_spec(virtual_machine_monitor_digest=one),
        native_sandbox_spec(runtime_digest=one),
        native_sandbox_spec(output_schema_digest=one),
        native_sandbox_spec(logical_clock="2027-06-01T00:00:00Z"),
        native_sandbox_spec(time_zone="America/New_York"),
        native_sandbox_spec(locale="en_US.UTF-8"),
    ]
    spec = native_sandbox_spec()
    spec["limits"]["memory_bytes"] = 1
    variants.append(spec)
    seeded = native_sandbox_spec()
    seeded["random"]["seed"] = 99
    variants.append(seeded)
    digests = [native_policy_digest(variant) for variant in variants]
    assert baseline not in digests
    assert len(set(digests)) == len(digests)


def test_native_result_records_every_effective_pin():
    spec = native_sandbox_spec(
        locale="fr_FR.UTF-8",
        time_zone="Europe/Paris",
        logical_clock="2026-06-01T00:00:00Z",
    )
    spec["random"]["seed"] = 21
    boundary = MicroVmBoundary(spec, channel_token="token-a")
    execution = boundary.start("token-a")
    report = boundary.run(execution, lambda vm, run: None)
    assert report["policy_digest"] == native_policy_digest(spec)
    assert report["policy_digest"] != native_policy_digest(
        native_sandbox_spec(),
    )


# ── The microVM guest boundary ───────────────────────────────────────


def _boundary() -> tuple[MicroVmBoundary, object]:
    boundary = MicroVmBoundary(
        native_sandbox_spec(), channel_token="token-a",
    )
    return boundary, boundary.start("token-a")


def test_request_channel_requires_authentication():
    boundary = MicroVmBoundary(
        native_sandbox_spec(), channel_token="token-a",
    )
    with pytest.raises(SandboxPolicyError, match="unauthenticated"):
        boundary.start("token-forged")


def test_microvm_denies_every_prohibited_capability():
    boundary, execution = _boundary()

    def adversary(vm, run):
        for attack in (
            vm.open_network, vm.mount_host_path, vm.open_device,
            vm.read_credential, vm.read_real_clock, vm.read_host_random,
        ):
            with pytest.raises(CapabilityDenied):
                attack(run)
        with pytest.raises(CapabilityDenied):
            vm.read_evidence(run, "/etc/passwd")

    report = boundary.run(execution, adversary)
    assert report["terminal_class"] == "completed"
    assert set(report["denials"]) == {
        "network", "host_mount", "device", "credential", "real_clock",
        "host_random", "undeclared_mount",
    }


def test_microvm_grants_only_the_declared_capabilities():
    boundary, execution = _boundary()

    def guest(vm, run):
        assert vm.read_evidence(run, "/evidence/case.json")
        assert vm.logical_clock(run) == "2026-01-01T00:00:00Z"
        assert vm.random_bytes(run, 16) == vm.random_bytes(run, 16)
        vm.write_scratch(run, 1024)
        vm.write_output(run, b'{"status": "scored"}')

    report = boundary.run(execution, guest)
    assert report["terminal_class"] == "completed"
    assert report["resource_report"]["output_bytes"] == 20


@pytest.mark.parametrize(("attack", "expected_class"), [
    (lambda vm, run: [vm.write_scratch(run, 1) for _ in range(100)],
     "file_count_limit"),
    (lambda vm, run: vm.write_scratch(run, 10_000_000),
     "disk_bytes_limit"),
    (lambda vm, run: [vm.spawn_process(run) for _ in range(4)],
     "process_limit"),
    (lambda vm, run: vm.charge_cpu(run, 100.0), "cpu_quota_limit"),
    (lambda vm, run: vm.allocate_memory(run, 300_000_000),
     "memory_bytes_limit"),
    (lambda vm, run: vm.advance_wall_clock(run, 100.0),
     "wall_time_limit"),
    (lambda vm, run: vm.write_output(run, b"x" * 100_000),
     "output_bytes_limit"),
])
def test_microvm_enforces_each_resource_limit(attack, expected_class):
    boundary, execution = _boundary()
    report = boundary.run(execution, attack)
    assert report["terminal_class"] == expected_class
    assert report["resource_report"]


# ── Host class equivalence qualification ─────────────────────────────


def _fixture_outcome(digest: str) -> dict:
    return {
        "canonical_output": '{"status":"scored"}',
        "result_digest": digest,
        "terminal_class": "completed",
        "resources": {"fuel_used": 10},
    }


def test_equal_host_classes_all_qualify():
    outcome = qualify_host_classes({
        "host-class-a": [_fixture_outcome("a" * 64)],
        "host-class-b": [_fixture_outcome("a" * 64)],
    })
    assert outcome["restricted"] is False
    assert outcome["qualified_host_classes"] == [
        "host-class-a", "host-class-b",
    ]


def test_equivalence_mismatch_restricts_to_one_host_class():
    outcome = qualify_host_classes({
        "host-class-a": [_fixture_outcome("a" * 64)],
        "host-class-b": [_fixture_outcome("b" * 64)],
    })
    assert outcome["restricted"] is True
    assert outcome["qualified_host_classes"] == ["host-class-a"]
    # No byte-identical replay claim exists on the unequal class.
    assert outcome["byte_identical_replay_classes"] == ["host-class-a"]
    assert outcome["unqualified_host_classes"] == ["host-class-b"]


# ── Stored score records trace to evidence and pinned scorers ────────


@pytest_asyncio.fixture
async def scoring_db(tmp_path, monkeypatch):
    from test_evidence_capture import make_attempts

    from benchmarks import evidence_capture, facade

    path = str(tmp_path / "scoring.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    attempts = await make_attempts(1)
    await facade.execute(
        "register_scorer_version", {"record": valid_scorer_spec()},
    )
    await evidence_capture.capture_attempt_evidence(
        attempt_id=attempts[0],
        run_manifest={"run_id": "run-evidence"},
        runtime_specification={"runtime": "classic"},
        case={"case_id": "case-0"},
        trace_events=[{"kind": "action", "action": "answer"}],
        final_output="42",
        resources={"cost": None, "tokens": 10, "latency_ms": 5},
        seed_evidence={"requested_seed": 1, "seed_control": "recorded"},
        ledger_references={"reservation_id": "reservation-a"},
    )
    return attempts[0]


@pytest.mark.asyncio
async def test_score_traces_to_evidence_and_pinned_scorer(scoring_db):
    from benchmarks import evaluation_records

    result = await score_execution.score_attempt(
        attempt_id=scoring_db,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="deterministic",
        configuration={"comparison": "exact"},
        extra_evidence={"final_output": "42", "reference_answer": "42"},
    )
    assert result["status"] == "scored"
    assert result["terminal_class"] == "completed"
    assert result["replay_eligible"] is True

    stored = await evaluation_records.get_record(
        "score-record", result["score_id"],
    )
    assert stored["attempt_id"] == scoring_db
    assert stored["scorer_version_id"] == "scorer-exact-match:2"
    sandbox = stored["record"]["sandbox"]
    for name in ("policy_digest", "runtime_digest", "component_digest",
                 "wit_digest", "compiler_digest",
                 "dependency_lock_digest", "output_schema_digest"):
        assert len(sandbox[name]) == 64, name
    assert sandbox["boundary"] == "wasi_component"
    assert sandbox["replay_eligible"] is True


@pytest.mark.asyncio
async def test_score_without_evidence_rejects(scoring_db):
    with pytest.raises(
        score_execution.ScoreExecutionError, match="No immutable evidence",
    ):
        await score_execution.score_attempt(
            attempt_id="attempt-unknown",
            scorer_id="scorer-exact-match",
            scorer_version="2",
            plugin_type="deterministic",
        )


@pytest.mark.asyncio
async def test_score_without_pinned_scorer_rejects(scoring_db):
    with pytest.raises(
        score_execution.ScoreExecutionError, match="No pinned scorer",
    ):
        await score_execution.score_attempt(
            attempt_id=scoring_db,
            scorer_id="scorer-unregistered",
            scorer_version="1",
            plugin_type="deterministic",
        )


@pytest.mark.asyncio
async def test_unavailable_evidence_stores_an_error_record(scoring_db):
    result = await score_execution.score_attempt(
        attempt_id=scoring_db,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="deterministic",
        configuration={"comparison": "exact"},
    )
    assert result["status"] == "error"
    assert "unavailable" in result["record"]["error"]
    assert result["record"]["passed"] is None


@pytest.mark.asyncio
async def test_stored_score_records_are_immutable(scoring_db):
    import aiosqlite

    result = await score_execution.score_attempt(
        attempt_id=scoring_db,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="deterministic",
        configuration={"comparison": "exact"},
        extra_evidence={"final_output": "42", "reference_answer": "42"},
    )
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE score_records SET status = 'excluded' "
                "WHERE id = ?",
                (result["score_id"],),
            )
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "DELETE FROM score_records WHERE id = ?",
                (result["score_id"],),
            )


@pytest.mark.asyncio
async def test_equal_evidence_produces_equal_score_digests(scoring_db):
    results = [
        await score_execution.score_attempt(
            attempt_id=scoring_db,
            scorer_id="scorer-exact-match",
            scorer_version="2",
            plugin_type="deterministic",
            configuration={"comparison": "exact"},
            extra_evidence={"final_output": "42",
                            "reference_answer": "42"},
        )
        for _ in range(2)
    ]
    assert results[0]["outcome"]["canonical_output"] == (
        results[1]["outcome"]["canonical_output"]
    )
    assert results[0]["outcome"]["result_digest"] == (
        results[1]["outcome"]["result_digest"]
    )
    assert results[0]["record"]["sandbox"]["policy_digest"] == (
        results[1]["record"]["sandbox"]["policy_digest"]
    )
