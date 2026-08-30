#!/usr/bin/env python3
"""Reject version tokens and numeric version suffixes in implementation source.

The source naming policy keeps contract versions in explicit metadata
values. Source identifiers, comments, feature flags, modules, file
names, events, domains, and profiles stay version-free.

The check scans these source elements:

1. Identifiers in Python, TypeScript, JavaScript, and shell code.
2. Comments in code, YAML, and shell files.
3. Mapping keys in YAML and JSON files.
4. File and directory names under the scanned roots.

The check never scans string or scalar values, so metadata values such
as a stored contract version always pass.

Two rules reject a token:

1. A version token: a word segment that is the letter "v" followed by
   digits, in any case.
2. A numeric version suffix: an identifier that ends in digits after a
   multi-letter stem, unless the term is a known technical word such as
   a hash or encoding name.

A frozen baseline file lists the legacy violations that existed before
this check became required. Renaming those identifiers would change
runtime behavior, so the baseline freezes them. The check fails when a
new violation appears, and it fails when the baseline becomes stale so
the frozen set only shrinks.

Usage:
    python3 scripts/check-source-naming.py
    python3 scripts/check-source-naming.py --update-baseline
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manifestlib

SCAN_ROOTS = [
    "daemon/src",
    "daemon/tests",
    "agent",
    "conformance",
    "eval",
    "scripts",
    "schemas",
    "mission-control/src",
    "mission-control/e2e",
    "test-manifest.yaml",
]

BASELINE_PATH = "scripts/source-naming-baseline.json"

CODE_EXTENSIONS = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
    ".sh": "shell",
    ".bash": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
}

VERSION_TOKEN = re.compile(r"^[vV][0-9]+$")
COMMENT_VERSION_TOKEN = re.compile(r"(?<![A-Za-z0-9])[vV][0-9]+(?![A-Za-z0-9])")
IDENTIFIER = re.compile(r"[$A-Za-z_][$A-Za-z0-9_]*")
TRAILING_DIGITS = re.compile(r"([A-Za-z]+)([0-9]+)$")

# Technical terms that end in digits without naming a contract version.
DIGIT_TERM_ALLOWLIST = {
    "adler32", "aes128", "aes256", "argon2", "atan2", "base16", "base32",
    "base64", "base85", "blake2", "blake3", "chacha20", "complex64",
    "complex128", "crc32", "curve25519", "ed25519", "float16", "float32",
    "copy2", "float64", "hour12", "http2", "http3", "int8", "int16",
    "int32", "int64", "ipv4", "ipv6", "iso8601", "latin1", "log2",
    "log10", "log1p", "md5", "oauth2", "poly1305", "python3",
    "rfc3339", "rfc9110", "secp256k1", "sqlite3",
    "sha1", "sha3", "sha224", "sha256", "sha384", "sha512", "uint8",
    "uint16", "uint32", "uint64", "utf8", "utf16", "utf32", "uuid1",
    "uuid3", "uuid4", "uuid5", "x25519",
}


def identifier_violations(identifier: str) -> list[str]:
    """Return the rule names that an identifier breaks."""
    violations = []
    for segment in manifestlib._identifier_segments(identifier):
        if VERSION_TOKEN.match(segment):
            violations.append("version-token")
            break
    if "version-token" not in violations:
        last_token = re.split(r"[._\-]", identifier)[-1]
        match = TRAILING_DIGITS.search(last_token)
        if match is not None:
            stem, digits = match.groups()
            term = (stem + digits).lower()
            if term not in DIGIT_TERM_ALLOWLIST and not (
                len(stem) == 1 and stem.lower() != "v"
            ):
                violations.append("numeric-suffix")
        elif re.fullmatch(r"[0-9]+", last_token):
            tokens = re.split(r"[._\-]", identifier)
            stem = tokens[-2] if len(tokens) > 1 else ""
            term = (stem + last_token).lower()
            if stem and term not in DIGIT_TERM_ALLOWLIST and len(stem) > 1:
                violations.append("numeric-suffix")
    return violations


def scan_identifier(findings: Counter, identifier: str) -> None:
    for rule in identifier_violations(identifier):
        findings[("identifier", f"{identifier} ({rule})")] += 1


def scan_comment(findings: Counter, comment: str) -> None:
    for token in COMMENT_VERSION_TOKEN.findall(comment):
        findings[("comment", f"{token} (version-token)")] += 1


def scan_python(findings: Counter, data: bytes) -> None:
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(data).readline))
    except (tokenize.TokenError, SyntaxError, UnicodeDecodeError, IndentationError):
        scan_generic(findings, data)
        return
    for token in tokens:
        if token.type == tokenize.NAME:
            scan_identifier(findings, token.string)
        elif token.type == tokenize.COMMENT:
            scan_comment(findings, token.string)


def scan_typescript(findings: Counter, data: bytes) -> None:
    text = data.decode("utf-8", "replace")
    code, comments = _split_code_and_comments(text, comment_styles=("slash",))
    for identifier in IDENTIFIER.findall(code):
        scan_identifier(findings, identifier)
    for comment in comments:
        scan_comment(findings, comment)


def scan_shell(findings: Counter, data: bytes) -> None:
    text = data.decode("utf-8", "replace")
    code, comments = _split_code_and_comments(text, comment_styles=("hash",))
    for identifier in IDENTIFIER.findall(code):
        scan_identifier(findings, identifier)
    for comment in comments:
        scan_comment(findings, comment)


def _split_code_and_comments(
    text: str, comment_styles: tuple[str, ...]
) -> tuple[str, list[str]]:
    """Split source text into string-free code and a comment list."""
    code: list[str] = []
    comments: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                if quote != "`" and text[index] == "\n":
                    break
                index += 1
            continue
        if "slash" in comment_styles and text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end == -1 else end
            comments.append(text[index:end])
            index = end
            continue
        if "slash" in comment_styles and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            comments.append(text[index:end])
            index = end
            continue
        if "hash" in comment_styles and character == "#":
            end = text.find("\n", index)
            end = length if end == -1 else end
            comments.append(text[index:end])
            index = end
            continue
        code.append(character)
        index += 1
    return "".join(code), comments


def scan_yaml(findings: Counter, data: bytes) -> None:
    """Scan YAML mapping keys and comments. Scalar values stay exempt."""
    key_pattern = re.compile(r"^\s*(?:-\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*:(?:\s|$)")
    for line in data.decode("utf-8", "replace").splitlines():
        without_strings, comments = _split_code_and_comments(line, comment_styles=("hash",))
        for comment in comments:
            scan_comment(findings, comment)
        match = key_pattern.match(without_strings)
        if match is not None:
            scan_identifier(findings, match.group(1))


def scan_json(findings: Counter, data: bytes) -> None:
    """Scan JSON object keys. Values stay exempt as metadata."""
    try:
        document = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        scan_generic(findings, data)
        return

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                scan_identifier(findings, key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)


def scan_generic(findings: Counter, data: bytes) -> None:
    for identifier in IDENTIFIER.findall(data.decode("utf-8", "replace")):
        scan_identifier(findings, identifier)


SCANNERS = {
    "python": scan_python,
    "typescript": scan_typescript,
    "shell": scan_shell,
    "yaml": scan_yaml,
    "json": scan_json,
}


def scan_file_name(findings: Counter, relative_path: str) -> None:
    for part in Path(relative_path).parts:
        stem = part.rsplit(".", 1)[0] if "." in part else part
        for rule in identifier_violations(stem):
            findings[("filename", f"{part} ({rule})")] += 1


def files_to_scan(repo_root: Path, roots: list[str]) -> list[str]:
    """List the source files under the scanned roots.

    Inside a git repository the file list comes from git, so ignored
    files never enter the scan. Outside a git repository the list comes
    from a directory walk.
    """
    inside_git = (
        subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
        ).returncode
        == 0
    )
    paths: list[str] = []
    if inside_git:
        raw = subprocess.run(
            [
                "git", "-C", str(repo_root), "ls-files",
                "--cached", "--others", "--exclude-standard", "-z", "--", *roots,
            ],
            capture_output=True,
            check=True,
        ).stdout
        paths = [p for p in raw.decode("utf-8").split("\0") if p]
    else:
        for root in roots:
            root_path = repo_root / root
            if root_path.is_file():
                paths.append(root)
            elif root_path.is_dir():
                paths.extend(
                    p.relative_to(repo_root).as_posix()
                    for p in root_path.rglob("*")
                    if p.is_file()
                )
    return sorted(set(p for p in paths if (repo_root / p).is_file()))


def scan_repository(
    repo_root: Path, roots: list[str], baseline_relative: str = BASELINE_PATH
) -> dict[str, Counter]:
    findings_by_file: dict[str, Counter] = {}
    for relative_path in files_to_scan(repo_root, roots):
        if relative_path == baseline_relative:
            continue
        findings: Counter = Counter()
        scan_file_name(findings, relative_path)
        language = CODE_EXTENSIONS.get(Path(relative_path).suffix.lower())
        if language is not None:
            SCANNERS[language](findings, (repo_root / relative_path).read_bytes())
        if findings:
            findings_by_file[relative_path] = findings
    return findings_by_file


def findings_to_baseline(findings_by_file: dict[str, Counter]) -> dict:
    return {
        "files": {
            path: [
                {"kind": kind, "token": token, "count": count}
                for (kind, token), count in sorted(findings.items())
            ]
            for path, findings in sorted(findings_by_file.items())
        }
    }


def load_baseline(baseline_path: Path) -> dict[str, Counter]:
    if not baseline_path.is_file():
        return {}
    document = manifestlib.load_json_text(baseline_path.read_text(encoding="utf-8"))
    baseline: dict[str, Counter] = {}
    for path, entries in document.get("files", {}).items():
        counter: Counter = Counter()
        for entry in entries:
            counter[(entry["kind"], entry["token"])] = entry["count"]
        baseline[path] = counter
    return baseline


def compare(
    findings_by_file: dict[str, Counter], baseline: dict[str, Counter]
) -> tuple[list[str], list[str]]:
    new_violations: list[str] = []
    stale_entries: list[str] = []
    all_paths = sorted(set(findings_by_file) | set(baseline))
    for path in all_paths:
        current = findings_by_file.get(path, Counter())
        frozen = baseline.get(path, Counter())
        for key in sorted(set(current) | set(frozen)):
            kind, token = key
            current_count = current.get(key, 0)
            frozen_count = frozen.get(key, 0)
            if current_count > frozen_count:
                new_violations.append(
                    f"{path}: {kind} {token}: {current_count - frozen_count} new "
                    f"occurrence(s) beyond the frozen baseline of {frozen_count}"
                )
            elif current_count < frozen_count:
                stale_entries.append(
                    f"{path}: {kind} {token}: the baseline lists {frozen_count} "
                    f"but the source now holds {current_count}"
                )
    return new_violations, stale_entries


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="Override the scanned roots. Repeat the flag for each root.",
    )
    parser.add_argument("--baseline", default=BASELINE_PATH)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Rewrite the baseline from the current scan after a reviewed cleanup.",
    )
    arguments = parser.parse_args(argv)

    repo_root = (
        Path(arguments.repo_root).resolve()
        if arguments.repo_root
        else Path(__file__).resolve().parent.parent
    )
    roots = arguments.roots or SCAN_ROOTS
    baseline_path = repo_root / arguments.baseline

    findings_by_file = scan_repository(repo_root, roots, arguments.baseline)

    if arguments.update_baseline:
        document = findings_to_baseline(findings_by_file)
        baseline_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        total = sum(sum(c.values()) for c in findings_by_file.values())
        print(f"Baseline updated: {len(findings_by_file)} files, {total} frozen findings.")
        return 0

    baseline = load_baseline(baseline_path)
    new_violations, stale_entries = compare(findings_by_file, baseline)

    if new_violations:
        print("FAIL: new version tokens or numeric version suffixes:", file=sys.stderr)
        for message in new_violations:
            print(f"  - {message}", file=sys.stderr)
        print(
            "Fix: use a stable semantic name and store the contract version in an "
            "explicit metadata value. Freeze an unavoidable external-library "
            "identifier with --update-baseline after review.",
            file=sys.stderr,
        )
    if stale_entries:
        print("FAIL: the baseline is stale:", file=sys.stderr)
        for message in stale_entries:
            print(f"  - {message}", file=sys.stderr)
        print(
            "Fix: run python3 scripts/check-source-naming.py --update-baseline and "
            "commit the smaller baseline.",
            file=sys.stderr,
        )
    if new_violations or stale_entries:
        return 1

    frozen_total = sum(sum(c.values()) for c in baseline.values())
    print(
        f"PASS: no new version tokens or numeric version suffixes; "
        f"{frozen_total} frozen legacy findings remain in the baseline."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
