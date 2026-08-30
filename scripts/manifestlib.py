"""Shared library for the authoritative test manifest.

This module gives every manifest consumer one implementation for:

1. Duplicate-key-safe YAML and JSON parsing.
2. Manifest loading, schema validation, and structural validation.
3. Profile resolution to an ordered group list.
4. Entry digests, environment-profile digests, and file digests.
5. Repository and host provenance collection.
6. Result-record cross-validation against the resolved manifest.
7. Atomic final-record writes.

Only `scripts/run-test-manifest.py` produces a final result record.
Every other consumer reads the record without mutation.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import jsonschema
import yaml

MANIFEST_FILE_NAME = "test-manifest.yaml"
MANIFEST_SCHEMA_PATH = "schemas/test-manifest.schema.json"
RESULT_SCHEMA_PATH = "schemas/test-manifest-result.schema.json"
MANIFEST_SCHEMA_ID = "bmas.test_manifest"
RESULT_SCHEMA_ID = "bmas.test_manifest_result"
RESULT_FILE_NAME = "test-manifest-result.json"
COMPLETE_PROFILE_ID = "complete"
CI_PROFILE_PREFIX = "ci."
TARGETED_GROUP_PREFIX = "group:"

GROUP_STATES = ("reserved", "active_optional", "active_required", "retired")
ACTIVE_STATES = ("active_optional", "active_required")
RUN_STATES = ("passed", "failed", "skipped", "cancelled", "timed_out", "infrastructure_error")
ATTEMPT_STATES = ("passed", "failed", "cancelled", "timed_out", "infrastructure_error")
RETRIABLE_STATES = ("failed", "timed_out")

TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VERSION_TOKEN_PATTERN = re.compile(r"^[vV][0-9]+$")

MEDIA_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".txt": "text/plain",
    ".webm": "video/webm",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".zip": "application/zip",
}


class ManifestError(Exception):
    """The manifest file is invalid."""


class ResultValidationError(Exception):
    """A result record failed validation."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


# ── Duplicate-key-safe parsing ─────────────────────────────────────


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key!r}")
        result[key] = value
    return result


def load_json_text(text: str) -> Any:
    """Parse JSON text and reject duplicate object keys."""
    return json.loads(text, object_pairs_hook=_reject_duplicate_pairs)


class _UniqueKeyYamlLoader(yaml.SafeLoader):
    """A safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(loader: _UniqueKeyYamlLoader, node: yaml.MappingNode) -> dict:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise ManifestError(f"duplicate mapping key: {key!r}")
        seen.add(key)
    return dict(loader.construct_pairs(node, deep=True))


_UniqueKeyYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_yaml_text(text: str) -> Any:
    """Parse YAML text and reject duplicate mapping keys."""
    return yaml.load(text, Loader=_UniqueKeyYamlLoader)


# ── Digests and canonical encoding ─────────────────────────────────


def canonical_json(value: Any) -> str:
    """Encode a value as deterministic JSON with sorted keys."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entry_digest(group: dict[str, Any]) -> str:
    """Return the digest of one manifest group entry as written."""
    return sha256_hex(canonical_json(group).encode("utf-8"))


def environment_profile_digest(group: dict[str, Any]) -> str:
    """Return the digest of the declared, sanitized environment profile.

    The digest covers the declared profile only. It never covers live
    environment values, so it cannot leak a secret.
    """
    profile = {
        "inherit": True,
        "set": group.get("environment", {}).get("set", {}),
        "passthrough": sorted(group.get("environment", {}).get("passthrough", [])),
    }
    return sha256_hex(canonical_json(profile).encode("utf-8"))


def media_type_for(path: str) -> str:
    return MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


# ── Time helpers ───────────────────────────────────────────────────


def utc_now_rfc3339() -> str:
    """Return the current UTC wall-clock time with nanosecond digits."""
    nanos = time.time_ns()
    seconds, fraction = divmod(nanos, 1_000_000_000)
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
    return f"{base}.{fraction:09d}Z"


