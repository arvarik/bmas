"""Foundation Stage 0G: keyed lookup digests and the digest audit.

The semantic text transform applies NFC and LF without other
whitespace changes and never replaces the exact digest. Keyed
HMAC-SHA-256 digests use one per-tenant key with recorded key
identifiers, rotation queries the active and previous epochs, no
keyed digest reaches a public export, and every persisted digest
passes the audit for its profile, domain, algorithm, value, and
optional key identifier.
"""
from __future__ import annotations

import pytest

from core.digest_profile import (
    DigestInputError,
    canonicalize,
    digest_hex,
    digest_value,
)
from core.keyed_digest import (
    KeyedDigestError,
    TenantKeyRing,
    audit_digest_record,
    export_digest_for_public_view,
    keyed_digest,
    keyed_lookup_values,
    semantic_text,
)

# ── The semantic text transform ──────────────────────────────────────


def test_semantic_text_applies_nfc_and_lf_only():
    decomposed = "café \t line\r\nnext\rlast  spaced"
    transformed = semantic_text(decomposed)
    assert "café" in transformed
    assert "\r" not in transformed
    assert "line\nnext\nlast" in transformed
    # Every other whitespace character stays exactly in place.
    assert " \t line" in transformed
    assert "last  spaced" in transformed


def test_canonically_equivalent_strings_keep_distinct_exact_digests():
    composed = "café"
    decomposed = "café"
    # RFC 8785 does not normalize Unicode, so the exact digests differ.
    assert canonicalize(composed) != canonicalize(decomposed)
    assert digest_hex("exact-text", composed) != digest_hex(
        "exact-text", decomposed,
    )
    # The separate semantic values compare equal.
    assert semantic_text(composed) == semantic_text(decomposed)


def test_the_semantic_value_never_replaces_the_exact_digest():
    original = "value\r\n"
    transformed = semantic_text(original)
    assert transformed != original
    assert digest_hex("exact-text", original) != digest_hex(
        "exact-text", transformed,
    )


# ── Keyed equality digests ───────────────────────────────────────────


def make_ring() -> TenantKeyRing:
    ring = TenantKeyRing()
    ring.install_key("tenant-a", "hmac-key-1", b"a" * 32)
    return ring


def test_keyed_digests_record_their_key_identifier():
    ring = make_ring()
    record = keyed_digest(ring, "tenant-a", "email-lookup", "user@example.com")
    assert record.algorithm == "hmac-sha256"
    assert record.key_id == "hmac-key-1"
    assert len(record.value) == 64
    audit_digest_record(record.to_dict())


def test_keyed_digests_differ_by_tenant_key_and_domain():
    ring = make_ring()
    ring.install_key("tenant-b", "hmac-key-b", b"b" * 32)
    same_value = "user@example.com"
    first = keyed_digest(ring, "tenant-a", "email-lookup", same_value)
    other_tenant = keyed_digest(ring, "tenant-b", "email-lookup", same_value)
    other_domain = keyed_digest(ring, "tenant-a", "phone-lookup", same_value)
    assert first.value != other_tenant.value
    assert first.value != other_domain.value


def test_rotation_queries_the_active_and_previous_epochs():
    ring = make_ring()
    before = keyed_digest(ring, "tenant-a", "email-lookup", "user@example.com")
    ring.install_key("tenant-a", "hmac-key-2", b"c" * 32)
    lookups = keyed_lookup_values(
        ring, "tenant-a", "email-lookup", "user@example.com",
    )
    assert [record.key_id for record in lookups] == [
        "hmac-key-1", "hmac-key-2",
    ]
    # A record written under the previous key still matches.
    assert before.value in {record.value for record in lookups}
    # New digests use the active key.
    active = keyed_digest(ring, "tenant-a", "email-lookup", "user@example.com")
    assert active.key_id == "hmac-key-2"


def test_weak_keys_and_unknown_tenants_fail_closed():
    ring = TenantKeyRing()
    with pytest.raises(KeyedDigestError):
        ring.install_key("tenant-a", "weak", b"short")
    with pytest.raises(KeyedDigestError):
        ring.active_epoch("tenant-missing")
    with pytest.raises(KeyedDigestError):
        keyed_lookup_values(ring, "tenant-missing", "domain", "value")


def test_no_keyed_digest_reaches_a_public_export():
    ring = make_ring()
    keyed = keyed_digest(ring, "tenant-a", "email-lookup", "user@example.com")
    exported = export_digest_for_public_view(keyed.to_dict())
    assert "value" not in exported
    assert "key_id" not in exported
    assert exported["redacted"] == "keyed_digest_withheld"
    plain = digest_value("artifact-content", {"public": True})
    exported_plain = export_digest_for_public_view(plain.to_dict())
    assert exported_plain["value"] == plain.value


# ── The digest audit ─────────────────────────────────────────────────


def test_the_audit_accepts_complete_declared_records():
    record = digest_value("policy-set", {"a": 1})
    audit_digest_record(record.to_dict())


def test_the_audit_rejects_incomplete_or_mixed_records():
    good = digest_value("policy-set", {"a": 1}).to_dict()
    for missing in ("profile", "profile_version", "domain",
                    "algorithm", "value"):
        broken = dict(good)
        broken.pop(missing)
        with pytest.raises(KeyedDigestError):
            audit_digest_record(broken)
    with pytest.raises(KeyedDigestError):
        audit_digest_record({**good, "profile": "other-profile"})
    with pytest.raises(KeyedDigestError):
        audit_digest_record({**good, "algorithm": "md5"})
    # An unkeyed digest never carries a key identifier, and a keyed
    # digest always does.
    with pytest.raises(KeyedDigestError):
        audit_digest_record({**good, "key_id": "hmac-key-1"})
    with pytest.raises(KeyedDigestError):
        audit_digest_record({**good, "algorithm": "hmac-sha256"})


# ── Cross-language digest fixture behavior ───────────────────────────


def test_domain_prefix_changes_the_digest_for_equal_payloads():
    payload = {"value": "same"}
    assert digest_hex("domain-one", payload) != digest_hex(
        "domain-two", payload,
    )


def test_strict_rejection_of_invalid_digest_inputs():
    from core.digest_profile import parse_digest_input

    with pytest.raises(DigestInputError):
        parse_digest_input('{"a": 1, "a": 2}')
    with pytest.raises(DigestInputError):
        parse_digest_input('{"a": 1.5}')
    with pytest.raises(DigestInputError):
        canonicalize({"a": 2**60})
    # Alternate member order canonicalizes to equal output.
    assert canonicalize({"b": 1, "a": 2}) == canonicalize({"a": 2, "b": 1})
