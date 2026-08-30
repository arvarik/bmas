#!/usr/bin/env python3
"""Execute the authoritative test manifest and write one durable result record.

This runner is the only producer of the final result record at
``test-results/<run_id>/test-manifest-result.json``. It:

1. Loads and validates the manifest.
2. Resolves one named profile into an ordered group list.
3. Executes each group command as an exact argument array without a shell.
4. Records the commit, dirty state, command, tools, counts, attempts,
   durations, logs, and artifacts.
5. Keeps every failed attempt. A retry never replaces a failed first attempt.
6. Validates a temporary record before one atomic rename creates the
   final record.

Usage:
    python3 scripts/run-test-manifest.py --profile complete
"""

from __future__ import annotations

import argparse
import glob
import os
import secrets
import signal as signal_module
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manifestlib


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="The manifest profile to execute.")
    parser.add_argument(
        "--manifest",
        default=manifestlib.MANIFEST_FILE_NAME,
        help="Path to the manifest file, relative to the repository root.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root. The default is the parent of this script's directory.",
    )
    parser.add_argument(
        "--results-dir",
        default="test-results",
        help="Directory that receives one subdirectory per run.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the resolved group identifiers and exit without execution.",
    )
    return parser.parse_args(argv)


def make_run_id() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{secrets.token_hex(4)}"


def parse_pytest_counts(stdout_text: str) -> dict[str, int] | None:
    import re

    counts = {}
    for state in ("passed", "failed", "skipped"):
        match = None
        for match in re.finditer(rf"(\d+) {state}", stdout_text):
            pass
        counts[state] = int(match.group(1)) if match else 0
    if any(counts.values()):
        return counts
    return None


def attempt_counts(group: dict[str, Any], state: str, stdout_text: str) -> dict[str, int]:
    if state == "passed" or state == "failed":
        if group.get("parser") == "pytest_summary":
            parsed = parse_pytest_counts(stdout_text)
            if parsed is not None:
                return parsed
        if state == "passed":
            return {"passed": 1, "failed": 0, "skipped": 0}
        return {"passed": 0, "failed": 1, "skipped": 0}
    return {"passed": 0, "failed": 0, "skipped": 0}


def file_record(path: Path, recorded_path: str) -> dict[str, Any]:
    return {
        "path": recorded_path,
        "media_type": manifestlib.media_type_for(recorded_path),
        "size_bytes": path.stat().st_size,
        "sha256": manifestlib.file_sha256(path),
    }


