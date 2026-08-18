"""Reusable soak runner and reliability metrics for classic coordination."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HORIZON_BANDS: tuple[int, ...] = (1, 5, 10, 25, 50, 100)
MIN_REPETITIONS = 10


@dataclass(frozen=True)
class TrialSpec:
    """One fixed soak configuration at one action horizon."""

    configuration: str
    settings: Mapping[str, Any]
    horizon: int
    repetition: int
    seed: int


@dataclass(frozen=True)
class RoleMeasurement:
    """The cost and latency for one role activation."""

    role: str
    cost_usd: float
    latency_ms: float


@dataclass
class TrialOutcome:
    """The measured result from one soak trial."""

    effective_actions: int
    exact_success: bool
    completed: bool
    restart_attempted: bool = False
    restart_recovered: bool = False
    external_action_keys: list[str] = field(default_factory=list)
    budget_limit_usd: float = 0.0
    budget_spent_usd: float = 0.0
    retrieval_expected: int = 0
    retrieval_found: int = 0
    stall_count: int = 0
    replan_count: int = 0
    role_measurements: list[RoleMeasurement] = field(default_factory=list)
    unresolved_conflicts: int = 0
    minority_opportunities: int = 0
    minority_corrections: int = 0


@dataclass
class TrialRecord:
    """A trial specification, its result, and an optional driver error."""

    spec: TrialSpec
    outcome: TrialOutcome
    error: str | None = None


@dataclass
class HorizonMetrics:
    """Aggregate reliability measurements for one fixed horizon."""

    configuration: str
    horizon: int
    trials: int
    exact_task_success: float
    strict_repeated_run_success: bool
    false_completion_rate: float
    reliability_decay: float
    restart_attempts: int
    restart_recovery_rate: float
    duplicate_external_actions: int
    budget_overshoot_rate: float
    budget_overshoot_usd: float
    context_retrieval_recall: float
    stall_count: int
    replan_count: int
    unresolved_conflict_count: int
    minority_correction_rate: float
    average_effective_actions: float
    role_metrics: dict[str, dict[str, float | int]]


@dataclass
class SoakReport:
    """The complete soak result for all configurations and horizons."""

    horizon_bands: tuple[int, ...]
    repetitions: int
    records: list[TrialRecord]
    metrics: list[HorizonMetrics]

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon_bands": list(self.horizon_bands),
            "repetitions": self.repetitions,
            "records": [asdict(record) for record in self.records],
            "metrics": [asdict(metric) for metric in self.metrics],
        }

    def save(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return output


TrialDriver = Callable[[TrialSpec], Awaitable[TrialOutcome]]


class SoakHarness:
    """Run every fixed configuration at each required action horizon."""

    def __init__(
        self,
        *,
        horizons: tuple[int, ...] = HORIZON_BANDS,
        repetitions: int = MIN_REPETITIONS,
        concurrency: int = 1,
        base_seed: int = 0,
        trial_timeout_s: float | None = None,
    ) -> None:
        if repetitions < MIN_REPETITIONS:
            raise ValueError(
                f"repetitions must be at least {MIN_REPETITIONS}"
            )
        if not horizons or any(horizon < 1 for horizon in horizons):
            raise ValueError("horizons must contain positive action counts")
        if len(set(horizons)) != len(horizons):
            raise ValueError("horizons must not contain duplicates")
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        if trial_timeout_s is not None and trial_timeout_s <= 0:
            raise ValueError("trial_timeout_s must be positive")

        self.horizons = tuple(horizons)
        self.repetitions = repetitions
        self.concurrency = concurrency
        self.base_seed = base_seed
        self.trial_timeout_s = trial_timeout_s

    async def run(
        self,
        configurations: Mapping[str, Mapping[str, Any]],
        driver: TrialDriver,
    ) -> SoakReport:
        """Run the driver and preserve failures as reliability observations."""
        if not configurations:
            raise ValueError("configurations must not be empty")

        semaphore = asyncio.Semaphore(self.concurrency)
        specs = [
            TrialSpec(
                configuration=name,
                settings=dict(settings),
                horizon=horizon,
                repetition=repetition,
                seed=_stable_seed(
                    self.base_seed, name, horizon, repetition,
                ),
            )
            for name, settings in configurations.items()
            for horizon in self.horizons
            for repetition in range(self.repetitions)
        ]

        async def run_one(spec: TrialSpec) -> TrialRecord:
            async with semaphore:
                try:
                    call = driver(spec)
                    outcome = (
                        await asyncio.wait_for(call, self.trial_timeout_s)
                        if self.trial_timeout_s is not None
                        else await call
                    )
                    _validate_outcome(outcome)
                    return TrialRecord(spec=spec, outcome=outcome)
                # A soak driver can wrap any provider or fault injector.
                # Preserve each driver failure as a reliability observation.
                except Exception as exc:  # noqa: BLE001
                    return TrialRecord(
                        spec=spec,
                        outcome=TrialOutcome(
                            effective_actions=0,
                            exact_success=False,
                            completed=False,
                        ),
                        error=f"{type(exc).__name__}: {exc}",
                    )

        records = await asyncio.gather(*(run_one(spec) for spec in specs))
        return SoakReport(
            horizon_bands=self.horizons,
            repetitions=self.repetitions,
            records=records,
            metrics=compute_soak_metrics(records),
        )


def compute_soak_metrics(records: list[TrialRecord]) -> list[HorizonMetrics]:
    """Aggregate all required long-horizon reliability measurements."""
    grouped: dict[tuple[str, int], list[TrialRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.spec.configuration, record.spec.horizon)].append(record)

    success_by_configuration: dict[str, dict[int, float]] = defaultdict(dict)
    for (configuration, horizon), group in grouped.items():
        success_by_configuration[configuration][horizon] = _rate(
            record.outcome.exact_success for record in group
        )

    metrics: list[HorizonMetrics] = []
    for (configuration, horizon), group in sorted(grouped.items()):
        outcomes = [record.outcome for record in group]
        exact_success = _rate(outcome.exact_success for outcome in outcomes)
        false_completion = _rate(
            outcome.completed and not outcome.exact_success
            for outcome in outcomes
        )
        restart_trials = [outcome for outcome in outcomes if outcome.restart_attempted]
        duplicate_actions = sum(
            len(outcome.external_action_keys)
            - len(set(outcome.external_action_keys))
            for outcome in outcomes
        )
        overshoots = [
            max(0.0, outcome.budget_spent_usd - outcome.budget_limit_usd)
            for outcome in outcomes
        ]
        retrieval_expected = sum(
            outcome.retrieval_expected for outcome in outcomes
        )
        retrieval_found = sum(outcome.retrieval_found for outcome in outcomes)
        minority_opportunities = sum(
            outcome.minority_opportunities for outcome in outcomes
        )
        minority_corrections = sum(
            outcome.minority_corrections for outcome in outcomes
        )
        baseline_horizon = min(success_by_configuration[configuration])
        baseline = success_by_configuration[configuration][baseline_horizon]

        metrics.append(HorizonMetrics(
            configuration=configuration,
            horizon=horizon,
            trials=len(group),
            exact_task_success=exact_success,
            strict_repeated_run_success=all(
                outcome.exact_success for outcome in outcomes
            ),
            false_completion_rate=false_completion,
            reliability_decay=baseline - exact_success,
            restart_attempts=len(restart_trials),
            restart_recovery_rate=_rate(
                outcome.restart_recovered for outcome in restart_trials
            ),
            duplicate_external_actions=duplicate_actions,
            budget_overshoot_rate=_rate(value > 0 for value in overshoots),
            budget_overshoot_usd=sum(overshoots),
            context_retrieval_recall=(
                retrieval_found / retrieval_expected
                if retrieval_expected else 1.0
            ),
            stall_count=sum(outcome.stall_count for outcome in outcomes),
            replan_count=sum(outcome.replan_count for outcome in outcomes),
            unresolved_conflict_count=sum(
                outcome.unresolved_conflicts for outcome in outcomes
            ),
            minority_correction_rate=(
                minority_corrections / minority_opportunities
                if minority_opportunities else 1.0
            ),
            average_effective_actions=statistics.mean(
                outcome.effective_actions for outcome in outcomes
            ),
            role_metrics=_role_metrics(outcomes),
        ))
    return metrics


def _role_metrics(
    outcomes: list[TrialOutcome],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[RoleMeasurement]] = defaultdict(list)
    for outcome in outcomes:
        for measurement in outcome.role_measurements:
            grouped[measurement.role].append(measurement)

    result: dict[str, dict[str, float | int]] = {}
    for role, measurements in sorted(grouped.items()):
        latencies = [measurement.latency_ms for measurement in measurements]
        result[role] = {
            "activations": len(measurements),
            "total_cost_usd": sum(
                measurement.cost_usd for measurement in measurements
            ),
            "average_cost_usd": statistics.mean(
                measurement.cost_usd for measurement in measurements
            ),
            "average_latency_ms": statistics.mean(latencies),
            "p95_latency_ms": _nearest_rank(latencies, 95),
        }
    return result


def _validate_outcome(outcome: TrialOutcome) -> None:
    if outcome.effective_actions < 0:
        raise ValueError("effective_actions must not be negative")
    if outcome.retrieval_expected < 0 or outcome.retrieval_found < 0:
        raise ValueError("retrieval counts must not be negative")
    if outcome.retrieval_found > outcome.retrieval_expected:
        raise ValueError("retrieval_found exceeds retrieval_expected")
    if outcome.minority_corrections > outcome.minority_opportunities:
        raise ValueError("minority_corrections exceeds minority_opportunities")
    if outcome.restart_recovered and not outcome.restart_attempted:
        raise ValueError("restart recovery requires a restart attempt")


def _stable_seed(
    base_seed: int,
    configuration: str,
    horizon: int,
    repetition: int,
) -> int:
    value = f"{base_seed}:{configuration}:{horizon}:{repetition}".encode()
    return int.from_bytes(hashlib.sha256(value).digest()[:8], "big")


def _rate(flags: Any) -> float:
    values = list(flags)
    return sum(bool(value) for value in values) / len(values) if values else 0.0


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(len(ordered) * percentile / 100)
    return float(ordered[max(0, min(len(ordered) - 1, rank - 1))])
