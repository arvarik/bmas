"""Tests for the authoritative test manifest runner, validators, and naming check.

The suite proves the pre-Foundation package contract:

1. Valid minimal and mixed records pass.
2. Every missing required field, unknown field, and duplicate key fails.
3. Changed commands, count mismatches, and digest mismatches fail.
4. Interrupted writes never leave a readable final record.
5. Attempt indexes stay contiguous and ordered, and a passing retry
   never hides a failed first attempt.
6. Local and continuous integration consumers resolve the same
   required groups and produce equal records outside host and time fields.
7. Reserved entries hold no command and never execute.
8. The naming check accepts metadata versions and rejects versioned
   source names.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

import manifestlib

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
PYTHON = sys.executable


# ── Fixture repository construction ────────────────────────────────


def build_repository(base: Path, manifest_text: str) -> Path:
    """Create one temporary git repository with the schemas and a manifest."""
    repo = base
    repo.mkdir(parents=True, exist_ok=True)
    schemas = repo / "schemas"
    schemas.mkdir(exist_ok=True)
    for name in ("test-manifest.schema.json", "test-manifest-result.schema.json"):
        (schemas / name).write_bytes((REPO_ROOT / "schemas" / name).read_bytes())
    (repo / "test-manifest.yaml").write_text(manifest_text, encoding="utf-8")
    (repo / ".gitignore").write_text(
        "test-results/\nlocal-consumer/\nci-consumer/\nmarker-file\n", encoding="utf-8"
    )

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.email", "manifest-tests@example.invalid")
    git("config", "user.name", "Manifest Tests")
    git("add", "-A")
    git("commit", "--quiet", "-m", "fixture")
    return repo


def manifest_header() -> str:
    return (
        "schema_id: bmas.test_manifest\n"
        "metadata:\n"
        '  contract_version: "1.0.0"\n'
    )


def simple_group(
    group_id: str,
    program: str,
    *,
    state: str = "active_required",
    timeout: float = 60,
    extra: str = "",
) -> str:
    return (
        f"  - id: {group_id}\n"
        f"    state: {state}\n"
        "    owner: fixtures\n"
        "    purpose: Execute one deterministic fixture command.\n"
        f"    argv: [{json.dumps(PYTHON)}, \"-c\", {json.dumps(program)}]\n"
        '    working_directory: "."\n'
        f"    timeout_seconds: {timeout}\n"
        "    tools: []\n"
        f"{extra}"
    )


def complete_profile() -> str:
    return (
        "profiles:\n"
        "  - id: complete\n"
        "    description: Every required fixture group.\n"
        "    selector:\n"
        "      states: [active_required]\n"
    )


def run_runner(repo: Path, profile: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            PYTHON,
            str(SCRIPTS_DIR / "run-test-manifest.py"),
            "--profile",
            profile,
            "--repo-root",
            str(repo),
            "--results-dir",
            "test-results",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def read_record(repo: Path) -> dict:
    records = sorted((repo / "test-results").glob("*/test-manifest-result.json"))
    assert records, "the runner wrote no final record"
    return manifestlib.load_json_text(records[-1].read_text(encoding="utf-8"))


def validate(record: dict, repo: Path) -> list[str]:
    manifest, manifest_bytes = manifestlib.load_manifest(repo, repo / "test-manifest.yaml")
    schema = manifestlib.load_schema(repo, manifestlib.RESULT_SCHEMA_PATH)
    return manifestlib.validate_result(record, manifest, manifest_bytes, schema)


# ── Shared fixture records ─────────────────────────────────────────


@pytest.fixture(scope="session")
def mixed_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A run with one passing, one failing, and one dependency-skipped group."""
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group(
            "fixture.pass", "print('2 passed')", extra="    parser: pytest_summary\n"
        )
        + simple_group("fixture.fail", "import sys; sys.exit(1)")
        + simple_group(
            "fixture.dependent",
            "print('never runs')",
            extra="    depends_on: [fixture.fail]\n",
        )
        + complete_profile()
    )
    repo = build_repository(tmp_path_factory.mktemp("mixed"), manifest_text)
    (repo / "untracked-note.txt").write_text("untracked fixture file\n", encoding="utf-8")
    outcome = run_runner(repo, "complete")
    assert outcome.returncode == 1, outcome.stderr
    return repo


