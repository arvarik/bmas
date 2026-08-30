"""Tests for the isolated deterministic reference scorer.

The suite proves the package contract:

1. Both scorers reproduce the frozen fixture bytes and digests.
2. One normalized input has one byte-identical output on every host.
3. Every prohibited capability stays rejected: network, environment,
   clock, randomness, child processes, and filesystem writes.
4. Every resource limit and every invalid input maps to its pinned
   failure code, and non-finite numbers never enter scoring.
5. The output obeys the pure result schema, and the scorer result
   contract stays disjoint from the test-manifest runner contract.
"""

from __future__ import annotations

import ast
import builtins
import json
import locale
import os
import random
import socket
import subprocess
import sys
import time
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import jsonschema
import pytest

import reference_scorer

PACKAGE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_DIR.parent.parent
FIXTURES_DIR = PACKAGE_DIR / "fixtures"
MODULE_PATH = PACKAGE_DIR / "reference_scorer.py"
PYTHON = sys.executable

FIXTURE_NAMES = sorted(
    path.name[: -len(".input.json")] for path in FIXTURES_DIR.glob("*.input.json")
)


def load_digest_manifest() -> dict:
    return json.loads((FIXTURES_DIR / "digests.json").read_text(encoding="utf-8"))


def make_input(scorer: str, cases: list[dict]) -> bytes:
    document = {
        "schema_id": "bmas.reference_scorer_input",
        "metadata": {"contract_version": "1.0.0"},
        "scorer": scorer,
        "cases": cases,
    }
    return json.dumps(document).encode("utf-8")


def exact_case(case_id: str = "one", expected: str = "a", actual: str = "a") -> dict:
    return {"case_id": case_id, "expected": expected, "actual": actual}


def numeric_case(case_id: str = "one", **overrides) -> dict:
    case = {"case_id": case_id, "expected": 1, "actual": 1, "tolerance": 1}
    case.update(overrides)
    return case


# ── Frozen fixtures ────────────────────────────────────────────────


def test_fixture_set_covers_both_scorers() -> None:
    assert len(FIXTURE_NAMES) >= 6
    scorers = set()
    for name in FIXTURE_NAMES:
        document = json.loads((FIXTURES_DIR / f"{name}.input.json").read_bytes())
        scorers.add(document["scorer"])
    assert scorers == {"exact_match", "bounded_numeric"}


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_output_bytes_are_frozen(name: str) -> None:
    input_bytes = (FIXTURES_DIR / f"{name}.input.json").read_bytes()
    expected_bytes = (FIXTURES_DIR / f"{name}.expected.json").read_bytes()
    assert reference_scorer.score_bytes(input_bytes) == expected_bytes


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_digest_manifest_is_frozen(name: str) -> None:
    manifest = load_digest_manifest()
    entry = manifest["fixtures"][name]
    input_bytes = (FIXTURES_DIR / f"{name}.input.json").read_bytes()
    expected_bytes = (FIXTURES_DIR / f"{name}.expected.json").read_bytes()
    assert sha256(input_bytes).hexdigest() == entry["input_sha256"]
    assert sha256(expected_bytes).hexdigest() == entry["expected_sha256"]
    result = json.loads(expected_bytes)
    assert result["input_sha256"] == entry["normalized_input_sha256"]


def test_digest_manifest_lists_every_fixture_pair() -> None:
    manifest = load_digest_manifest()
    assert sorted(manifest["fixtures"]) == FIXTURE_NAMES
    assert manifest["metadata"]["contract_version"] == "1.0.0"


# ── Determinism and normalization ──────────────────────────────────


def test_equal_input_bytes_produce_equal_result_digests() -> None:
    input_bytes = make_input("exact_match", [exact_case()])
    first_run = reference_scorer.score_bytes(input_bytes)
    second_run = reference_scorer.score_bytes(input_bytes)
    assert sha256(first_run).hexdigest() == sha256(second_run).hexdigest()


