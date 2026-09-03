"""The portable ``bmas-analysis-rng`` random algorithm.

The algorithm name never changes; the algorithm version is metadata
that selects one of two published derivations.

Version 1 (``sha-256-rejection``): every bootstrap draw derives from
SHA-256 over one exact input: the algorithm name with a NUL
terminator, the algorithm version, the master seed, the analysis
input digest, the replicate index, the family identifier digest, the
draw index, and a rejection counter. The first eight digest bytes
form one unsigned 64-bit candidate. A candidate at or above
``2^64 - (2^64 mod n)`` rejects, the counter increments only after a
rejection, and every accepted candidate selects ``candidate mod n``.

Version 2 (``keyed-counter-splitmix64``): one SHA-256 over the same
static prefix (name, version, master seed, input digest, family
digest) yields one 64-bit family key. Every draw then mixes one
counter word with the SplitMix64 finalizer, so a vectorized engine
derives a whole replicate in one pass and every language reproduces
the same words. The counter word for a bootstrap draw is
``(replicate_index << 32) | (draw_index >> 1)``; the mixed word
yields two 32-bit candidates, the high half for an even draw index
and the low half for an odd one. A candidate at or above
``2^32 - (2^32 mod n)`` rejects, the rejection counter adds one
constant step to the state, and every accepted candidate selects
``candidate mod n``. The sign-flip schedule mixes the word
``(replicate_index << 32) | (case_index >> 6)`` under the reserved
family ``sign-flip`` and reads bit ``case_index & 63``.

Every language reproduces the same candidates, rejections, indexes,
and draws. The algorithm version is metadata, never part of an
identifier.
"""

from __future__ import annotations

import hashlib
from typing import Any

RNG_ALGORITHM = "bmas-analysis-rng"
RNG_ALGORITHM_VERSION = 2
SUPPORTED_ALGORITHM_VERSIONS = (1, 2)
RNG_IMPLEMENTATIONS = {
    1: "sha-256-rejection",
    2: "keyed-counter-splitmix64",
}
RNG_IMPLEMENTATION = RNG_IMPLEMENTATIONS[RNG_ALGORITHM_VERSION]

_WORD = 2**64
_HALF_WORD = 2**32
_MASK64 = _WORD - 1
_MASK32 = _HALF_WORD - 1
_SEED_LIMIT = 2**64 - 1
_COUNTER_LIMIT = 2**32 - 1

# The SplitMix64 constants: the golden-ratio increment, the two
# finalizer multipliers, and one distinct odd step for rejections.
GOLDEN_GAMMA = 0x9E3779B97F4A7C15
MIX_MULTIPLIER_ONE = 0xBF58476D1CE4E5B9
MIX_MULTIPLIER_TWO = 0x94D049BB133111EB
COUNTER_STEP = 0xD1B54A32D192ED03
SIGN_FLIP_FAMILY = "sign-flip"


class AnalysisRngError(ValueError):
    """The draw request violates the algorithm contract."""


def _check_version(algorithm_version: int) -> None:
    if algorithm_version not in SUPPORTED_ALGORITHM_VERSIONS:
        raise AnalysisRngError(
            f"Unsupported {RNG_ALGORITHM} algorithm version "
            f"{algorithm_version}; supported versions are "
            f"{list(SUPPORTED_ALGORITHM_VERSIONS)}"
        )


def implementation_for(algorithm_version: int) -> str:
    """Name the published derivation behind one algorithm version."""
    _check_version(algorithm_version)
    return RNG_IMPLEMENTATIONS[algorithm_version]


def derivation_schedule(algorithm_version: int) -> list[str]:
    """Describe the derivation schedule of one algorithm version."""
    _check_version(algorithm_version)
    if algorithm_version == 1:
        return [
            "bootstrap:replicate:family:draw:counter",
            "sign-flip:replicate:case:counter",
        ]
    return [
        "bootstrap:family-key:replicate:draw-pair:half:counter",
        "sign-flip:family-key:replicate:case-block:bit",
    ]


