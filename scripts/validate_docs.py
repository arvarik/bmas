#!/usr/bin/env python3
"""Check local links in published Markdown documentation."""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    """Return tracked and new Markdown files outside ignored paths."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [REPO_ROOT / line for line in sorted(set(result.stdout.splitlines())) if line]


def main() -> int:
    failures: list[str] = []
    files = markdown_files()
    for path in files:
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for raw_target in LINK_PATTERN.findall(line):
                target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
                target = unquote(target).split("#", 1)[0]
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.exists():
                    relative = path.relative_to(REPO_ROOT)
                    failures.append(f"{relative}:{line_number}: missing {raw_target}")

    if failures:
        print("\n".join(failures))
        return 1
    print(f"PASS: {len(files)} Markdown files have valid local links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
