"""The frozen keyed digest fixtures reproduce from the reference implementation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.digest_profile import DIGEST_PROFILE_VERSION, digest_bytes
from core.keyed_digest import KEYED_DIGEST_ALGORITHM, TenantKeyRing, keyed_digest, semantic_text

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "keyed_digest.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="ascii"))
METADATA = FIXTURE["metadata"]


def _ring() -> TenantKeyRing:
    ring = TenantKeyRing()
    ring.install_key(METADATA["tenant_id"], METADATA["key_id"], bytes.fromhex(METADATA["key_hex"]))
    return ring


def test_fixture_metadata_is_explicit() -> None:
    assert METADATA["digest_profile_version"] == DIGEST_PROFILE_VERSION
    assert METADATA["keyed_algorithm"] == KEYED_DIGEST_ALGORITHM
    assert len(FIXTURE["semantic_text"]) >= 6
    assert len(FIXTURE["exact_bytes"]) >= 5
    assert len(FIXTURE["keyed"]) >= 4


@pytest.mark.parametrize("vector", FIXTURE["semantic_text"], ids=lambda v: v["name"])
def test_semantic_text_vectors_reproduce(vector: dict) -> None:
    assert semantic_text(vector["input"]) == vector["semantic_text"]


@pytest.mark.parametrize("vector", FIXTURE["exact_bytes"], ids=lambda v: v["name"])
def test_exact_bytes_vectors_reproduce(vector: dict) -> None:
    assert digest_bytes(vector["domain"], vector["input"].encode("utf-8")) == vector["sha256"]


@pytest.mark.parametrize("vector", FIXTURE["keyed"], ids=lambda v: v["name"])
def test_keyed_vectors_reproduce(vector: dict) -> None:
    record = keyed_digest(_ring(), METADATA["tenant_id"], vector["domain"], vector["value"])
    assert record.value == vector["hmac_sha256"]
    assert record.key_id == vector["key_id"]


def test_exact_digests_separate_what_semantic_text_joins() -> None:
    by_name = {vector["name"]: vector for vector in FIXTURE["exact_bytes"]}
    nfd, nfc = by_name["nfd-differs-from-nfc"], by_name["nfc-differs-from-nfd"]
    assert nfd["sha256"] != nfc["sha256"]
    assert semantic_text(nfd["input"]) == semantic_text(nfc["input"])
