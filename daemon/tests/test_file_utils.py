# /opt/bmas/daemon/tests/test_file_utils.py
"""
Tests for file_utils.py — sanitization, path traversal, slug, sha256.

Security-critical tests: these verify the trust boundary between
user-supplied filenames/paths and the filesystem.
"""

import os

# Add parent src dir to path for imports
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from file_utils import (
    compute_sha256,
    extract_text_file,
    sanitize_filename,
    slugify_task,
    validate_path_traversal,
)

# ── sanitize_filename ────────────────────────────────────────────────

class TestSanitizeFilename:
    def test_simple_filename(self):
        assert sanitize_filename("report.pdf") == "report.pdf"

    def test_strips_directory_traversal(self):
        result = sanitize_filename("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result
        assert result  # not empty

    def test_strips_windows_traversal(self):
        result = sanitize_filename("..\\..\\windows\\system32\\cmd.exe")
        assert ".." not in result

    def test_strips_absolute_path_unix(self):
        result = sanitize_filename("/etc/passwd")
        assert result == "passwd"

    def test_strips_absolute_path_windows(self):
        result = sanitize_filename("C:\\Users\\evil\\file.txt")
        assert "C:" not in result
        assert "Users" not in result

    def test_strips_null_bytes(self):
        result = sanitize_filename("file\x00.pdf")
        assert "\x00" not in result
        assert result.endswith(".pdf")

    def test_unicode_normalization(self):
        # NFC normalization: é (U+0065 U+0301) → é (U+00E9)
        result = sanitize_filename("cafe\u0301.pdf")
        assert result  # non-empty
        assert result.endswith(".pdf")

    def test_replaces_unsafe_chars(self):
        result = sanitize_filename("my file (v2).pdf")
        assert " " not in result
        assert "(" not in result
        assert ")" not in result

    def test_truncates_long_names(self):
        long_name = "a" * 300 + ".pdf"
        result = sanitize_filename(long_name)
        assert len(result) <= 200

    def test_empty_filename_raises(self):
        with pytest.raises(ValueError):
            sanitize_filename("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            sanitize_filename("   ")

    def test_dotfile_stripped(self):
        result = sanitize_filename(".hidden")
        assert not result.startswith(".")

    def test_preserves_extension(self):
        result = sanitize_filename("my-report.csv")
        assert result.endswith(".csv")

    def test_double_extension(self):
        result = sanitize_filename("malware.pdf.exe")
        # Should keep the full name but sanitize
        assert "exe" in result


# ── validate_path_traversal ──────────────────────────────────────────

class TestValidatePathTraversal:
    def setup_method(self):
        self.base_dir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def test_accepts_valid_path(self):
        result = validate_path_traversal("src/main.py", self.base_dir)
        assert result.startswith(os.path.realpath(self.base_dir))

    def test_accepts_simple_filename(self):
        result = validate_path_traversal("readme.txt", self.base_dir)
        assert result.endswith("readme.txt")

    def test_rejects_dotdot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_path_traversal("../evil.txt", self.base_dir)

    def test_rejects_nested_dotdot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_path_traversal("src/../../evil.txt", self.base_dir)

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="Absolute"):
            validate_path_traversal("/etc/passwd", self.base_dir)

    def test_rejects_empty_path(self):
        with pytest.raises(ValueError, match="empty"):
            validate_path_traversal("", self.base_dir)

    def test_rejects_null_bytes(self):
        with pytest.raises(ValueError, match="null"):
            validate_path_traversal("file\x00.txt", self.base_dir)

    def test_rejects_symlink(self):
        # Create a symlink inside base_dir pointing outside
        link_path = os.path.join(self.base_dir, "link")
        target = "/tmp"
        os.symlink(target, link_path)

        with pytest.raises(ValueError, match="[Ss]ymlink|escapes|outside"):
            validate_path_traversal("link/evil.txt", self.base_dir)

    def test_accepts_nested_valid_path(self):
        result = validate_path_traversal("a/b/c/file.txt", self.base_dir)
        expected = os.path.join(os.path.realpath(self.base_dir), "a", "b", "c", "file.txt")
        assert result == expected

    def test_rejects_windows_backslash_traversal(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_path_traversal("..\\evil.txt", self.base_dir)


# ── slugify_task ─────────────────────────────────────────────────────

class TestSlugifyTask:
    def test_simple_title(self):
        assert slugify_task("Create a CLI Todo App") == "create-a-cli-todo-app"

    def test_truncates_at_60(self):
        long_title = "a" * 100
        result = slugify_task(long_title)
        assert len(result) <= 60

    def test_removes_special_chars(self):
        result = slugify_task("Build a C++ parser (v2)")
        assert "+" not in result
        assert "(" not in result

    def test_empty_title(self):
        result = slugify_task("")
        assert result == "task"

    def test_collapses_dashes(self):
        result = slugify_task("hello   world")
        assert "--" not in result


# ── compute_sha256 ───────────────────────────────────────────────────

class TestComputeSha256:
    def test_known_hash(self):
        result = compute_sha256(b"hello world")
        assert result == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    def test_empty_content(self):
        result = compute_sha256(b"")
        assert result == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_consistent(self):
        data = b"test data for hashing"
        assert compute_sha256(data) == compute_sha256(data)


# ── extract_text_file ────────────────────────────────────────────────

class TestExtractTextFile:
    def test_simple_text(self):
        result = extract_text_file(b"Hello, world!")
        assert result == "Hello, world!"

    def test_truncates_at_cap(self):
        data = b"x" * 100
        result = extract_text_file(data, max_chars=50)
        assert len(result) <= 80  # 50 + truncation marker
        assert "truncated" in result

    def test_empty_file(self):
        result = extract_text_file(b"")
        assert result == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
