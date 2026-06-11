# /opt/bmas/daemon/src/file_utils.py
"""
File & artifact utilities — shared by upload and artifact routes.

Security-critical: every function in this module is on the trust boundary
between user-supplied filenames/paths and the filesystem. All path operations
use os.path.realpath() + startswith() with trailing os.sep to prevent
traversal attacks.
"""

import hashlib
import os
import re
import unicodedata


# ── Filename Sanitization ────────────────────────────────────────────

# Characters allowed in sanitized filenames
_SAFE_CHARS = re.compile(r"[^a-zA-Z0-9._\-]")
_COLLAPSE_DASHES = re.compile(r"-{2,}")
_MAX_FILENAME_LEN = 200


def sanitize_filename(name: str) -> str:
    """Sanitize a user-provided filename to a safe filesystem name.

    - Strips directory components (only basename)
    - Removes null bytes
    - NFC-normalizes unicode
    - Replaces unsafe chars with dashes
    - Truncates to 200 chars (preserving extension)
    - Raises ValueError if result is empty or starts with a dot

    >>> sanitize_filename("../../etc/passwd")
    'etc-passwd'
    >>> sanitize_filename("report (final).pdf")
    'report-final-.pdf'
    """
    if not name or not name.strip():
        raise ValueError("Filename is empty")

    # Strip null bytes
    name = name.replace("\x00", "")

    # NFC normalization to collapse multi-codepoint sequences
    name = unicodedata.normalize("NFC", name)

    # Take only the basename — strip any directory components
    # Handle both Unix and Windows separators
    name = name.replace("\\", "/")
    name = os.path.basename(name)

    if not name:
        raise ValueError("Filename resolves to empty after stripping directories")

    # Split off extension for preservation
    base, ext = os.path.splitext(name)

    # Replace unsafe characters with dashes
    base = _SAFE_CHARS.sub("-", base)
    ext = _SAFE_CHARS.sub("-", ext)

    # Collapse multiple dashes
    base = _COLLAPSE_DASHES.sub("-", base).strip("-")

    # Re-add the dot for extension
    if ext and ext != ".":
        # ext already has the dot from splitext, but we sanitized it
        # Restore leading dot
        ext = "." + ext.lstrip(".-")
    else:
        ext = ""

    if not base:
        raise ValueError("Filename resolves to empty after sanitization")

    # Truncate base to leave room for extension
    max_base = _MAX_FILENAME_LEN - len(ext)
    if max_base < 1:
        raise ValueError("Extension too long")
    base = base[:max_base]

    result = base + ext

    # Reject hidden files (dotfiles)
    if result.startswith("."):
        result = result.lstrip(".")
        if not result:
            raise ValueError("Filename is a dotfile")

    return result


# ── Path Traversal Guard ─────────────────────────────────────────────

def validate_path_traversal(rel_path: str, base_dir: str) -> str:
    """Validate that rel_path stays within base_dir.

    Returns the safe, resolved absolute path.
    Raises ValueError if the path escapes the base directory or contains
    traversal sequences, absolute paths, or symlinks.
    """
    if not rel_path:
        raise ValueError("Path is empty")

    # Reject null bytes
    if "\x00" in rel_path:
        raise ValueError("Path contains null bytes")

    # Reject absolute paths
    if os.path.isabs(rel_path):
        raise ValueError(f"Absolute paths not allowed: {rel_path}")

    # Reject explicit traversal
    # Normalize separators first
    normalized = rel_path.replace("\\", "/")
    parts = normalized.split("/")
    for part in parts:
        if part == "..":
            raise ValueError(f"Path traversal detected: {rel_path}")

    # Resolve the base directory (must exist)
    real_base = os.path.realpath(base_dir)
    if not real_base.endswith(os.sep):
        real_base += os.sep

    # Join and resolve the full path
    candidate = os.path.realpath(os.path.join(base_dir, rel_path))

    # Verify it's within the base directory (trailing sep prevents partial match)
    if not candidate.startswith(real_base) and candidate != real_base.rstrip(os.sep):
        raise ValueError(
            f"Path escapes base directory: {rel_path} resolves to {candidate}, "
            f"which is outside {real_base}"
        )

    # Reject symlinks in the resolved path
    # Walk each component to check for intermediate symlinks
    check_path = base_dir
    for part in parts:
        check_path = os.path.join(check_path, part)
        if os.path.islink(check_path):
            raise ValueError(f"Symlinks not allowed in path: {rel_path}")

    return candidate