@pytest.fixture(scope="session")
def mixed_record(mixed_repo: Path) -> dict:
    return read_record(mixed_repo)


@pytest.fixture(scope="session")
def retry_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A run whose only group fails first and passes on the retry."""
    flaky = (
        "import pathlib, sys\n"
        "marker = pathlib.Path('marker-file')\n"
        "if marker.exists():\n"
        "    print('1 passed')\n"
        "else:\n"
        "    marker.write_text('present')\n"
        "    print('1 failed')\n"
        "    sys.exit(1)\n"
    )
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group(
            "fixture.flaky",
            flaky,
            extra="    retry:\n      max_attempts: 2\n",
        )
        + complete_profile()
    )
    repo = build_repository(tmp_path_factory.mktemp("retry"), manifest_text)
    outcome = run_runner(repo, "complete")
    assert outcome.returncode == 0, outcome.stderr
    return repo


@pytest.fixture(scope="session")
def retry_record(retry_repo: Path) -> dict:
    return read_record(retry_repo)


# ── Valid records ──────────────────────────────────────────────────


def test_minimal_clean_record_is_valid(tmp_path: Path) -> None:
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group("fixture.pass", "print('1 passed')")
        + complete_profile()
    )
    repo = build_repository(tmp_path / "clean", manifest_text)
    outcome = run_runner(repo, "complete")
    assert outcome.returncode == 0, outcome.stderr
    record = read_record(repo)
    assert validate(record, repo) == []
    assert record["schema_id"] == "bmas.test_manifest_result"
    assert record["state"] == "passed"
    assert record["repository"]["dirty"] is False
    assert record["repository"]["branch"] == "main"
    empty_digest = manifestlib.sha256_hex(b"")
    assert record["repository"]["tracked_diff_sha256"] == empty_digest
    assert record["repository"]["untracked_files"] == []


def test_mixed_record_is_valid_and_complete(mixed_record: dict, mixed_repo: Path) -> None:
    assert validate(mixed_record, mixed_repo) == []
    states = {g["group_id"]: g["state"] for g in mixed_record["groups"]}
    assert states == {
        "fixture.pass": "passed",
        "fixture.fail": "failed",
        "fixture.dependent": "skipped",
    }
    assert mixed_record["state"] == "failed"
    skipped = next(g for g in mixed_record["groups"] if g["state"] == "skipped")
    assert skipped["attempts"] == []
    assert "fixture.fail" in skipped["skip_reason"]
    passing = next(g for g in mixed_record["groups"] if g["state"] == "passed")
    assert passing["attempts"][0]["counts"] == {"passed": 2, "failed": 0, "skipped": 0}
    log_paths = {log["path"] for log in passing["attempts"][0]["logs"]}
    assert log_paths == {
        "groups/fixture.pass/attempt-0/stdout.log",
        "groups/fixture.pass/attempt-0/stderr.log",
    }


def test_record_captures_untracked_and_dirty_state(mixed_record: dict) -> None:
    repository = mixed_record["repository"]
    assert repository["dirty"] is True
    paths = [entry["path"] for entry in repository["untracked_files"]]
    assert "untracked-note.txt" in paths
    assert paths == sorted(paths, key=lambda p: p.encode("utf-8"))
    for entry in repository["untracked_files"]:
        assert manifestlib.SHA256_PATTERN.match(entry["sha256"])


def test_detached_head_records_null_branch(tmp_path: Path) -> None:
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group("fixture.pass", "print('1 passed')")
        + complete_profile()
    )
    repo = build_repository(tmp_path / "detached", manifest_text)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", "--detach"],
        check=True,
        capture_output=True,
    )
    outcome = run_runner(repo, "complete")
    assert outcome.returncode == 0, outcome.stderr
    record = read_record(repo)
    assert record["repository"]["branch"] is None
    assert validate(record, repo) == []


def test_timeout_produces_timed_out_state(tmp_path: Path) -> None:
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group("fixture.slow", "import time; time.sleep(30)", timeout=1)
        + complete_profile()
    )
    repo = build_repository(tmp_path / "slow", manifest_text)
    outcome = run_runner(repo, "complete")
    assert outcome.returncode == 1
    record = read_record(repo)
    group = record["groups"][0]
    assert group["state"] == "timed_out"
    assert group["attempts"][0]["exit_code"] is None
    assert record["state"] == "timed_out"
    assert validate(record, repo) == []


# ── Missing fields, unknown fields, duplicate keys ─────────────────


def _resolve_reference(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        reference = node["$ref"]
        assert reference.startswith("#/$defs/")
        node = schema["$defs"][reference.split("/")[-1]]
    return node


def _walk_instance(schema: dict, node_schema: dict, instance, path):
    node_schema = _resolve_reference(schema, node_schema)
    if isinstance(instance, dict):
        yield path, node_schema, instance
        properties = node_schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                yield from _walk_instance(schema, properties[key], value, path + [key])
    elif isinstance(instance, list):
        item_schema = node_schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                yield from _walk_instance(schema, item_schema, item, path + [index])


def _delete_at(record: dict, path: list, key: str) -> dict:
    mutated = copy.deepcopy(record)
    node = mutated
    for part in path:
        node = node[part]
    del node[key]
    return mutated


def _insert_at(record: dict, path: list, key: str, value) -> dict:
    mutated = copy.deepcopy(record)
    node = mutated
    for part in path:
        node = node[part]
    node[key] = value
    return mutated


def test_every_missing_required_field_fails(mixed_record: dict, mixed_repo: Path) -> None:
    schema = manifestlib.load_schema(REPO_ROOT, manifestlib.RESULT_SCHEMA_PATH)
    checked = 0
    for path, node_schema, instance in _walk_instance(schema, schema, mixed_record, []):
        for key in node_schema.get("required", []):
            assert key in instance, f"{path}: fixture record lacks required field {key}"
            mutated = _delete_at(mixed_record, path, key)
            assert validate(mutated, mixed_repo), f"missing {path + [key]} passed validation"
            checked += 1
    assert checked > 60


def test_every_unknown_field_fails(mixed_record: dict, mixed_repo: Path) -> None:
    schema = manifestlib.load_schema(REPO_ROOT, manifestlib.RESULT_SCHEMA_PATH)
    checked = 0
    for path, node_schema, _instance in _walk_instance(schema, schema, mixed_record, []):
        if node_schema.get("additionalProperties") is False:
            mutated = _insert_at(mixed_record, path, "unexpected_extra_field", 1)
            assert validate(mutated, mixed_repo), f"unknown field at {path} passed validation"
            checked += 1
    assert checked > 10


def test_duplicate_json_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate object key"):
        manifestlib.load_json_text('{"state": "passed", "state": "failed"}')


def test_duplicate_manifest_keys_are_rejected(tmp_path: Path) -> None:
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group("fixture.pass", "print('1 passed')")
        + complete_profile()
    )
    repo = build_repository(tmp_path / "dupkeys", manifest_text)
    duplicated = manifest_text.replace(
        "schema_id: bmas.test_manifest\n",
        "schema_id: bmas.test_manifest\nschema_id: bmas.test_manifest\n",
    )
    (repo / "test-manifest.yaml").write_text(duplicated, encoding="utf-8")
    with pytest.raises(manifestlib.ManifestError, match="duplicate mapping key"):
        manifestlib.load_manifest(repo, repo / "test-manifest.yaml")


# ── Manifest cross-check failures ──────────────────────────────────


def mutate(record: dict, apply) -> dict:
    mutated = copy.deepcopy(record)
    apply(mutated)
    return mutated


def test_changed_command_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(mixed_record, lambda r: r["groups"][0]["argv"].append("--extra"))
    assert any("argv" in e for e in validate(mutated, mixed_repo))


def test_changed_working_directory_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(
        mixed_record, lambda r: r["groups"][0].update(working_directory="daemon")
    )
    assert any("working_directory" in e for e in validate(mutated, mixed_repo))


def test_changed_timeout_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(
        mixed_record,
        lambda r: r["groups"][0].update(timeout_nanos=r["groups"][0]["timeout_nanos"] + 1),
    )
    assert any("timeout_nanos" in e for e in validate(mutated, mixed_repo))


def test_changed_entry_digest_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(
        mixed_record,
        lambda r: r["groups"][0].update(manifest_entry_digest="0" * 64),
    )
    assert any("manifest_entry_digest" in e for e in validate(mutated, mixed_repo))


def test_changed_tool_key_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(
        mixed_record,
        lambda r: r["groups"][0]["tool_versions"].update(surprise="0.0"),
    )
    assert any("tool_versions" in e for e in validate(mutated, mixed_repo))


def test_changed_dependency_key_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(
        mixed_record,
        lambda r: r["groups"][0]["dependency_versions"].update(redis="0.0"),
    )
    assert any("dependency_versions" in e for e in validate(mutated, mixed_repo))


def test_manifest_digest_mismatch_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(mixed_record, lambda r: r.update(manifest_digest="0" * 64))
    assert any("manifest_digest" in e for e in validate(mutated, mixed_repo))


def test_missing_group_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(mixed_record, lambda r: r["groups"].pop())
    assert validate(mutated, mixed_repo)


def test_extra_group_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(mixed_record, lambda r: r["groups"].append(r["groups"][0]))
    assert validate(mutated, mixed_repo)


def test_changed_group_order_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(mixed_record, lambda r: r["groups"].reverse())
    assert validate(mutated, mixed_repo)


def test_summary_count_mismatch_fails(mixed_record: dict, mixed_repo: Path) -> None:
    for field in ("passed", "failed", "skipped", "attempt_count", "retry_count"):
        mutated = mutate(
            mixed_record, lambda r, f=field: r["summary"].update({f: r["summary"][f] + 1})
        )
        assert any("summary" in e for e in validate(mutated, mixed_repo)), field


def test_run_state_mismatch_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(mixed_record, lambda r: r.update(state="passed"))
    assert any("run state" in e for e in validate(mutated, mixed_repo))


# ── Field-format rejections ────────────────────────────────────────


def test_unknown_state_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(mixed_record, lambda r: r["groups"][0].update(state="exploded"))
    assert validate(mutated, mixed_repo)


def test_negative_count_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(
        mixed_record, lambda r: r["groups"][0]["attempts"][0]["counts"].update(passed=-1)
    )
    assert validate(mutated, mixed_repo)


def test_malformed_timestamp_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(mixed_record, lambda r: r.update(started_at_utc="2026-08-30 12:00:00"))
    assert validate(mutated, mixed_repo)


def test_reversed_timestamps_fail(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(
        mixed_record,
        lambda r: r.update(
            started_at_utc="2999-01-01T00:00:00.000000000Z",
        ),
    )
    assert any("before" in e for e in validate(mutated, mixed_repo))


def test_malformed_digest_fails(mixed_record: dict, mixed_repo: Path) -> None:
    mutated = mutate(
        mixed_record,
        lambda r: r["groups"][0]["attempts"][0].update(stdout_sha256="not-a-digest"),
    )
    assert validate(mutated, mixed_repo)


def test_unsafe_paths_fail(mixed_record: dict, mixed_repo: Path) -> None:
    for unsafe in ("/etc/passwd", "../outside.log", "logs/../../outside.log"):
        mutated = mutate(
            mixed_record,
            lambda r, p=unsafe: r["groups"][0]["attempts"][0]["logs"][0].update(path=p),
        )
        assert validate(mutated, mixed_repo), unsafe


def test_skipped_group_without_reason_fails(mixed_record: dict, mixed_repo: Path) -> None:
    skipped_index = next(
        i for i, g in enumerate(mixed_record["groups"]) if g["state"] == "skipped"
    )
    mutated = mutate(
        mixed_record, lambda r: r["groups"][skipped_index].update(skip_reason=None)
    )
    assert validate(mutated, mixed_repo)


def test_non_skipped_group_without_attempts_fails(
    mixed_record: dict, mixed_repo: Path
) -> None:
    mutated = mutate(
        mixed_record, lambda r: r["groups"][0].update(attempts=[], skip_reason=None)
    )
    assert validate(mutated, mixed_repo)


# ── Attempt ordering and preserved failures ────────────────────────


def test_retry_preserves_failed_first_attempt(retry_record: dict, retry_repo: Path) -> None:
    assert validate(retry_record, retry_repo) == []
    group = retry_record["groups"][0]
    assert group["state"] == "passed"
    assert [a["attempt_index"] for a in group["attempts"]] == [0, 1]
    first_attempt, second_attempt = group["attempts"]
    assert first_attempt["state"] == "failed"
    assert first_attempt["exit_code"] == 1
    assert first_attempt["retry_reason"] is None
    assert first_attempt["logs"], "the failed attempt keeps its logs"
    assert second_attempt["state"] == "passed"
    assert "attempt 0 ended failed" in second_attempt["retry_reason"]
    assert retry_record["summary"]["retry_count"] == 1
    assert retry_record["summary"]["attempt_count"] == 2


def test_hidden_first_failure_fails_validation(
    retry_record: dict, retry_repo: Path
) -> None:
    mutated = mutate(
        retry_record,
        lambda r: r["groups"][0]["attempts"][0].update(state="passed", exit_code=0),
    )
    assert any("hide" in e or "passed attempt" in e for e in validate(mutated, retry_repo))


def test_dropping_failed_first_attempt_fails_validation(
    retry_record: dict, retry_repo: Path
) -> None:
    mutated = mutate(retry_record, lambda r: r["groups"][0]["attempts"].pop(0))
    assert validate(mutated, retry_repo)


def test_out_of_order_attempts_fail(retry_record: dict, retry_repo: Path) -> None:
    mutated = mutate(retry_record, lambda r: r["groups"][0]["attempts"].reverse())
    assert any("contiguous" in e or "attempt" in e for e in validate(mutated, retry_repo))


def test_duplicate_attempt_index_fails(retry_record: dict, retry_repo: Path) -> None:
    mutated = mutate(
        retry_record,
        lambda r: r["groups"][0]["attempts"][1].update(attempt_index=0),
    )
    assert validate(mutated, retry_repo)


def test_attempt_index_gap_fails(retry_record: dict, retry_repo: Path) -> None:
    mutated = mutate(
        retry_record,
        lambda r: r["groups"][0]["attempts"][1].update(attempt_index=2),
    )
    assert validate(mutated, retry_repo)


# ── Interrupted writes ─────────────────────────────────────────────


def test_invalid_record_never_becomes_final(retry_repo: Path, tmp_path: Path) -> None:
    manifest, manifest_bytes = manifestlib.load_manifest(
        retry_repo, retry_repo / "test-manifest.yaml"
    )
    schema = manifestlib.load_schema(retry_repo, manifestlib.RESULT_SCHEMA_PATH)
    result_dir = tmp_path / "interrupted"
    result_dir.mkdir()
    with pytest.raises(manifestlib.ResultValidationError):
        manifestlib.write_final_record(
            {"schema_id": "bmas.test_manifest_result"},
            result_dir,
            manifest,
            manifest_bytes,
            schema,
            retry_repo,
        )
    assert not (result_dir / manifestlib.RESULT_FILE_NAME).exists()
    assert not list(result_dir.glob("*.tmp"))


def test_final_record_write_is_atomic(retry_repo: Path, retry_record: dict) -> None:
    run_dir = sorted((retry_repo / "test-results").glob("*"))[-1]
    assert not list(run_dir.glob("*.tmp"))
    manifest, manifest_bytes = manifestlib.load_manifest(
        retry_repo, retry_repo / "test-manifest.yaml"
    )
    schema = manifestlib.load_schema(retry_repo, manifestlib.RESULT_SCHEMA_PATH)
    rewritten = manifestlib.write_final_record(
        retry_record, run_dir, manifest, manifest_bytes, schema, retry_repo
    )
    assert rewritten == run_dir / manifestlib.RESULT_FILE_NAME
    assert not list(run_dir.glob("*.tmp"))


def test_log_digest_mismatch_fails_file_verification(
    retry_repo: Path, retry_record: dict
) -> None:
    run_dir = sorted((retry_repo / "test-results").glob("*"))[-1]
    manifest, manifest_bytes = manifestlib.load_manifest(
        retry_repo, retry_repo / "test-manifest.yaml"
    )
    schema = manifestlib.load_schema(retry_repo, manifestlib.RESULT_SCHEMA_PATH)
    mutated = mutate(
        retry_record,
        lambda r: r["groups"][0]["attempts"][0]["logs"][0].update(sha256="1" * 64),
    )
    errors = manifestlib.validate_result(
        mutated,
        manifest,
        manifest_bytes,
        schema,
        repo_root=retry_repo,
        result_dir=run_dir,
        verify_files=True,
    )
    assert any("log digest mismatch" in e for e in errors)


# ── Equal local and continuous integration consumers ───────────────


VOLATILE_TOP_FIELDS = ("run_id", "started_at_utc", "ended_at_utc", "duration_nanos", "host")


def _stable_view(record: dict) -> dict:
    view = copy.deepcopy(record)
    for field in VOLATILE_TOP_FIELDS:
        view.pop(field)
    for group in view["groups"]:
        for field in ("started_at_utc", "ended_at_utc", "duration_nanos"):
            group.pop(field)
        for attempt in group["attempts"]:
            for field in ("started_at_utc", "ended_at_utc", "duration_nanos"):
                attempt.pop(field)
    return view


def test_local_and_ci_consumers_produce_equal_records(tmp_path: Path) -> None:
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group("fixture.pass", "print('3 passed')")
        + complete_profile()
    )
    repo = build_repository(tmp_path / "parity", manifest_text)
    first_outcome = run_runner(repo, "complete", "--results-dir", "local-consumer")
    second_outcome = run_runner(repo, "complete", "--results-dir", "ci-consumer")
    assert first_outcome.returncode == 0, first_outcome.stderr
    assert second_outcome.returncode == 0, second_outcome.stderr
    records = []
    for consumer in ("local-consumer", "ci-consumer"):
        paths = sorted((repo / consumer).glob("*/test-manifest-result.json"))
        records.append(manifestlib.load_json_text(paths[-1].read_text(encoding="utf-8")))
    assert _stable_view(records[0]) == _stable_view(records[1])


def test_repository_manifest_consumers_agree() -> None:
    outcome = subprocess.run(
        [PYTHON, str(SCRIPTS_DIR / "validate-test-manifest.py")],
        capture_output=True,
        text=True,
    )
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr
    assert "Playwright" in outcome.stdout


def test_repository_complete_profile_includes_playwright() -> None:
    manifest, _ = manifestlib.load_manifest(REPO_ROOT, REPO_ROOT / "test-manifest.yaml")
    complete = manifestlib.resolve_profile(manifest, "complete")
    tools = {tool for group in complete for tool in group.get("tools", [])}
    assert "playwright" in tools
    identifiers = [group["id"] for group in complete]
    assert "mission-control.e2e" in identifiers
    assert "mission-control.browser-install" in identifiers
    assert "repo.source-naming" in identifiers


def test_new_required_group_fails_until_every_consumer_includes_it(
    tmp_path: Path,
) -> None:
    manifest_path = REPO_ROOT / "test-manifest.yaml"
    tampered = manifest_path.read_text(encoding="utf-8").replace(
        "profiles:\n",
        simple_group("fixture.orphan", "print('orphan')") + "\nprofiles:\n",
        1,
    )
    tampered_path = tmp_path / "tampered-manifest.yaml"
    tampered_path.write_text(tampered, encoding="utf-8")
    outcome = subprocess.run(
        [
            PYTHON,
            str(SCRIPTS_DIR / "validate-test-manifest.py"),
            "--manifest",
            str(tampered_path),
        ],
        capture_output=True,
        text=True,
    )
    assert outcome.returncode == 1
    assert "fixture.orphan" in outcome.stderr


def test_workflow_missing_a_partition_fails(tmp_path: Path) -> None:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    reduced = workflow_path.read_text(encoding="utf-8").replace(
        "run: python3 scripts/run-test-manifest.py --profile ci.agent\n",
        "run: echo skipped\n",
    )
    reduced_path = tmp_path / "reduced-ci.yml"
    reduced_path.write_text(reduced, encoding="utf-8")
    outcome = subprocess.run(
        [
            PYTHON,
            str(SCRIPTS_DIR / "validate-test-manifest.py"),
            "--ci-workflow",
            str(reduced_path),
        ],
        capture_output=True,
        text=True,
    )
    assert outcome.returncode == 1
    assert "ci.agent" in outcome.stderr


# ── Reserved lifecycle ─────────────────────────────────────────────


def test_reserved_entry_with_command_is_rejected(tmp_path: Path) -> None:
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group("fixture.pass", "print('1 passed')")
        + simple_group("fixture.future", "print('reserved')", state="reserved")
        + complete_profile()
    )
    repo = build_repository(tmp_path / "reserved-command", manifest_text)
    with pytest.raises(manifestlib.ManifestError):
        manifestlib.load_manifest(repo, repo / "test-manifest.yaml")


def test_reserved_entry_cannot_enter_a_required_profile(tmp_path: Path) -> None:
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group("fixture.pass", "print('1 passed')")
        + "  - id: fixture.future\n"
        "    state: reserved\n"
        "    owner: fixtures\n"
        "    purpose: Hold one future identifier.\n"
        "    activation_stage: Later delivery stage\n"
        + complete_profile()
        + "  - id: targeted\n"
        "    description: Attempt to execute a reserved entry.\n"
        "    selector:\n"
        "      groups: [fixture.future]\n"
    )
    repo = build_repository(tmp_path / "reserved-profile", manifest_text)
    with pytest.raises(manifestlib.ManifestError, match="non-executable"):
        manifestlib.load_manifest(repo, repo / "test-manifest.yaml")


def test_runner_never_executes_reserved_entries(tmp_path: Path) -> None:
    manifest_text = (
        manifest_header()
        + "groups:\n"
        + simple_group("fixture.pass", "print('1 passed')")
        + "  - id: fixture.future\n"
        "    state: reserved\n"
        "    owner: fixtures\n"
        "    purpose: Hold one future identifier.\n"
        "    activation_stage: Later delivery stage\n"
        + complete_profile()
    )
    repo = build_repository(tmp_path / "reserved-skip", manifest_text)
    outcome = run_runner(repo, "complete")
    assert outcome.returncode == 0, outcome.stderr
    record = read_record(repo)
    assert record["resolved_group_ids"] == ["fixture.pass"]


def test_repository_reserved_learning_entries_have_no_command() -> None:
    manifest, _ = manifestlib.load_manifest(REPO_ROOT, REPO_ROOT / "test-manifest.yaml")
    reserved = [g for g in manifest["groups"] if g["state"] == "reserved"]
    assert len(reserved) >= 16
    for group in reserved:
        assert group["id"].startswith("learning.")
        assert "argv" not in group
        assert "activation_stage" in group


# ── Schema metadata ────────────────────────────────────────────────


def test_result_schema_identifier_and_contract_version() -> None:
    schema = manifestlib.load_schema(REPO_ROOT, manifestlib.RESULT_SCHEMA_PATH)
    assert schema["schema_id"] == "bmas.test_manifest_result"
    assert schema["metadata"]["contract_version"] == "1.0.0"


def test_manifest_schema_identifier_and_contract_version() -> None:
    schema = manifestlib.load_schema(REPO_ROOT, manifestlib.MANIFEST_SCHEMA_PATH)
    assert schema["schema_id"] == "bmas.test_manifest"
    assert schema["metadata"]["contract_version"] == "1.0.0"


# ── Naming check ───────────────────────────────────────────────────


def run_naming_check(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            PYTHON,
            str(SCRIPTS_DIR / "check-source-naming.py"),
            "--repo-root",
            str(root),
            "--root",
            ".",
            "--baseline",
            "naming-baseline.json",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def test_naming_check_rejects_versioned_source_names(tmp_path: Path) -> None:
    root = tmp_path / "violations"
    root.mkdir()
    (root / "runtime.py").write_text(
        "RuntimeAdmissionV2 = 1\n"
        "board_entries_2 = []\n"
        "# migrate this before the v3 cutover\n",
        encoding="utf-8",
    )
    outcome = run_naming_check(root)
    assert outcome.returncode == 1
    assert "RuntimeAdmissionV2 (version-token)" in outcome.stderr
    assert "board_entries_2 (numeric-suffix)" in outcome.stderr
    assert "v3 (version-token)" in outcome.stderr


def test_naming_check_rejects_versioned_typescript_and_files(tmp_path: Path) -> None:
    root = tmp_path / "typescript"
    root.mkdir()
    (root / "adapterV2.ts").write_text(
        "export const adapterVersionTag = 'string value: v2 never scans';\n"
        "const legacyAdapterV2 = 1;\n",
        encoding="utf-8",
    )
    outcome = run_naming_check(root)
    assert outcome.returncode == 1
    assert "legacyAdapterV2 (version-token)" in outcome.stderr
    assert "adapterV2.ts (version-token)" in outcome.stderr
    assert "never scans" not in outcome.stderr


def test_naming_check_accepts_metadata_versions(tmp_path: Path) -> None:
    root = tmp_path / "metadata"
    root.mkdir()
    (root / "contract.yaml").write_text(
        "schema_id: bmas.example\n"
        "metadata:\n"
        '  contract_version: "2.0.0"\n'
        "  stored_reader: classic\n",
        encoding="utf-8",
    )
    (root / "contract.json").write_text(
        '{"metadata": {"contract_version": "2.0.0"}, "schema_id": "bmas.example"}\n',
        encoding="utf-8",
    )
    (root / "digests.py").write_text(
        "import hashlib\n"
        "def entry_digest(data: bytes) -> str:\n"
        "    return hashlib.sha256(data).hexdigest()\n"
        "CONTRACT_VERSION = '2.0.0'\n",
        encoding="utf-8",
    )
    outcome = run_naming_check(root)
    assert outcome.returncode == 0, outcome.stderr
    assert "PASS" in outcome.stdout


def test_naming_check_baseline_freezes_and_ratchets(tmp_path: Path) -> None:
    root = tmp_path / "ratchet"
    root.mkdir()
    legacy = root / "legacy.py"
    legacy.write_text("legacy_v2_journal = 1\n", encoding="utf-8")

    frozen = run_naming_check(root, "--update-baseline")
    assert frozen.returncode == 0
    assert run_naming_check(root).returncode == 0

    legacy.write_text(
        "legacy_v2_journal = 1\nfresh_v3_journal = 2\n", encoding="utf-8"
    )
    grown = run_naming_check(root)
    assert grown.returncode == 1
    assert "fresh_v3_journal" in grown.stderr

    legacy.write_text("journal = 1\n", encoding="utf-8")
    stale = run_naming_check(root)
    assert stale.returncode == 1
    assert "stale" in stale.stderr


def test_repository_naming_check_passes() -> None:
    outcome = subprocess.run(
        [PYTHON, str(SCRIPTS_DIR / "check-source-naming.py")],
        capture_output=True,
        text=True,
    )
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr
