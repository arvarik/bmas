"""The vectorized frozen-analysis engine.

The pure-Python bootstrap derives one draw at a time and cannot meet
the published analysis limit of 100,000 cases and 10,000 replicates
in 30 seconds. This engine derives every replicate of one batch in a
few array passes: the keyed-counter ``bmas-analysis-rng`` words mix
under the SplitMix64 finalizer as unsigned 64-bit arrays, the two
32-bit halves of every word become candidates, rejected candidates
retry with the rejection counter, and the accepted indexes gather the
per-case weights and weighted deltas.

Every number equals the reference engine bit for bit. The reference
accumulates in binary64 from left to right; this engine reduces the
draw axis of a two-dimensional array, which NumPy accumulates in the
same order, and it verifies that property on the host before the
first use. Replicate batches run on a bounded thread pool because
NumPy releases the interpreter lock inside array kernels, and the
batch order never changes a result because every replicate derives
from its own counter word.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from benchmarks import analysis_rng

try:  # NumPy is the pinned vector toolchain; the reference engine stays.
    import numpy as _np
except ImportError:  # pragma: no cover - exercised on hosts without NumPy
    _np = None  # type: ignore[assignment]

ENGINE_REFERENCE = "python-sequential"
ENGINE_VECTORIZED = "numpy-vectorized"
ENGINES = (ENGINE_REFERENCE, ENGINE_VECTORIZED)
# The arithmetic contract both engines honour: binary64 operations
# in the published order with left-to-right accumulation.
STATISTICS_CONTRACT = "binary64-sequential-summation"

_MAX_BATCH_ELEMENTS = 4_000_000
_INLINE_ELEMENTS = 250_000
_verified: bool | None = None


class AnalysisEngineError(RuntimeError):
    """The vectorized engine cannot honour the requested computation."""


def available() -> bool:
    """Report whether the vectorized engine can run on this host."""
    return _np is not None and _sequential_reduction_verified()


def numpy_version() -> str | None:
    return None if _np is None else str(_np.__version__)


def describe() -> str:
    """Name the vector toolchain the host resolves."""
    if _np is None:
        return "python"
    return f"numpy-{_np.__version__}"


def worker_count() -> int:
    configured = os.getenv("BMAS_ANALYSIS_THREADS")
    if configured:
        return max(1, int(configured))
    return max(1, min(8, os.cpu_count() or 1))


def _sequential_reduction_verified() -> bool:
    """Prove on this host that the axis-zero reduction is sequential."""
    global _verified
    if _verified is not None:
        return _verified
    if _np is None:
        _verified = False
        return False
    generator = _np.random.default_rng(20260903)
    sample = generator.random((4096, 2)) * 1e6 - 2.5e5
    reduced = _np.add.reduce(sample, axis=0)
    expected = []
    for column in range(2):
        total = 0.0
        for value in sample[:, column].tolist():
            total += value
        expected.append(total)
    _verified = bool(
        float(reduced[0]) == expected[0] and float(reduced[1]) == expected[1]
    )
    return _verified


def sequential_sum(values: Any) -> float:
    """Accumulate binary64 values from left to right starting at zero."""
    total = 0.0
    for value in values:
        total += value
    return total


# ── Prepared families ────────────────────────────────────────────────


def _prepare_families(
    specification: dict[str, Any],
    paired: dict[str, Any],
    input_digest: bytes,
) -> list[dict[str, Any]]:
    resampling = specification["resampling"]
    version = int(resampling["algorithm_version"])
    master_seed = int(resampling["master_seed"])
    prepared = []
    offset = 0
    for family in specification["cluster_order"]:
        entry = paired["families"][family]
        usable = entry["usable_case_ids"]
        count = len(usable)
        weights = entry["renormalized_weights"]
        weight_values = [float(weights[case_id]) for case_id in usable]
        delta_values = [
            float(entry["usable"][case_id]["delta"]) for case_id in usable
        ]
        weight_array = _np.asarray(weight_values, dtype=_np.float64)
        delta_array = _np.asarray(delta_values, dtype=_np.float64)
        prepared.append({
            "family": family,
            "count": count,
            "offset": offset,
            "weights": weight_array,
            "weighted_deltas": weight_array * delta_array,
            "weight_sum": sequential_sum(weight_values),
            "family_weight": float(entry["family_weight"]),
            "key": analysis_rng.family_key(
                master_seed=master_seed, input_digest=input_digest,
                family_id_digest=analysis_rng.family_digest(family),
                algorithm_version=version,
            ) if count else 0,
            "limit": (
                analysis_rng.rejection_limit(count, version) if count else 0
            ),
        })
        offset += count
    return prepared


# ── The keyed-counter derivation as array passes ─────────────────────


def _mix(state: Any) -> Any:
    z = state ^ (state >> _np.uint64(30))
    z *= _np.uint64(analysis_rng.MIX_MULTIPLIER_ONE)
    z ^= z >> _np.uint64(27)
    z *= _np.uint64(analysis_rng.MIX_MULTIPLIER_TWO)
    z ^= z >> _np.uint64(31)
    return z


def draw_indexes(
    key: int, case_count: int, first_replicate: int, last_replicate: int,
    limit: int,
) -> Any:
    """Derive the accepted case indexes of one family and batch.

    The result has one row per draw and one column per replicate.
    """
    replicate_count = last_replicate - first_replicate
    pair_count = (case_count + 1) // 2
    replicates = _np.arange(
        first_replicate, last_replicate, dtype=_np.uint64,
    ) << _np.uint64(32)
    pairs = _np.arange(pair_count, dtype=_np.uint64)
    words = replicates[None, :] | pairs[:, None]
    state = words * _np.uint64(analysis_rng.GOLDEN_GAMMA)
    state += _np.uint64(key)
    mixed = _mix(state)
    candidates = _np.empty((2 * pair_count, replicate_count), dtype=_np.uint32)
    candidates[0::2] = (mixed >> _np.uint64(32)).astype(_np.uint32)
    candidates[1::2] = (mixed & _np.uint64(0xFFFFFFFF)).astype(_np.uint32)
    candidates = candidates[:case_count]
    del state, mixed
    if limit < 2**32:
        rejected = candidates >= _np.uint32(limit)
        counter = 1
        while rejected.any():
            draw_rows, replicate_columns = _np.nonzero(rejected)
            retried_words = words[draw_rows >> 1, replicate_columns]
            retried_state = retried_words * _np.uint64(
                analysis_rng.GOLDEN_GAMMA,
            )
            retried_state += _np.uint64(key)
            retried_state += _np.uint64(
                (counter * analysis_rng.COUNTER_STEP) & (2**64 - 1),
            )
            retried = _mix(retried_state)
            halves = _np.where(
                draw_rows % 2 == 0,
                retried >> _np.uint64(32),
                retried & _np.uint64(0xFFFFFFFF),
            ).astype(_np.uint32)
            candidates[draw_rows, replicate_columns] = halves
            rejected = _np.zeros_like(rejected)
            rejected[draw_rows, replicate_columns] = halves >= _np.uint32(
                limit,
            )
            counter += 1
    return (candidates % _np.uint32(case_count)).astype(_np.int64)


def sign_flip_bits(
    key: int, case_count: int, first_replicate: int, last_replicate: int,
) -> Any:
    """Derive the sign-flip bits of every case for one batch."""
    replicate_count = last_replicate - first_replicate
    block_count = (case_count + 63) // 64
    replicates = _np.arange(
        first_replicate, last_replicate, dtype=_np.uint64,
    ) << _np.uint64(32)
    blocks = _np.arange(block_count, dtype=_np.uint64)
    words = replicates[None, :] | blocks[:, None]
    state = words * _np.uint64(analysis_rng.GOLDEN_GAMMA)
    state += _np.uint64(key)
    mixed = _mix(state)
    shifts = _np.arange(64, dtype=_np.uint64)
    bits = (mixed[:, None, :] >> shifts[None, :, None]) & _np.uint64(1)
    return bits.reshape(block_count * 64, replicate_count)[:case_count] == 1


# ── Replicate estimates ──────────────────────────────────────────────


def _batches(replicate_count: int, largest_family: int) -> list[tuple[int, int]]:
    width = max(2, min(256, _MAX_BATCH_ELEMENTS // max(largest_family, 1)))
    return [
        (start, min(start + width, replicate_count))
        for start in range(0, replicate_count, width)
    ]


def _run_batches(
    task: Any, batches: list[tuple[int, int]], element_count: int,
) -> list[Any]:
    if len(batches) <= 1 or element_count <= _INLINE_ELEMENTS:
        return [task(start, stop) for start, stop in batches]
    with ThreadPoolExecutor(max_workers=worker_count()) as pool:
        return list(pool.map(lambda span: task(*span), batches))


def _combine(
    numerator: Any, denominator: Any, mask: Any, family_weight: float,
    aggregate: Any,
) -> tuple[Any, Any]:
    weight = _np.float64(family_weight)
    numerator = numerator + _np.where(mask, weight * aggregate, 0.0)
    denominator = denominator + _np.where(mask, weight, 0.0)
    return numerator, denominator


def bootstrap_estimates(
    specification: dict[str, Any],
    paired: dict[str, Any],
    input_digest: bytes,
) -> list[float | None]:
    """Compute every replicate estimate of the weighted case bootstrap."""
    if not available():
        raise AnalysisEngineError("The vectorized engine is unavailable")
    resampling = specification["resampling"]
    if int(resampling["algorithm_version"]) < 2:
        raise AnalysisEngineError(
            "The SHA-256 derivation of algorithm version 1 never "
            "vectorizes; freeze algorithm version 2 for this engine"
        )
    replicate_count = int(resampling["resample_count"])
    families = _prepare_families(specification, paired, input_digest)
    largest = max((family["count"] for family in families), default=0)

    def task(first: int, last: int) -> Any:
        padded_last = last if last - first >= 2 else first + 2
        width = padded_last - first
        numerator = _np.zeros(width, dtype=_np.float64)
        denominator = _np.zeros(width, dtype=_np.float64)
        for family in families:
            count = family["count"]
            if not count:
                continue
            indexes = draw_indexes(
                family["key"], count, first, padded_last, family["limit"],
            )
            weight_sum = _np.add.reduce(
                _np.take(family["weights"], indexes), axis=0,
            ) + 0.0
            mask = weight_sum > 0
            total = _np.add.reduce(
                _np.take(family["weighted_deltas"], indexes), axis=0,
            ) + 0.0
            aggregate = _np.divide(
                total, weight_sum, out=_np.zeros(width), where=mask,
            )
            numerator, denominator = _combine(
                numerator, denominator, mask, family["family_weight"],
                aggregate,
            )
        estimate = _np.divide(
            numerator, denominator, out=_np.full(width, _np.nan),
            where=denominator > 0,
        )
        return estimate[: last - first]

    batches = _batches(replicate_count, largest)
    pieces = _run_batches(task, batches, replicate_count * max(largest, 1))
    estimates: list[float | None] = []
    for piece in pieces:
        for value in piece.tolist():
            estimates.append(None if value != value else float(value))
    return estimates


def sign_flip_exceedances(
    specification: dict[str, Any],
    paired: dict[str, Any],
    input_digest: bytes,
    *,
    target: float,
) -> int:
    """Count the replicates whose flipped statistic reaches the target."""
    if not available():
        raise AnalysisEngineError("The vectorized engine is unavailable")
    resampling = specification["resampling"]
    version = int(resampling["algorithm_version"])
    if version < 2:
        raise AnalysisEngineError(
            "The SHA-256 derivation of algorithm version 1 never vectorizes"
        )
    replicate_count = int(resampling["resample_count"])
    families = _prepare_families(specification, paired, input_digest)
    case_count = sum(family["count"] for family in families)
    key = analysis_rng.family_key(
        master_seed=int(resampling["master_seed"]),
        input_digest=input_digest,
        family_id_digest=analysis_rng.family_digest(
            analysis_rng.SIGN_FLIP_FAMILY,
        ),
        algorithm_version=version,
    )

    def task(first: int, last: int) -> int:
        padded_last = last if last - first >= 2 else first + 2
        width = padded_last - first
        flips = sign_flip_bits(key, case_count, first, padded_last)
        numerator = _np.zeros(width, dtype=_np.float64)
        denominator = _np.zeros(width, dtype=_np.float64)
        for family in families:
            count = family["count"]
            if not count or family["weight_sum"] <= 0:
                continue
            rows = flips[family["offset"]: family["offset"] + count]
            deltas = family["weighted_deltas"][:, None]
            signed = _np.where(rows, -deltas, deltas)
            total = _np.add.reduce(signed, axis=0) + 0.0
            aggregate = total / _np.float64(family["weight_sum"])
            numerator, denominator = _combine(
                numerator, denominator, _np.ones(width, dtype=bool),
                family["family_weight"], aggregate,
            )
        values = _np.divide(
            numerator, denominator, out=_np.full(width, _np.nan),
            where=denominator > 0,
        )[: last - first]
        return int(_np.count_nonzero(_np.abs(values) >= target))

    batches = _batches(replicate_count, case_count)
    return sum(_run_batches(task, batches, replicate_count * max(case_count, 1)))
