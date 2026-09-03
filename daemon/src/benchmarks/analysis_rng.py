"""The portable ``bmas-analysis-rng`` random algorithm.

Every bootstrap draw derives from SHA-256 over one exact input: the
algorithm name with a NUL terminator, the algorithm version, the
master seed, the analysis input digest, the replicate index, the
family identifier digest, the draw index, and a rejection counter.
The first eight digest bytes form one unsigned 64-bit candidate. A
candidate at or above ``2^64 - (2^64 mod n)`` rejects, the counter
increments only after a rejection, and every accepted candidate
selects ``candidate mod n``. Every language reproduces the same
candidates, rejections, indexes, and draws. The algorithm version is
metadata, never part of an identifier.
"""

from __future__ import annotations

import hashlib
from typing import Any

RNG_ALGORITHM = "bmas-analysis-rng"
RNG_ALGORITHM_VERSION = 1
RNG_IMPLEMENTATION = "sha-256-rejection"

_WORD = 2**64
_SEED_LIMIT = 2**64 - 1
_COUNTER_LIMIT = 2**32 - 1


class AnalysisRngError(ValueError):
    """The draw request violates the algorithm contract."""


def family_digest(family_id: str) -> bytes:
    """Digest one family identifier for the derivation input."""
    return hashlib.sha256(str(family_id).encode("utf-8")).digest()


def candidate(
    *,
    master_seed: int,
    input_digest: bytes,
    replicate_index: int,
    family_id_digest: bytes,
    draw_index: int,
    counter: int,
    algorithm_version: int = RNG_ALGORITHM_VERSION,
) -> int:
    """Derive one unsigned 64-bit candidate from the exact input."""
    if not 0 <= master_seed <= _SEED_LIMIT:
        raise AnalysisRngError(
            "The master seed fits one unsigned 64-bit integer"
        )
    if len(input_digest) != 32 or len(family_id_digest) != 32:
        raise AnalysisRngError("A digest input holds 32 bytes")
    for name, value in (
        ("replicate_index", replicate_index),
        ("draw_index", draw_index),
        ("counter", counter),
        ("algorithm_version", algorithm_version),
    ):
        if not 0 <= value <= _COUNTER_LIMIT:
            raise AnalysisRngError(
                f"The {name} fits one unsigned 32-bit integer"
            )
    payload = (
        RNG_ALGORITHM.encode("utf-8") + b"\x00"
        + algorithm_version.to_bytes(4, "big")
        + master_seed.to_bytes(8, "big")
        + input_digest
        + replicate_index.to_bytes(4, "big")
        + family_id_digest
        + draw_index.to_bytes(4, "big")
        + counter.to_bytes(4, "big")
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def rejection_limit(case_count: int) -> int:
    """Return the first rejected candidate value for one case count."""
    if case_count <= 0:
        raise AnalysisRngError("A draw needs at least one case")
    return _WORD - (_WORD % case_count)


def draw(
    *,
    master_seed: int,
    input_digest: bytes,
    replicate_index: int,
    family_id: str,
    draw_index: int,
    case_count: int,
    algorithm_version: int = RNG_ALGORITHM_VERSION,
) -> dict[str, Any]:
    """Select one uniform case index by rejection sampling.

    The result records every candidate and the rejection count, so a
    second implementation can compare its complete derivation.
    """
    limit = rejection_limit(case_count)
    digest = family_digest(family_id)
    candidates: list[int] = []
    counter = 0
    while True:
        value = candidate(
            master_seed=master_seed,
            input_digest=input_digest,
            replicate_index=replicate_index,
            family_id_digest=digest,
            draw_index=draw_index,
            counter=counter,
            algorithm_version=algorithm_version,
        )
        candidates.append(value)
        if value < limit:
            return {
                "index": value % case_count,
                "candidates": candidates,
                "rejections": counter,
            }
        # The counter advances only after a rejected candidate.
        counter += 1


def replicate_draws(
    *,
    master_seed: int,
    input_digest: bytes,
    replicate_index: int,
    family_id: str,
    case_count: int,
    algorithm_version: int = RNG_ALGORITHM_VERSION,
) -> list[int]:
    """Draw the original case count uniformly with replacement."""
    return [
        draw(
            master_seed=master_seed,
            input_digest=input_digest,
            replicate_index=replicate_index,
            family_id=family_id,
            draw_index=draw_index,
            case_count=case_count,
            algorithm_version=algorithm_version,
        )["index"]
        for draw_index in range(case_count)
    ]


def sign_flip(
    *,
    master_seed: int,
    input_digest: bytes,
    replicate_index: int,
    case_index: int,
    algorithm_version: int = RNG_ALGORITHM_VERSION,
) -> bool:
    """Derive one sign-flip bit through the same derivation schedule.

    The sign-flip schedule uses the reserved family identifier
    ``sign-flip`` and one draw of size two per case.
    """
    return bool(draw(
        master_seed=master_seed,
        input_digest=input_digest,
        replicate_index=replicate_index,
        family_id="sign-flip",
        draw_index=case_index,
        case_count=2,
        algorithm_version=algorithm_version,
    )["index"])


def implementation_digest() -> str:
    """Digest this implementation's source for the analysis metadata."""
    import inspect
    import sys

    module = sys.modules[__name__]
    return hashlib.sha256(
        inspect.getsource(module).encode("utf-8"),
    ).hexdigest()