def collect_artifacts(repo_root: Path, group: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for pattern in group.get("artifacts", []):
        matches = sorted(glob.glob(str(repo_root / pattern), recursive=True))
        for match in matches:
            path = Path(match)
            if not path.is_file():
                continue
            records.append(file_record(path, path.relative_to(repo_root).as_posix()))
    return records


def run_attempt(
    repo_root: Path,
    run_dir: Path,
    group: dict[str, Any],
    attempt_index: int,
    retry_reason: str | None,
) -> dict[str, Any]:
    attempt_dir = run_dir / "groups" / group["id"] / f"attempt-{attempt_index}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = attempt_dir / "stdout.log"
    stderr_path = attempt_dir / "stderr.log"
    relative_dir = attempt_dir.relative_to(run_dir).as_posix()

    working_directory = repo_root / group["working_directory"]
    child_environment = dict(os.environ)
    child_environment.update(group.get("environment", {}).get("set", {}))

    started_at = manifestlib.utc_now_rfc3339()
    started_monotonic = time.monotonic_ns()
    exit_code: int | None = None
    signal_name: str | None = None

    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            process = subprocess.Popen(
                group["argv"],
                cwd=working_directory,
                env=child_environment,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=group["timeout_seconds"])
                if returncode >= 0:
                    exit_code = returncode
                    state = "passed" if returncode == 0 else "failed"
                else:
                    try:
                        signal_name = signal_module.Signals(-returncode).name
                    except ValueError:
                        signal_name = f"signal {-returncode}"
                    state = "failed"
            except subprocess.TimeoutExpired:
                _terminate_process_group(process)
                state = "timed_out"
            except KeyboardInterrupt:
                _terminate_process_group(process)
                state = "cancelled"
    except OSError as error:
        stderr_path.write_bytes(f"runner infrastructure error: {error}\n".encode())
        stdout_path.touch()
        state = "infrastructure_error"

    duration_nanos = time.monotonic_ns() - started_monotonic
    ended_at = manifestlib.utc_now_rfc3339()

    stdout_text = stdout_path.read_bytes().decode("utf-8", "replace")
    logs = [
        file_record(stdout_path, f"{relative_dir}/stdout.log"),
        file_record(stderr_path, f"{relative_dir}/stderr.log"),
    ]

    return {
        "attempt_index": attempt_index,
        "state": state,
        "exit_code": exit_code,
        "signal": signal_name,
        "retry_reason": retry_reason,
        "started_at_utc": started_at,
        "ended_at_utc": ended_at,
        "duration_nanos": duration_nanos,
        "counts": attempt_counts(group, state, stdout_text),
        "stdout_sha256": manifestlib.file_sha256(stdout_path),
        "stderr_sha256": manifestlib.file_sha256(stderr_path),
        "logs": logs,
        "artifacts": collect_artifacts(repo_root, group),
    }


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal_module.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    process.wait()


def skipped_group_result(group: dict[str, Any], reason: str) -> dict[str, Any]:
    now = manifestlib.utc_now_rfc3339()
    return {
        **group_result_header(group),
        "started_at_utc": now,
        "ended_at_utc": now,
        "duration_nanos": 0,
        "state": "skipped",
        "skip_reason": reason,
        "attempts": [],
        "artifacts": [],
    }


def group_result_header(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": group["id"],
        "manifest_entry_digest": manifestlib.manifest_entry_digest(group),
        "argv": list(group["argv"]),
        "working_directory": group["working_directory"],
        "environment_profile_digest": manifestlib.environment_profile_digest(group),
        "dependency_versions": {},
        "tool_versions": {},
        "timeout_nanos": int(group["timeout_seconds"] * 1_000_000_000),
    }


def run_group(
    repo_root: Path,
    run_dir: Path,
    group: dict[str, Any],
    group_index: int,
    group_total: int,
) -> dict[str, Any]:
    print(f"[{group_index}/{group_total}] {group['id']} ...", flush=True)
    started_at = manifestlib.utc_now_rfc3339()
    started_monotonic = time.monotonic_ns()

    working_directory = repo_root / group["working_directory"]
    tool_versions = {
        tool: manifestlib.probe_tool_version(tool, working_directory)
        for tool in group.get("tools", [])
    }
    dependency_versions = {name: "undeclared" for name in group.get("dependencies", [])}

    max_attempts = group.get("retry", {}).get("max_attempts", 1)
    attempts: list[dict[str, Any]] = []
    retry_reason: str | None = None
    cancelled = False
    while len(attempts) < max_attempts:
        try:
            attempt = run_attempt(repo_root, run_dir, group, len(attempts), retry_reason)
        except KeyboardInterrupt:
            cancelled = True
            break
        attempts.append(attempt)
        if attempt["state"] == "cancelled":
            cancelled = True
            break
        if attempt["state"] == "passed" or attempt["state"] not in manifestlib.RETRIABLE_STATES:
            break
        retry_reason = (
            f"attempt {attempt['attempt_index']} ended {attempt['state']} "
            f"with exit code {attempt['exit_code']}"
        )

    duration_nanos = time.monotonic_ns() - started_monotonic
    state = attempts[-1]["state"] if attempts else "cancelled"
    print(f"    {group['id']}: {state} ({duration_nanos / 1_000_000_000:.1f}s)", flush=True)

    result = {
        **group_result_header(group),
        "started_at_utc": started_at,
        "ended_at_utc": manifestlib.utc_now_rfc3339(),
        "duration_nanos": duration_nanos,
        "state": state,
        "skip_reason": None,
        "attempts": attempts,
        "artifacts": attempts[-1]["artifacts"] if attempts else [],
    }
    result["tool_versions"] = tool_versions
    result["dependency_versions"] = dependency_versions
    if cancelled and not attempts:
        raise KeyboardInterrupt
    return result


def build_summary(group_results: list[dict[str, Any]]) -> dict[str, int]:
    states = [group["state"] for group in group_results]
    summary = {state: states.count(state) for state in manifestlib.RUN_STATES}
    summary["attempt_count"] = sum(len(group["attempts"]) for group in group_results)
    summary["retry_count"] = sum(
        max(len(group["attempts"]) - 1, 0) for group in group_results
    )
    return summary


def overall_state(group_results: list[dict[str, Any]]) -> str:
    states = [group["state"] for group in group_results]
    for candidate in ("infrastructure_error", "timed_out", "cancelled", "failed"):
        if candidate in states:
            return candidate
    if states and all(state == "skipped" for state in states):
        return "skipped"
    return "passed"


def main(argv: list[str]) -> int:
    arguments = parse_arguments(argv)
    repo_root = (
        Path(arguments.repo_root).resolve()
        if arguments.repo_root
        else Path(__file__).resolve().parent.parent
    )
    manifest_path = repo_root / arguments.manifest

    try:
        manifest, manifest_bytes = manifestlib.load_manifest(repo_root, manifest_path)
        resolved = manifestlib.resolve_profile(manifest, arguments.profile)
    except manifestlib.ManifestError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if arguments.list:
        for group in resolved:
            print(group["id"])
        return 0

    result_schema = manifestlib.load_schema(repo_root, manifestlib.RESULT_SCHEMA_PATH)
    run_id = make_run_id()
    run_dir = (repo_root / arguments.results_dir / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)

    started_at = manifestlib.utc_now_rfc3339()
    started_monotonic = time.monotonic_ns()

    repository = manifestlib.repository_provenance(repo_root)
    host_tools = sorted({tool for group in resolved for tool in group.get("tools", [])} | {"git"})
    host = manifestlib.host_info(host_tools, repo_root)

    group_results: list[dict[str, Any]] = []
    outcome_by_id: dict[str, str] = {}
    cancelled = False
    for index, group in enumerate(resolved, start=1):
        if cancelled:
            group_results.append(
                skipped_group_result(group, "the run was cancelled before this group started")
            )
            outcome_by_id[group["id"]] = "skipped"
            continue
        failed_dependency = next(
            (
                dependency
                for dependency in group.get("depends_on", [])
                if outcome_by_id.get(dependency) != "passed"
            ),
            None,
        )
        if failed_dependency is not None:
            reason = f"dependency {failed_dependency} did not pass"
            group_results.append(skipped_group_result(group, reason))
            outcome_by_id[group["id"]] = "skipped"
            print(f"[{index}/{len(resolved)}] {group['id']}: skipped ({reason})", flush=True)
            continue
        try:
            result = run_group(repo_root, run_dir, group, index, len(resolved))
        except KeyboardInterrupt:
            cancelled = True
            result = skipped_group_result(group, "the run was cancelled during this group")
        if result["state"] == "cancelled":
            cancelled = True
        group_results.append(result)
        outcome_by_id[group["id"]] = result["state"]

    record = {
        "schema_id": manifestlib.RESULT_SCHEMA_ID,
        "run_id": run_id,
        "state": overall_state(group_results),
        "started_at_utc": started_at,
        "ended_at_utc": manifestlib.utc_now_rfc3339(),
        "duration_nanos": time.monotonic_ns() - started_monotonic,
        "manifest_schema_version": manifest["metadata"]["contract_version"],
        "manifest_digest": manifestlib.sha256_hex(manifest_bytes),
        "profile_id": arguments.profile,
        "resolved_group_ids": [group["id"] for group in resolved],
        "repository": repository,
        "host": host,
        "summary": build_summary(group_results),
        "groups": group_results,
    }

    try:
        final_path = manifestlib.write_final_record(
            record, run_dir, manifest, manifest_bytes, result_schema, repo_root
        )
    except manifestlib.ResultValidationError as error:
        print("ERROR: the runner produced an invalid record:", file=sys.stderr)
        for message in error.errors:
            print(f"  - {message}", file=sys.stderr)
        return 2

    summary = record["summary"]
    print(
        f"\n{record['state'].upper()}: profile {arguments.profile} — "
        f"{summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['skipped']} skipped, {summary['timed_out']} timed out, "
        f"{summary['cancelled']} cancelled, "
        f"{summary['infrastructure_error']} infrastructure errors, "
        f"{summary['retry_count']} retries"
    )
    print(f"Result: {final_path}")
    return 0 if record["state"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
