#!/usr/bin/env python3
"""Validate one runner result record without mutation.

The validator checks the record against the result schema and against
the resolved manifest: group set, entry digests, argument arrays,
working directories, dependencies, timeouts, attempt order, preserved
failures, and summary counts.

Usage:
    python3 scripts/validate-test-result.py test-results/<run_id>/test-manifest-result.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manifestlib


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", help="Path to a test-manifest-result.json file.")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--manifest", default=manifestlib.MANIFEST_FILE_NAME)
    parser.add_argument(
        "--verify-files",
        action="store_true",
        help="Also verify the sizes and digests of the recorded log and artifact files.",
    )
    arguments = parser.parse_args(argv)

    repo_root = (
        Path(arguments.repo_root).resolve()
        if arguments.repo_root
        else Path(__file__).resolve().parent.parent
    )
    result_path = Path(arguments.result).resolve()

    try:
        manifest, manifest_bytes = manifestlib.load_manifest(
            repo_root, repo_root / arguments.manifest
        )
    except manifestlib.ManifestError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    try:
        result = manifestlib.load_json_text(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"FAIL: cannot read the result record: {error}", file=sys.stderr)
        return 1

    result_schema = manifestlib.load_schema(repo_root, manifestlib.RESULT_SCHEMA_PATH)
    errors = manifestlib.validate_result(
        result,
        manifest,
        manifest_bytes,
        result_schema,
        repo_root=repo_root,
        result_dir=result_path.parent,
        verify_files=arguments.verify_files,
    )
    if errors:
        for message in errors:
            print(f"FAIL: {message}", file=sys.stderr)
        return 1

    print(
        f"PASS: run {result['run_id']} ({result['state']}) is a valid record for "
        f"profile {result['profile_id']} at commit {result['repository']['commit'][:12]}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
