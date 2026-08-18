"""Small endpoint circuit breaker for agent dispatch.

The breaker blocks new activations after repeated transport failures. An
activation with an uncertain delivery result still retries the same endpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

CircuitStatus = Literal["closed", "open", "half_open"]


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None
    half_open_probe: bool = False


class EndpointCircuitBreaker:
    """Track transport health for each agent endpoint."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout_s = max(0.0, float(recovery_timeout_s))
        self._clock = clock
        self._states: dict[str, _CircuitState] = {}

    def allow(self, endpoint: str) -> bool:
        """Return true when a new activation can use this endpoint."""
        state = self._states.get(endpoint)
        if state is None or state.opened_at is None:
            return True
        if self._clock() - state.opened_at < self.recovery_timeout_s:
            return False
        if state.half_open_probe:
            return False
        state.half_open_probe = True
        return True

    def record_success(self, endpoint: str) -> None:
        """Close the endpoint circuit after one valid response."""
        self._states.pop(endpoint, None)

    def record_failure(self, endpoint: str) -> None:
        """Record one transport or protocol failure."""
        state = self._states.setdefault(endpoint, _CircuitState())
        state.failures += 1
        state.half_open_probe = False
        if state.failures >= self.failure_threshold:
            state.opened_at = self._clock()

    def status(self, endpoint: str) -> CircuitStatus:
        """Return the current endpoint state without reserving a probe."""
        state = self._states.get(endpoint)
        if state is None or state.opened_at is None:
            return "closed"
        if self._clock() - state.opened_at < self.recovery_timeout_s:
            return "open"
        return "half_open"

    def failures(self, endpoint: str) -> int:
        """Return the current consecutive failure count."""
        return self._states.get(endpoint, _CircuitState()).failures
