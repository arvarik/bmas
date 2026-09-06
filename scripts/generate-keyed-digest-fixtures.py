#!/usr/bin/env python3
"""Generate the keyed, semantic-text, and exact-bytes digest fixtures.

The daemon is the reference implementation of the keyed digest, the
semantic text transform, and the exact content digest. This script
freezes representative vectors so the TypeScript second implementation
reproduces every byte. Run it from the repository root after a change
to ``daemon/src/core/keyed_digest.py`` or ``digest_profile.py`` and
commit the updated fixture file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "daemon" / "src"))

from core.digest_profile import (
    DIGEST_PROFILE,
    DIGEST_PROFILE_VERSION,
    digest_bytes,
)
from core.keyed_digest import (
    KEYED_DIGEST_ALGORITHM,
    KEYED_DIGEST_DOMAIN_PREFIX,
    TenantKeyRing,
    keyed_digest,
    semantic_text,
)

OUTPUT = REPO_ROOT / "daemon/tests/fixtures/keyed_digest.json"
FIXTURE_ID = "keyed-digest"

TENANT_ID = "tenant-fixture"
KEY_ID = "hmac-key-fixture"
KEY_BYTES = bytes(range(32))

SEMANTIC_INPUTS = {
    "plain-ascii": "hello world",
    "empty": "",
    "crlf-and-cr": "line one\r\nline two\rline three\n",
    "nfd-composes-to-nfc": "café näive",
    "nfc-stays": "café naïve",
    "tabs-and-spaces-stay": "a\tb  c  d",
    "emoji-and-cjk": "\U0001f600 日本語 مرحبا",
    "trailing-whitespace-stays": "keep  \n",
}

EXACT_INPUTS = {
    "plain-ascii": ("artifact-content", "hello world"),
    "empty": ("artifact-content", ""),
    "nfd-differs-from-nfc": ("artifact-content", "café"),
    "nfc-differs-from-nfd": ("artifact-content", "café"),
    "domain-separates": ("evidence-final-output", "hello world"),
    "crlf-kept": ("artifact-content", "a\r\nb"),
}

KEYED_INPUTS = {
    "email": ("principal-email", "person@example.org"),
    "empty-value": ("principal-email", ""),
    "unicode-value": ("principal-name", "Zoë Østergård"),
    "other-domain": ("api-key", "person@example.org"),
    "long-value": ("free-text", "x" * 300),
}


def build() -> dict:
    ring = TenantKeyRing()
    ring.install_key(TENANT_ID, KEY_ID, KEY_BYTES)
    semantic = [
        {"name": name, "input": value, "semantic_text": semantic_text(value)}
        for name, value in sorted(SEMANTIC_INPUTS.items())
    ]
    exact = [
        {
            "name": name,
            "domain": domain,
            "input": value,
            "sha256": digest_bytes(domain, value.encode("utf-8")),
        }
        for name, (domain, value) in sorted(EXACT_INPUTS.items())
    ]
    keyed = []
    for name, (domain, value) in sorted(KEYED_INPUTS.items()):
        record = keyed_digest(ring, TENANT_ID, domain, value)
        keyed.append({
            "name": name,
            "domain": domain,
            "value": value,
            "key_id": record.key_id,
            "hmac_sha256": record.value,
        })
    return {
        "fixture_id": FIXTURE_ID,
        "metadata": {
            "digest_profile": DIGEST_PROFILE,
            "digest_profile_version": DIGEST_PROFILE_VERSION,
            "keyed_algorithm": KEYED_DIGEST_ALGORITHM,
            "domain_prefix": KEYED_DIGEST_DOMAIN_PREFIX,
            "tenant_id": TENANT_ID,
            "key_id": KEY_ID,
            "key_hex": KEY_BYTES.hex(),
        },
        "semantic_text": semantic,
        "exact_bytes": exact,
        "keyed": keyed,
    }


def main() -> None:
    document = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=1, sort_keys=True, ensure_ascii=True) + "\n")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
