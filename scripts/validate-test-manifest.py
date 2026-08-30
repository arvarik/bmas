#!/usr/bin/env python3
"""Validate the authoritative test manifest and its consumers.

The validator proves four claims:

1. The manifest matches its schema and its structural rules.
2. The complete profile resolves every active_required group, and the
   continuous integration partition profiles exactly cover that set.
3. The continuous integration workflow and the local script consume the
   manifest through the same runner and the same group identifiers.
4. The complete profile includes the Playwright browser groups.

Usage:
    python3 scripts/validate-test-manifest.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manifestlib

PROFILE_FLAG_PATTERN = re.compile(r"run-test-manifest\.py\s+--profile[= ]([A-Za-z0-9._-]+)")


def profiles_in_workflow(workflow_path: Path) -> list[str]:
    """Collect every profile that the workflow executes through the runner."""
    workflow = manifestlib.load_yaml_text(workflow_path.read_text(encoding="utf-8"))
    found: list[str] = []
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            run_text = step.get("run")
            if not isinstance(run_text, str):
                continue
            found.extend(PROFILE_FLAG_PATTERN.findall(run_text))
    return found


def profiles_in_script(script_path: Path) -> list[str]:
    return PROFILE_FLAG_PATTERN.findall(script_path.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--manifest", default=manifestlib.MANIFEST_FILE_NAME)
    parser.add_argument("--ci-workflow", default=".github/workflows/ci.yml")
    parser.add_argument("--local-script", default="scripts/check-ci.sh")
    arguments = parser.parse_args(argv)

    repo_root = (
        Path(arguments.repo_root).resolve()
        if arguments.repo_root
        else Path(__file__).resolve().parent.parent
    )

    errors: list[str] = []
    try:
        manifest, _ = manifestlib.load_manifest(repo_root, repo_root / arguments.manifest)
    except manifestlib.ManifestError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    complete = manifestlib.resolve_profile(manifest, manifestlib.COMPLETE_PROFILE_ID)
    complete_ids = [group["id"] for group in complete]

    playwright_groups = [
        group["id"] for group in complete if "playwright" in group.get("tools", [])
    ]
    if not playwright_groups:
        errors.append("the complete profile does not include a Playwright group")

    partition_ids = sorted(
        profile["id"]
        for profile in manifest.get("profiles", [])
        if profile["id"].startswith(manifestlib.CI_PROFILE_PREFIX)
    )
    if not partition_ids:
        errors.append("the manifest defines no continuous integration partition profiles")

    workflow_profiles = profiles_in_workflow(repo_root / arguments.ci_workflow)
    if sorted(workflow_profiles) != partition_ids:
        errors.append(
            f"{arguments.ci_workflow} executes profiles {sorted(workflow_profiles)} "
            f"but the manifest partitions are {partition_ids}; both consumers must "
            "select the same required groups"
        )

    workflow_group_ids: list[str] = []
    for profile_id in workflow_profiles:
        try:
            workflow_group_ids.extend(
                group["id"] for group in manifestlib.resolve_profile(manifest, profile_id)
            )
        except manifestlib.ManifestError as error:
            errors.append(str(error))
    if sorted(workflow_group_ids) != sorted(complete_ids):
        missing = sorted(set(complete_ids) - set(workflow_group_ids))
        extra = sorted(set(workflow_group_ids) - set(complete_ids))
        errors.append(
            "continuous integration does not execute the complete required set; "
            f"missing {missing}, extra {extra}"
        )

    local_profiles = profiles_in_script(repo_root / arguments.local_script)
    if manifestlib.COMPLETE_PROFILE_ID not in local_profiles:
        errors.append(
            f"{arguments.local_script} does not execute the "
            f"{manifestlib.COMPLETE_PROFILE_ID} profile through the runner"
        )

    if errors:
        for message in errors:
            print(f"FAIL: {message}", file=sys.stderr)
        return 1

    print(
        f"PASS: {len(complete_ids)} required groups; continuous integration "
        f"partitions {partition_ids} cover the complete profile; Playwright "
        f"groups {playwright_groups} are required in both consumers."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