# ── Task Slug Generation ─────────────────────────────────────────────

_SLUG_CHARS = re.compile(r"[^a-z0-9-]")
_SLUG_COLLAPSE = re.compile(r"-{2,}")
_MAX_SLUG_LEN = 60


def slugify_task(title: str) -> str:
    """Convert a task title to a filesystem-safe slug.

    First 60 chars → lowercase → [a-z0-9-] only → collapse dashes.

    >>> slugify_task("Create a CLI Todo App")
    'create-a-cli-todo-app'
    """
    slug = title[:_MAX_SLUG_LEN].lower().strip()
    slug = _SLUG_CHARS.sub("-", slug)
    slug = _SLUG_COLLAPSE.sub("-", slug).strip("-")
    return slug or "task"


def resolve_slug_collision(artifacts_dir: str, slug: str) -> str:
    """Append -2, -3, etc. if the slug directory already exists.

    Returns the first available slug.
    """
    candidate = slug
    counter = 2
    while os.path.exists(os.path.join(artifacts_dir, candidate)):
        candidate = f"{slug}-{counter}"
        counter += 1
    return candidate


# ── SHA-256 ──────────────────────────────────────────────────────────

def compute_sha256(file_bytes: bytes) -> str:
    """Compute the hex SHA-256 digest of file contents."""
    return hashlib.sha256(file_bytes).hexdigest()


# ── MIME Type Mapping ────────────────────────────────────────────────

_MIME_MAP: dict[str, str] = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def get_mime_type(filename: str) -> str:
    """Get MIME type from filename extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _MIME_MAP.get(ext, "application/octet-stream")


def get_extension(filename: str) -> str:
    """Get the lowercase extension without the dot."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


# ── PDF Text Extraction ─────────────────────────────────────────────

def extract_pdf_text(file_bytes: bytes, max_chars: int = 60000) -> str:
    """Extract text from a PDF using pymupdf, page by page.

    Returns text with [page N] markers. Truncates at max_chars with a
    '[truncated — fetch full file]' marker.

    Returns empty string for image-only/scanned PDFs.
    """
    try:
        import pymupdf  # type: ignore[import-untyped]
    except ImportError:
        return ""

    text_parts: list[str] = []
    total_chars = 0

    try:
        doc = pymupdf.open(stream=file_bytes, filetype="pdf")
        for page_num in range(len(doc)):
            page = doc[page_num]
            page_text = page.get_text("text").strip()
            if not page_text:
                continue

            marker = f"[page {page_num + 1}]\n"
            remaining = max_chars - total_chars - len(marker)

            if remaining <= 0:
                text_parts.append("\n[truncated — fetch full file]")
                break

            if len(page_text) > remaining:
                text_parts.append(marker + page_text[:remaining])
                text_parts.append("\n[truncated — fetch full file]")
                total_chars = max_chars
                break
            else:
                text_parts.append(marker + page_text)
                total_chars += len(marker) + len(page_text)

        doc.close()
    except Exception:
        return ""

    return "\n\n".join(text_parts)


def extract_text_file(file_bytes: bytes, max_chars: int = 60000) -> str:
    """Passthrough extraction for text files (txt, md, csv, json)."""
    try:
        text = file_bytes.decode("utf-8", errors="replace")
    except Exception:
        return ""

    if len(text) > max_chars:
        return text[:max_chars] + "\n[truncated — fetch full file]"
    return text