def test_normalized_input_variants_share_one_output() -> None:
    compact = make_input(
        "bounded_numeric", [numeric_case(expected=3, actual=2.5, tolerance=1)]
    )
    document = json.loads(compact)
    airy = json.dumps(document, indent=4, sort_keys=True).encode("utf-8")
    variant = json.dumps(
        {key: document[key] for key in ("cases", "scorer", "metadata", "schema_id")}
    ).encode("utf-8")
    reordered_case = json.dumps(
        {
            **document,
            "cases": [{"tolerance": 1, "actual": 2.5, "expected": 3, "case_id": "one"}],
        }
    ).encode("utf-8")
    equal_value_forms = compact.replace(b"2.5", b"2.50").replace(b'"expected": 3', b'"expected": 3.0')
    outputs = {
        reference_scorer.score_bytes(candidate)
        for candidate in (compact, airy, variant, reordered_case, equal_value_forms)
    }
    assert len(outputs) == 1


def test_output_is_stable_under_hash_randomization() -> None:
    input_path = FIXTURES_DIR / "exact-match-mixed.input.json"
    expected = (FIXTURES_DIR / "exact-match-mixed.expected.json").read_bytes()
    for seed in ("0", "1", "424242"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        outcome = subprocess.run(
            [PYTHON, str(MODULE_PATH), str(input_path)],
            capture_output=True,
            env=environment,
        )
        assert outcome.returncode == 0, outcome.stderr
        assert outcome.stdout == expected


def test_output_is_locale_independent() -> None:
    input_bytes = make_input(
        "bounded_numeric", [numeric_case(expected=1000.5, actual=999, tolerance=10)]
    )
    baseline = reference_scorer.score_bytes(input_bytes)
    saved = locale.setlocale(locale.LC_ALL)
    try:
        for name in ("C", "en_US.UTF-8", "de_DE.UTF-8"):
            try:
                locale.setlocale(locale.LC_ALL, name)
            except locale.Error:
                continue
            assert reference_scorer.score_bytes(input_bytes) == baseline
    finally:
        locale.setlocale(locale.LC_ALL, saved)


def test_decimal_arithmetic_avoids_binary_float_drift() -> None:
    input_bytes = make_input(
        "bounded_numeric", [numeric_case(expected=0.3, actual=0.1, tolerance=0.2)]
    )
    result = json.loads(reference_scorer.score_bytes(input_bytes))
    assert result["case_results"][0]["score"] == "0.000000"


def test_aggregate_recomputes_from_case_scores() -> None:
    for name in FIXTURE_NAMES:
        result = json.loads((FIXTURES_DIR / f"{name}.expected.json").read_bytes())
        scores = [Decimal(case["score"]) for case in result["case_results"]]
        aggregate = result["aggregate"]
        assert aggregate["case_count"] == len(scores)
        assert Decimal(aggregate["score_sum"]) == sum(scores)


# ── Output contract ────────────────────────────────────────────────


def result_schema() -> dict:
    return json.loads((PACKAGE_DIR / "result.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_output_matches_the_result_schema(name: str) -> None:
    result = json.loads((FIXTURES_DIR / f"{name}.expected.json").read_bytes())
    jsonschema.Draft202012Validator(result_schema()).validate(result)


def test_result_schema_identifier_and_contract_version() -> None:
    schema = result_schema()
    assert schema["schema_id"] == "bmas.reference_scorer_result"
    assert schema["metadata"]["contract_version"] == "1.0.0"


def test_native_output_validation_agrees_with_the_schema() -> None:
    result = json.loads(
        reference_scorer.score_bytes(make_input("exact_match", [exact_case()]))
    )
    reference_scorer.validate_result_document(result)
    validator = jsonschema.Draft202012Validator(result_schema())
    tampering = [
        lambda r: r.pop("aggregate"),
        lambda r: r.update(unexpected_extra_field=1),
        lambda r: r.update(scorer="surprise"),
        lambda r: r.update(input_sha256="short"),
        lambda r: r["case_results"][0].update(score="2.000000"),
        lambda r: r["case_results"][0].update(score="0.5"),
        lambda r: r["aggregate"].update(case_count=0),
        lambda r: r["metadata"].update(contract_version="9.9.9"),
    ]
    for tamper in tampering:
        broken = json.loads(json.dumps(result))
        tamper(broken)
        with pytest.raises(reference_scorer.InvalidOutputError):
            reference_scorer.validate_result_document(broken)
        assert not validator.is_valid(broken)


def test_scorer_result_fails_the_runner_result_schema() -> None:
    runner_schema = json.loads(
        (REPO_ROOT / "schemas" / "test-manifest-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    result = json.loads(
        reference_scorer.score_bytes(make_input("exact_match", [exact_case()]))
    )
    assert not jsonschema.Draft202012Validator(runner_schema).is_valid(result)


# ── Failure codes ──────────────────────────────────────────────────


def expect_failure(input_bytes: bytes, error_type: type) -> None:
    with pytest.raises(error_type):
        reference_scorer.score_bytes(input_bytes)


def test_non_finite_numbers_are_rejected() -> None:
    for literal in (b"Infinity", b"-Infinity", b"NaN"):
        broken = make_input("bounded_numeric", [numeric_case()]).replace(
            b'"actual": 1', b'"actual": ' + literal
        )
        expect_failure(broken, reference_scorer.InvalidInputError)


def test_invalid_inputs_use_the_invalid_input_code() -> None:
    invalid_inputs = [
        b"not json at all",
        b'"a bare string"',
        b'{"schema_id": "bmas.reference_scorer_input", "schema_id": "twice"}',
        make_input("exact_match", [exact_case()]).replace(
            b"bmas.reference_scorer_input", b"bmas.other_contract"
        ),
        make_input("exact_match", [exact_case()]).replace(b'"1.0.0"', b'"2.0.0"'),
        make_input("surprise_scorer", [exact_case()]),
        make_input("exact_match", []),
        make_input("exact_match", [{"case_id": "one", "expected": "a"}]),
        make_input("exact_match", [{**exact_case(), "unexpected_extra_field": 1}]),
        make_input("exact_match", [exact_case("dup"), exact_case("dup")]),
        make_input("exact_match", [exact_case(case_id="")]),
        make_input("exact_match", [{"case_id": "one", "expected": 3, "actual": "3"}]),
        make_input("bounded_numeric", [numeric_case(expected="3")]),
        make_input("bounded_numeric", [numeric_case(actual=True)]),
        make_input("bounded_numeric", [numeric_case(tolerance=0)]),
        make_input("bounded_numeric", [numeric_case(tolerance=-1)]),
        '{"schema_id": "bmas.reference_scorer_input"}'.encode("utf-16"),
    ]
    for input_bytes in invalid_inputs:
        expect_failure(input_bytes, reference_scorer.InvalidInputError)


def test_resource_limits_use_the_resource_limit_code() -> None:
    oversized_document = make_input(
        "exact_match", [exact_case(expected="x" * (reference_scorer.MAX_INPUT_BYTES))]
    )
    long_text = make_input(
        "exact_match",
        [exact_case(expected="x" * (reference_scorer.MAX_TEXT_CHARS + 1), actual="x")],
    )
    long_case_id = make_input(
        "exact_match", [exact_case(case_id="c" * (reference_scorer.MAX_CASE_ID_CHARS + 1))]
    )
    many_cases = make_input(
        "exact_match",
        [exact_case(case_id=f"case-{i}") for i in range(reference_scorer.MAX_CASES + 1)],
    )
    wide_number = make_input("bounded_numeric", [numeric_case()]).replace(
        b'"actual": 1', b'"actual": 1.' + b"1" * 40
    )
    huge_exponent = make_input("bounded_numeric", [numeric_case()]).replace(
        b'"actual": 1', b'"actual": 1e60'
    )
    for input_bytes in (
        oversized_document,
        long_text,
        long_case_id,
        many_cases,
        wide_number,
        huge_exponent,
    ):
        expect_failure(input_bytes, reference_scorer.ResourceLimitError)


def test_failure_codes_stay_pinned() -> None:
    assert reference_scorer.EXIT_SUCCESS == 0
    assert reference_scorer.EXIT_INVALID_INPUT == 2
    assert reference_scorer.EXIT_INVALID_OUTPUT == 3
    assert reference_scorer.EXIT_RESOURCE_LIMIT == 4
    assert reference_scorer.InvalidInputError("x").exit_code == 2
    assert reference_scorer.InvalidOutputError("x").exit_code == 3
    assert reference_scorer.ResourceLimitError("x").exit_code == 4


# ── Isolation ──────────────────────────────────────────────────────

PROHIBITED_IMPORTS = {
    "os",
    "io",
    "socket",
    "ssl",
    "http",
    "urllib",
    "requests",
    "httpx",
    "subprocess",
    "multiprocessing",
    "threading",
    "asyncio",
    "sqlite3",
    "aiosqlite",
    "redis",
    "time",
    "datetime",
    "random",
    "secrets",
    "uuid",
    "locale",
    "pathlib",
    "tempfile",
    "shutil",
    "ctypes",
    "importlib",
}

ALLOWED_IMPORTS = {"__future__", "json", "sys", "decimal", "hashlib"}


def module_imports() -> set[str]:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            names.add(node.module.split(".")[0])
    return names


def test_module_imports_stay_pinned_to_the_standard_library() -> None:
    imports = module_imports()
    assert imports <= ALLOWED_IMPORTS
    assert not imports & PROHIBITED_IMPORTS


def test_scoring_runs_with_every_prohibited_capability_blocked(monkeypatch) -> None:
    def forbid(capability: str):
        def guard(*_args, **_kwargs):
            raise AssertionError(f"the scorer used the prohibited capability: {capability}")

        return guard

    monkeypatch.setattr(socket, "socket", forbid("network socket"))
    monkeypatch.setattr(socket, "create_connection", forbid("network connection"))
    monkeypatch.setattr(time, "time", forbid("wall clock"))
    monkeypatch.setattr(time, "time_ns", forbid("wall clock"))
    monkeypatch.setattr(time, "monotonic", forbid("clock"))
    monkeypatch.setattr(time, "perf_counter", forbid("clock"))
    monkeypatch.setattr(random, "random", forbid("unseeded randomness"))
    monkeypatch.setattr(os, "urandom", forbid("entropy source"))
    monkeypatch.setattr(os, "environb", {}, raising=False)
    monkeypatch.setattr(subprocess, "Popen", forbid("child process"))
    monkeypatch.setattr(os, "system", forbid("child process"))
    monkeypatch.setattr(builtins, "open", forbid("filesystem access"))
    monkeypatch.setattr(os, "getenv", forbid("environment access"))
    monkeypatch.setattr(
        os.environ.__class__, "__getitem__", forbid("environment access")
    )

    input_bytes = make_input(
        "bounded_numeric",
        [numeric_case(case_id=f"case-{i}", actual=1.25) for i in range(50)],
    )
    result = json.loads(reference_scorer.score_bytes(input_bytes))
    assert result["aggregate"]["case_count"] == 50


def test_command_writes_no_file(tmp_path: Path) -> None:
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()
    input_path = FIXTURES_DIR / "bounded-numeric-exact.input.json"
    expected = (FIXTURES_DIR / "bounded-numeric-exact.expected.json").read_bytes()
    outcome = subprocess.run(
        [PYTHON, str(MODULE_PATH), str(input_path)],
        capture_output=True,
        cwd=workspace,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.stdout == expected
    assert outcome.stderr == b""
    assert list(workspace.iterdir()) == []


def test_command_reads_stdin_and_maps_failure_codes(tmp_path: Path) -> None:
    good = make_input("exact_match", [exact_case()])
    outcome = subprocess.run(
        [PYTHON, str(MODULE_PATH)], input=good, capture_output=True
    )
    assert outcome.returncode == 0
    assert json.loads(outcome.stdout)["schema_id"] == "bmas.reference_scorer_result"

    invalid = subprocess.run(
        [PYTHON, str(MODULE_PATH)], input=b"not json", capture_output=True
    )
    assert invalid.returncode == reference_scorer.EXIT_INVALID_INPUT
    assert invalid.stdout == b""
    assert invalid.stderr.startswith(b"error 2: ")

    oversized_path = tmp_path / "oversized.input.json"
    oversized_path.write_bytes(b"x" * (reference_scorer.MAX_INPUT_BYTES + 1))
    oversized = subprocess.run(
        [PYTHON, str(MODULE_PATH), str(oversized_path)], capture_output=True
    )
    assert oversized.returncode == reference_scorer.EXIT_RESOURCE_LIMIT
    assert oversized.stderr.startswith(b"error 4: ")
