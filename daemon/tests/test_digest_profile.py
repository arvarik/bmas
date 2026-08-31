"""Foundation Stage 0C: the canonical bmas-digest profile.

The frozen fixture vectors at ``conformance/digest_profile`` prove that
every supported language and host produces byte-identical canonical
text and digests. The Mission Control test suite replays the same
vectors in TypeScript.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.digest_profile import (
    DIGEST_PROFILE,
    DIGEST_PROFILE_VERSION,
    MAX_SAFE_INTEGER,
    MIN_SAFE_INTEGER,
    DigestInputError,
    canonicalize,
    digest_bytes,
    digest_hex,
    digest_input_bytes,
    digest_value,
    parse_digest_input,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "conformance" / "digest_profile" / "fixtures.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="ascii"))

VECTORS = {vector["name"]: vector for vector in FIXTURE["vectors"]}


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_frozen_vector_reproduces(name: str) -> None:
    vector = VECTORS[name]
    assert canonicalize(vector["input"]) == vector["canonical"]
    assert digest_hex(vector["domain"], vector["input"]) == vector["sha256"]


def test_fixture_metadata_is_explicit() -> None:
    assert FIXTURE["metadata"]["digest_profile_version"] == (
        DIGEST_PROFILE_VERSION
    )
    assert len(FIXTURE["vectors"]) >= 10


def test_digest_record_carries_complete_metadata() -> None:
    record = digest_value("digest-fixture", {"a": 1}, key_id="key-primary")
    assert record.profile == DIGEST_PROFILE
    assert record.profile_version == DIGEST_PROFILE_VERSION
    assert record.domain == "digest-fixture"
    assert record.algorithm == "sha256"
    assert len(record.value) == 64
    assert record.key_id == "key-primary"
    assert record.to_dict()["key_id"] == "key-primary"
    assert "key_id" not in digest_value("digest-fixture", {"a": 1}).to_dict()


def test_domain_framing_separates_equal_content() -> None:
    value = {"same": "content"}
    assert digest_hex("policy-set", value) != digest_hex(
        "asset-manifest", value,
    )
    framed = digest_input_bytes("policy-set", value)
    assert framed.startswith(b"bmas:policy-set\x00")


def test_key_sorting_uses_utf16_code_units() -> None:
    astral = "\U0001f600"
    halfwidth = "｡"
    canonical = canonicalize({halfwidth: 1, astral: 2})
    assert canonical.index(astral) < canonical.index(halfwidth)


def test_unicode_is_never_normalized() -> None:
    decomposed = "é"
    precomposed = "é"
    assert canonicalize(decomposed) != canonicalize(precomposed)
    assert digest_hex("digest-fixture", decomposed) != digest_hex(
        "digest-fixture", precomposed,
    )


def test_numbers_outside_the_safe_range_are_rejected() -> None:
    assert canonicalize(MAX_SAFE_INTEGER) == "9007199254740991"
    assert canonicalize(MIN_SAFE_INTEGER) == "-9007199254740991"
    with pytest.raises(DigestInputError):
        canonicalize(MAX_SAFE_INTEGER + 1)
    with pytest.raises(DigestInputError):
        canonicalize(MIN_SAFE_INTEGER - 1)


def test_non_integer_numbers_are_rejected() -> None:
    with pytest.raises(DigestInputError):
        canonicalize(1.5)
    with pytest.raises(DigestInputError):
        canonicalize(10.0)
    with pytest.raises(DigestInputError):
        parse_digest_input("[1.5]")
    with pytest.raises(DigestInputError):
        parse_digest_input("[1e3]")
    with pytest.raises(DigestInputError):
        parse_digest_input("[NaN]")


def test_duplicate_keys_are_rejected() -> None:
    with pytest.raises(DigestInputError, match="Duplicate"):
        parse_digest_input('{"a": 1, "a": 2}')
    parsed = parse_digest_input('{"a": 1, "b": {"c": 2}}')
    assert parsed == {"a": 1, "b": {"c": 2}}


def test_invalid_unicode_is_rejected() -> None:
    lone_surrogate = "\ud800"
    with pytest.raises(DigestInputError):
        canonicalize(lone_surrogate)
    with pytest.raises(DigestInputError):
        canonicalize({lone_surrogate: 1})


def test_unsupported_types_are_rejected() -> None:
    with pytest.raises(DigestInputError):
        canonicalize({"key": object()})
    with pytest.raises(DigestInputError):
        canonicalize({1: "non-string key"})
    with pytest.raises(DigestInputError):
        digest_hex("Invalid Domain", {})


def test_byte_digests_frame_raw_content() -> None:
    first = digest_bytes("artifact-content", b"payload")
    second = digest_bytes("artifact-content", b"payload")
    other_domain = digest_bytes("digest-fixture", b"payload")
    assert first == second
    assert first != other_domain
    assert len(first) == 64
