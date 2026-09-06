"""The agent's vendored protocol modules stay equal to the daemon modules.

The agent process cannot import the daemon package, so it carries a
copy of the digest profile and the signing module. One implementation
must produce the bytes both sides sign, so the copies stay identical
except for the one relative import the package needs.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAIRS = (
    ("daemon/src/core/digest_profile.py", "agent/bmas_protocol/digest_profile.py", {}),
    (
        "daemon/src/core/signing.py",
        "agent/bmas_protocol/signing.py",
        {"from core.digest_profile import canonicalize": "from .digest_profile import canonicalize"},
    ),
)


def test_vendored_modules_match_the_daemon() -> None:
    for daemon_path, agent_path, rewrites in PAIRS:
        expected = (REPO_ROOT / daemon_path).read_text()
        for source, target in rewrites.items():
            assert source in expected, source
            expected = expected.replace(source, target)
        assert (REPO_ROOT / agent_path).read_text() == expected, agent_path