def family_digest(family_id: str) -> bytes:
    """Digest one family identifier for the derivation input."""
    return hashlib.sha256(str(family_id).encode("utf-8")).digest()


def _check_inputs(
    *, master_seed: int, input_digest: bytes, family_id_digest: bytes,
    counters: tuple[tuple[str, int], ...], algorithm_version: int,
) -> None:
    _check_version(algorithm_version)
    if not 0 <= master_seed <= _SEED_LIMIT:
        raise AnalysisRngError(
            "The master seed fits one unsigned 64-bit integer"
        )
    if len(input_digest) != 32 or len(family_id_digest) != 32:
        raise AnalysisRngError("A digest input holds 32 bytes")
    for name, value in counters:
        if not 0 <= value <= _COUNTER_LIMIT:
            raise AnalysisRngError(
                f"The {name} fits one unsigned 32-bit integer"
            )


# ── Version 2: the family key and the SplitMix64 finalizer ───────────


def family_key(
    *,
    master_seed: int,
    input_digest: bytes,
    family_id_digest: bytes,
    algorithm_version: int = RNG_ALGORITHM_VERSION,
) -> int:
    """Derive the 64-bit family key of the keyed-counter derivation."""
    _check_inputs(
        master_seed=master_seed, input_digest=input_digest,
        family_id_digest=family_id_digest,
        counters=(("algorithm_version", algorithm_version),),
        algorithm_version=algorithm_version,
    )
    payload = (
        RNG_ALGORITHM.encode("utf-8") + b"\x00"
        + algorithm_version.to_bytes(4, "big")
        + master_seed.to_bytes(8, "big")
        + input_digest
        + family_id_digest
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def mix64(state: int) -> int:
    """Apply the SplitMix64 finalizer to one 64-bit state."""
    z = state & _MASK64
    z = ((z ^ (z >> 30)) * MIX_MULTIPLIER_ONE) & _MASK64
    z = ((z ^ (z >> 27)) * MIX_MULTIPLIER_TWO) & _MASK64
    return z ^ (z >> 31)


def counter_state(key: int, word: int, counter: int) -> int:
    """Combine the family key, one counter word, and the rejection count."""
    return (key + word * GOLDEN_GAMMA + counter * COUNTER_STEP) & _MASK64


def bootstrap_word(replicate_index: int, draw_index: int) -> int:
    """The counter word that carries one pair of bootstrap draws."""
    return (replicate_index << 32) | (draw_index >> 1)


def sign_flip_word(replicate_index: int, case_index: int) -> int:
    """The counter word that carries sixty-four sign-flip bits."""
    return (replicate_index << 32) | (case_index >> 6)


# ── Candidates ───────────────────────────────────────────────────────


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
    """Derive one unsigned candidate from the exact input.

    Version 1 returns one 64-bit candidate. Version 2 returns one
    32-bit candidate, the selected half of the mixed counter word.
    """
    _check_inputs(
        master_seed=master_seed, input_digest=input_digest,
        family_id_digest=family_id_digest,
        counters=(
            ("replicate_index", replicate_index),
            ("draw_index", draw_index),
            ("counter", counter),
            ("algorithm_version", algorithm_version),
        ),
        algorithm_version=algorithm_version,
    )
    if algorithm_version == 1:
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
    key = family_key(
        master_seed=master_seed, input_digest=input_digest,
        family_id_digest=family_id_digest,
        algorithm_version=algorithm_version,
    )
    mixed = mix64(counter_state(
        key, bootstrap_word(replicate_index, draw_index), counter,
    ))
    return (mixed >> 32) if draw_index % 2 == 0 else (mixed & _MASK32)


def candidate_width(algorithm_version: int = RNG_ALGORITHM_VERSION) -> int:
    """The number of candidate values one draw can take."""
    _check_version(algorithm_version)
    return _WORD if algorithm_version == 1 else _HALF_WORD


def rejection_limit(
    case_count: int, algorithm_version: int = RNG_ALGORITHM_VERSION,
) -> int:
    """Return the first rejected candidate value for one case count."""
    if case_count <= 0:
        raise AnalysisRngError("A draw needs at least one case")
    width = candidate_width(algorithm_version)
    return width - (width % case_count)


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
    limit = rejection_limit(case_count, algorithm_version)
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
    if algorithm_version == 1:
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
    limit = rejection_limit(case_count, algorithm_version)
    key = family_key(
        master_seed=master_seed, input_digest=input_digest,
        family_id_digest=family_digest(family_id),
        algorithm_version=algorithm_version,
    )
    indexes: list[int] = []
    mixed = 0
    for draw_index in range(case_count):
        if draw_index % 2 == 0:
            mixed = mix64(counter_state(
                key, bootstrap_word(replicate_index, draw_index), 0,
            ))
            value = mixed >> 32
        else:
            value = mixed & _MASK32
        counter = 0
        while value >= limit:
            counter += 1
            retried = mix64(counter_state(
                key, bootstrap_word(replicate_index, draw_index), counter,
            ))
            value = (retried >> 32) if draw_index % 2 == 0 else (
                retried & _MASK32
            )
        indexes.append(value % case_count)
    return indexes


def sign_flip(
    *,
    master_seed: int,
    input_digest: bytes,
    replicate_index: int,
    case_index: int,
    algorithm_version: int = RNG_ALGORITHM_VERSION,
) -> bool:
    """Derive one sign-flip bit through the version's derivation schedule.

    Version 1 draws one value of size two per case under the reserved
    family ``sign-flip``. Version 2 reads one bit of the mixed
    case-block word under the same reserved family.
    """
    if algorithm_version == 1:
        return bool(draw(
            master_seed=master_seed,
            input_digest=input_digest,
            replicate_index=replicate_index,
            family_id=SIGN_FLIP_FAMILY,
            draw_index=case_index,
            case_count=2,
            algorithm_version=algorithm_version,
        )["index"])
    _check_inputs(
        master_seed=master_seed, input_digest=input_digest,
        family_id_digest=family_digest(SIGN_FLIP_FAMILY),
        counters=(
            ("replicate_index", replicate_index),
            ("case_index", case_index),
        ),
        algorithm_version=algorithm_version,
    )
    key = family_key(
        master_seed=master_seed, input_digest=input_digest,
        family_id_digest=family_digest(SIGN_FLIP_FAMILY),
        algorithm_version=algorithm_version,
    )
    mixed = mix64(counter_state(
        key, sign_flip_word(replicate_index, case_index), 0,
    ))
    return bool((mixed >> (case_index & 63)) & 1)


def replicate_sign_flips(
    *,
    master_seed: int,
    input_digest: bytes,
    replicate_index: int,
    case_count: int,
    algorithm_version: int = RNG_ALGORITHM_VERSION,
) -> list[bool]:
    """Derive every sign-flip bit of one replicate in case order."""
    if algorithm_version == 1:
        return [
            sign_flip(
                master_seed=master_seed, input_digest=input_digest,
                replicate_index=replicate_index, case_index=case_index,
                algorithm_version=algorithm_version,
            )
            for case_index in range(case_count)
        ]
    key = family_key(
        master_seed=master_seed, input_digest=input_digest,
        family_id_digest=family_digest(SIGN_FLIP_FAMILY),
        algorithm_version=algorithm_version,
    )
    flips: list[bool] = []
    mixed = 0
    for case_index in range(case_count):
        if case_index % 64 == 0:
            mixed = mix64(counter_state(
                key, sign_flip_word(replicate_index, case_index), 0,
            ))
        flips.append(bool((mixed >> (case_index & 63)) & 1))
    return flips


def implementation_digest() -> str:
    """Digest this implementation's source for the analysis metadata."""
    import inspect
    import sys

    module = sys.modules[__name__]
    return hashlib.sha256(
        inspect.getsource(module).encode("utf-8"),
    ).hexdigest()