def parse_rfc3339(value: str) -> tuple[int, int]:
    """Parse one runner timestamp into whole seconds and fraction nanos."""
    if not TIMESTAMP_PATTERN.match(value):
        raise ValueError(f"invalid RFC 3339 UTC timestamp: {value!r}")
    body, fraction = value[:-1].split(".")
    parsed = time.strptime(body, "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(parsed), int(fraction)


def clock_sources() -> tuple[str, str]:
    """Return the UTC clock source and the monotonic clock implementation."""
    monotonic_implementation = time.get_clock_info("monotonic").implementation
    return "python.time.time_ns", f"python.time.monotonic_ns/{monotonic_implementation}"


# ── Schema loading and validation ──────────────────────────────────


def load_schema(repo_root: Path, relative_path: str) -> dict[str, Any]:
    schema = load_json_text((repo_root / relative_path).read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise ManifestError(f"{relative_path} is not a JSON object")
    return schema


def schema_errors(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Return every schema violation as a readable message list."""
    validator = jsonschema.Draft202012Validator(schema)
    messages = []
    for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        messages.append(f"schema: {location}: {error.message}")
    return messages


# ── Manifest loading and structural validation ─────────────────────


def _identifier_segments(identifier: str) -> list[str]:
    """Split an identifier into words on separators and case changes."""
    parts = re.split(r"[._\-]", identifier)
    words: list[str] = []
    word_pattern = r"[A-Z][0-9]+(?![a-z])|[A-Z]+(?![a-z0-9])|[A-Z][a-z0-9]*|[a-z0-9]+"
    for part in parts:
        words.extend(re.findall(word_pattern, part))
    return [word for word in words if word]


def contains_version_token(identifier: str) -> bool:
    """Report whether an identifier contains a version token segment."""
    return any(VERSION_TOKEN_PATTERN.match(word) for word in _identifier_segments(identifier))


def load_manifest(repo_root: Path, manifest_path: Path) -> tuple[dict[str, Any], bytes]:
    """Load, schema-validate, and structurally validate the manifest.

    Returns the parsed manifest and the exact file bytes.
    """
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = load_yaml_text(manifest_bytes.decode("utf-8"))
    except yaml.YAMLError as error:
        raise ManifestError(f"cannot parse {manifest_path}: {error}") from error
    if not isinstance(manifest, dict):
        raise ManifestError(f"{manifest_path} is not a mapping")

    schema = load_schema(repo_root, MANIFEST_SCHEMA_PATH)
    errors = schema_errors(manifest, schema)
    errors.extend(_manifest_structure_errors(manifest))
    if errors:
        raise ManifestError("; ".join(errors))
    return manifest, manifest_bytes


def _manifest_structure_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    groups = manifest.get("groups", [])
    profiles = manifest.get("profiles", [])

    seen_ids: set[str] = set()
    active_ids: list[str] = []
    ordered_ids: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for group in groups:
        group_id = group.get("id", "")
        if group_id in seen_ids:
            errors.append(f"duplicate group id: {group_id}")
        seen_ids.add(group_id)
        ordered_ids.append(group_id)
        by_id[group_id] = group
        if contains_version_token(group_id):
            errors.append(f"group id contains a version token: {group_id}")
        if group.get("state") in ACTIVE_STATES:
            active_ids.append(group_id)

    for group in groups:
        for dependency in group.get("depends_on", []):
            if dependency not in by_id:
                errors.append(f"{group['id']}: unknown dependency {dependency}")
                continue
            if by_id[dependency].get("state") not in ACTIVE_STATES:
                errors.append(f"{group['id']}: dependency {dependency} is not active")
            elif ordered_ids.index(dependency) >= ordered_ids.index(group["id"]):
                errors.append(f"{group['id']}: dependency {dependency} must come first")
            elif (
                group.get("state") == "active_required"
                and by_id[dependency].get("state") != "active_required"
            ):
                errors.append(
                    f"{group['id']}: a required group cannot depend on the "
                    f"optional group {dependency}"
                )

    profile_ids: set[str] = set()
    for profile in profiles:
        profile_id = profile.get("id", "")
        if profile_id in profile_ids:
            errors.append(f"duplicate profile id: {profile_id}")
        profile_ids.add(profile_id)
        if contains_version_token(profile_id):
            errors.append(f"profile id contains a version token: {profile_id}")
        segments = _identifier_segments(profile_id)
        if segments and segments[-1].isdigit():
            errors.append(f"profile id contains a numeric suffix: {profile_id}")
        selector = profile.get("selector", {})
        for state in selector.get("states", []):
            if state not in ACTIVE_STATES:
                errors.append(f"profile {profile_id} selects non-executable state {state}")
        for group_id in selector.get("groups", []):
            if group_id not in by_id:
                errors.append(f"profile {profile_id} names unknown group {group_id}")
            elif by_id[group_id].get("state") not in ACTIVE_STATES:
                errors.append(
                    f"profile {profile_id} names non-executable group {group_id}"
                )

    if COMPLETE_PROFILE_ID not in profile_ids:
        errors.append(f"the manifest must define the {COMPLETE_PROFILE_ID} profile")
        return errors

    complete_ids = [g["id"] for g in _resolve_profile_groups(manifest, COMPLETE_PROFILE_ID)]
    required_ids = [g["id"] for g in groups if g.get("state") == "active_required"]
    if complete_ids != required_ids:
        errors.append(
            "the complete profile must resolve to every active_required group in "
            f"manifest order; expected {required_ids}, resolved {complete_ids}"
        )

    partition_ids = sorted(pid for pid in profile_ids if pid.startswith(CI_PROFILE_PREFIX))
    if partition_ids:
        covered: list[str] = []
        for partition_id in partition_ids:
            for group in _resolve_profile_groups(manifest, partition_id):
                if group["id"] in covered:
                    errors.append(
                        f"group {group['id']} appears in more than one ci partition"
                    )
                covered.append(group["id"])
        if sorted(covered) != sorted(complete_ids):
            missing = sorted(set(complete_ids) - set(covered))
            extra = sorted(set(covered) - set(complete_ids))
            errors.append(
                "the ci partitions must exactly cover the complete profile; "
                f"missing {missing}, extra {extra}"
            )
        for partition_id in partition_ids:
            partition_group_ids = {
                g["id"] for g in _resolve_profile_groups(manifest, partition_id)
            }
            for group_id in partition_group_ids:
                for dependency in by_id[group_id].get("depends_on", []):
                    if dependency not in partition_group_ids:
                        errors.append(
                            f"profile {partition_id}: group {group_id} depends on "
                            f"{dependency} outside the profile"
                        )

    for profile in profiles:
        if not _resolve_profile_groups(manifest, profile["id"]):
            errors.append(f"profile {profile['id']} resolves to zero groups")

    return errors


def _resolve_profile_groups(manifest: dict[str, Any], profile_id: str) -> list[dict[str, Any]]:
    profile = next(
        (p for p in manifest.get("profiles", []) if p.get("id") == profile_id), None
    )
    if profile is None:
        raise ManifestError(f"unknown profile: {profile_id}")
    selector = profile.get("selector", {})
    states = selector.get("states")
    explicit = selector.get("groups")
    resolved = []
    for group in manifest.get("groups", []):
        if states is not None and group.get("state") in states:
            resolved.append(group)
        elif explicit is not None and group.get("id") in explicit:
            resolved.append(group)
    return resolved


def resolve_profile(manifest: dict[str, Any], profile_id: str) -> list[dict[str, Any]]:
    """Resolve a profile, or one targeted group, into an ordered group list.

    A profile identifier with the ``group:`` prefix names one executable
    group directly. A targeted group run assumes that its dependencies
    already passed.
    """
    if profile_id.startswith(TARGETED_GROUP_PREFIX):
        group_id = profile_id[len(TARGETED_GROUP_PREFIX):]
        group = next(
            (g for g in manifest.get("groups", []) if g.get("id") == group_id), None
        )
        if group is None:
            raise ManifestError(f"unknown group: {group_id}")
        if group.get("state") not in ACTIVE_STATES:
            raise ManifestError(f"group {group_id} is not executable")
        return [group]
    groups = _resolve_profile_groups(manifest, profile_id)
    for group in groups:
        if group.get("state") not in ACTIVE_STATES:
            raise ManifestError(
                f"profile {profile_id} resolved non-executable group {group.get('id')}"
            )
    return groups


# ── Provenance ─────────────────────────────────────────────────────


def _git(repo_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
    ).stdout


def repository_provenance(repo_root: Path) -> dict[str, Any]:
    """Collect the exact commit, branch, tracked diff, and untracked files."""
    commit = _git(repo_root, "rev-parse", "HEAD").decode("ascii").strip()
    branch_probe = subprocess.run(
        ["git", "-C", str(repo_root), "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True,
    )
    branch = branch_probe.stdout.decode("utf-8").strip() if branch_probe.returncode == 0 else None

    tracked_diff = _git(repo_root, "diff", "--binary", "HEAD", "--")
    untracked_raw = _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_paths = sorted(
        (p for p in untracked_raw.decode("utf-8").split("\0") if p),
        key=lambda p: p.encode("utf-8"),
    )
    untracked_files = []
    for path in untracked_paths:
        full = repo_root / path
        try:
            untracked_files.append(
                {
                    "path": path,
                    "size_bytes": full.stat().st_size,
                    "sha256": file_sha256(full),
                }
            )
        except OSError:
            continue

    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(tracked_diff) or bool(untracked_files),
        "tracked_diff_sha256": sha256_hex(tracked_diff),
        "untracked_files": untracked_files,
    }


def probe_tool_version(tool: str, working_directory: Path) -> str:
    """Resolve one pinned tool version without any network access."""
    if tool == "playwright":
        package_json = working_directory / "node_modules" / "@playwright" / "test" / "package.json"
        try:
            package = load_json_text(package_json.read_text(encoding="utf-8"))
            return str(package.get("version", "unavailable"))
        except (OSError, ValueError):
            return "unavailable"

    probes: dict[str, list[str]] = {
        "python": ["python3", "--version"],
        "git": ["git", "--version"],
        "node": ["node", "--version"],
        "npm": ["npm", "--version"],
        "docker": ["docker", "--version"],
        "ruff": ["python3", "-m", "ruff", "--version"],
        "mypy": ["python3", "-m", "mypy", "--version"],
        "pytest": ["python3", "-m", "pytest", "--version"],
    }
    argv = probes.get(tool)
    if argv is None or shutil.which(argv[0]) is None:
        return "unavailable"
    try:
        probe = subprocess.run(
            argv, capture_output=True, timeout=60, cwd=working_directory
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if probe.returncode != 0:
        return "unavailable"
    return probe.stdout.decode("utf-8", "replace").strip().splitlines()[0]


def host_info(tools: list[str], working_directory: Path) -> dict[str, Any]:
    utc_clock_source, monotonic_clock = clock_sources()
    return {
        "os": platform.system(),
        "architecture": platform.machine(),
        "image_digest": os.environ.get("BMAS_HOST_IMAGE_DIGEST") or None,
        "kernel_version": platform.release(),
        "utc_clock_source": utc_clock_source,
        "monotonic_clock": monotonic_clock,
        "tool_versions": {
            tool: probe_tool_version(tool, working_directory) for tool in sorted(set(tools))
        },
    }


# ── Result cross-validation ────────────────────────────────────────


def _state_severity(states: list[str]) -> str:
    for candidate in ("infrastructure_error", "timed_out", "cancelled", "failed"):
        if candidate in states:
            return candidate
    if states and all(state == "skipped" for state in states):
        return "skipped"
    return "passed"


def _check_timestamps(errors: list[str], label: str, record: dict[str, Any]) -> None:
    try:
        started = parse_rfc3339(record["started_at_utc"])
        ended = parse_rfc3339(record["ended_at_utc"])
    except (ValueError, KeyError) as error:
        errors.append(f"{label}: {error}")
        return
    if ended < started:
        errors.append(f"{label}: ended_at_utc comes before started_at_utc")


def validate_result(
    result: Any,
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    result_schema: dict[str, Any],
    *,
    repo_root: Path | None = None,
    result_dir: Path | None = None,
    verify_files: bool = False,
) -> list[str]:
    """Validate a result record against the schema and the resolved manifest.

    Returns every violation. An empty list means the record is valid.
    """
    errors = schema_errors(result, result_schema)
    if errors:
        return errors

    if result["manifest_digest"] != sha256_hex(manifest_bytes):
        errors.append("manifest_digest does not match the manifest file bytes")
    expected_contract = manifest.get("metadata", {}).get("contract_version")
    if result["manifest_schema_version"] != expected_contract:
        errors.append(
            f"manifest_schema_version {result['manifest_schema_version']!r} does not "
            f"match the manifest contract version {expected_contract!r}"
        )

    try:
        resolved = resolve_profile(manifest, result["profile_id"])
    except ManifestError as error:
        errors.append(str(error))
        return errors

    resolved_ids = [group["id"] for group in resolved]
    if result["resolved_group_ids"] != resolved_ids:
        errors.append(
            f"resolved_group_ids {result['resolved_group_ids']} does not match the "
            f"profile resolution {resolved_ids}"
        )
    recorded_ids = [group["group_id"] for group in result["groups"]]
    if recorded_ids != resolved_ids:
        errors.append(
            f"result groups {recorded_ids} do not match the profile resolution "
            f"{resolved_ids}"
        )
        return errors

    _check_timestamps(errors, "run", result)

    group_states: list[str] = []
    attempt_total = 0
    retry_total = 0
    for entry, recorded in zip(resolved, result["groups"], strict=True):
        label = f"group {recorded['group_id']}"
        group_states.append(recorded["state"])
        _check_timestamps(errors, label, recorded)

        if recorded["manifest_entry_digest"] != manifest_entry_digest(entry):
            errors.append(f"{label}: manifest_entry_digest does not match the manifest")
        if recorded["argv"] != entry["argv"]:
            errors.append(f"{label}: argv does not match the manifest")
        if recorded["working_directory"] != entry["working_directory"]:
            errors.append(f"{label}: working_directory does not match the manifest")
        if recorded["environment_profile_digest"] != environment_profile_digest(entry):
            errors.append(f"{label}: environment_profile_digest does not match the manifest")
        if recorded["timeout_nanos"] != int(entry["timeout_seconds"] * 1_000_000_000):
            errors.append(f"{label}: timeout_nanos does not match the manifest")
        if sorted(recorded["dependency_versions"]) != sorted(entry.get("dependencies", [])):
            errors.append(f"{label}: dependency_versions keys do not match the manifest")
        if sorted(recorded["tool_versions"]) != sorted(entry.get("tools", [])):
            errors.append(f"{label}: tool_versions keys do not match the manifest")

        attempts = recorded["attempts"]
        attempt_total += len(attempts)
        retry_total += max(len(attempts) - 1, 0)
        max_attempts = entry.get("retry", {}).get("max_attempts", 1)
        if len(attempts) > max_attempts:
            errors.append(f"{label}: {len(attempts)} attempts exceed the limit {max_attempts}")

        indexes = [attempt["attempt_index"] for attempt in attempts]
        if indexes != list(range(len(attempts))):
            errors.append(f"{label}: attempt indexes {indexes} are not contiguous from zero")

        if not attempts:
            if recorded["state"] != "skipped":
                errors.append(f"{label}: a group without attempts must be skipped")
            if not recorded["skip_reason"]:
                errors.append(f"{label}: a skipped group needs a nonempty skip_reason")
        else:
            if recorded["state"] == "skipped":
                errors.append(f"{label}: a skipped group cannot have attempts")
            if recorded["skip_reason"] is not None:
                errors.append(f"{label}: a group with attempts needs a null skip_reason")
            if recorded["state"] != attempts[-1]["state"]:
                errors.append(f"{label}: the group state must equal the last attempt state")

        for attempt in attempts:
            attempt_label = f"{label} attempt {attempt['attempt_index']}"
            _check_timestamps(errors, attempt_label, attempt)
            if attempt["attempt_index"] == 0:
                if attempt["retry_reason"] is not None:
                    errors.append(f"{attempt_label}: the first attempt needs a null retry_reason")
            elif not attempt["retry_reason"]:
                errors.append(f"{attempt_label}: a retry needs a nonempty retry_reason")
            if attempt is not attempts[-1] and attempt["state"] == "passed":
                errors.append(
                    f"{attempt_label}: a passed attempt cannot come before a retry; "
                    "a retry can never hide a first-attempt result"
                )
            if verify_files and result_dir is not None:
                for log in attempt["logs"]:
                    log_path = result_dir / log["path"]
                    if not log_path.is_file():
                        errors.append(f"{attempt_label}: missing log file {log['path']}")
                        continue
                    if log_path.stat().st_size != log["size_bytes"]:
                        errors.append(f"{attempt_label}: log size mismatch for {log['path']}")
                    if file_sha256(log_path) != log["sha256"]:
                        errors.append(f"{attempt_label}: log digest mismatch for {log['path']}")
            if verify_files and repo_root is not None:
                for artifact in attempt["artifacts"]:
                    artifact_path = repo_root / artifact["path"]
                    if not artifact_path.is_file():
                        errors.append(
                            f"{attempt_label}: missing artifact file {artifact['path']}"
                        )
                        continue
                    if file_sha256(artifact_path) != artifact["sha256"]:
                        errors.append(
                            f"{attempt_label}: artifact digest mismatch for {artifact['path']}"
                        )

    summary = result["summary"]
    for state in RUN_STATES:
        expected = group_states.count(state)
        if summary[state] != expected:
            errors.append(
                f"summary.{state} is {summary[state]} but {expected} groups ended {state}"
            )
    if summary["attempt_count"] != attempt_total:
        errors.append(
            f"summary.attempt_count is {summary['attempt_count']} but the record "
            f"holds {attempt_total} attempts"
        )
    if summary["retry_count"] != retry_total:
        errors.append(
            f"summary.retry_count is {summary['retry_count']} but the record "
            f"holds {retry_total} retries"
        )

    expected_state = _state_severity(group_states)
    if result["state"] != expected_state:
        errors.append(f"run state is {result['state']} but the groups imply {expected_state}")

    return errors


# ── Atomic final-record write ──────────────────────────────────────


def write_final_record(
    record: dict[str, Any],
    result_dir: Path,
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    result_schema: dict[str, Any],
    repo_root: Path,
) -> Path:
    """Validate a temporary record, then atomically rename it into place.

    An invalid record never becomes a readable final record.
    """
    errors = validate_result(
        record,
        manifest,
        manifest_bytes,
        result_schema,
        repo_root=repo_root,
        result_dir=result_dir,
        verify_files=True,
    )
    if errors:
        raise ResultValidationError(errors)

    result_dir.mkdir(parents=True, exist_ok=True)
    final_path = result_dir / RESULT_FILE_NAME
    temporary_path = result_dir / (RESULT_FILE_NAME + ".tmp")
    encoded = json.dumps(record, indent=2, sort_keys=False) + "\n"
    with temporary_path.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    reparsed = load_json_text(temporary_path.read_text(encoding="utf-8"))
    errors = validate_result(
        reparsed,
        manifest,
        manifest_bytes,
        result_schema,
        repo_root=repo_root,
        result_dir=result_dir,
        verify_files=True,
    )
    if errors:
        temporary_path.unlink(missing_ok=True)
        raise ResultValidationError(errors)
    os.replace(temporary_path, final_path)
    return final_path
