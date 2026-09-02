"""Environment drivers: setup, actions, observations, and final state.

One contract covers every environment: setup builds one isolated
session from a declared configuration, actions apply one at a time
with an observation returned for each, observation reads never mutate
state, final state freezes into one canonical dictionary, cleanup
releases every session resource, and one explicit success predicate
grades the final state without reading final prose. The first driver
is one deterministic local test environment with logical steps and no
wall clock. A third-party driver never registers without sandbox
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from benchmarks.provenance import content_checksum


class EnvironmentDriverError(ValueError):
    """The driver request violates the environment contract."""


@dataclass
class EnvironmentSession:
    """One live environment session with its logical step counter."""

    session_id: str
    state: dict[str, Any] = field(default_factory=dict)
    logical_step: int = 0
    closed: bool = False


class EnvironmentDriver(Protocol):
    """The declared contract every environment driver implements."""

    driver_id: str
    driver_version: str
    origin: str  # built_in | third_party
    sandbox_isolated: bool

    async def setup(
        self, configuration: dict[str, Any],
    ) -> EnvironmentSession: ...

    async def apply_action(
        self, session: EnvironmentSession, action: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def observe(
        self, session: EnvironmentSession,
    ) -> dict[str, Any]: ...

    async def final_state(
        self, session: EnvironmentSession,
    ) -> dict[str, Any]: ...

    async def cleanup(self, session: EnvironmentSession) -> None: ...

    def success(
        self,
        final_state: dict[str, Any],
        expected: dict[str, Any],
    ) -> bool: ...


class LocalKeyValueEnvironment:
    """The deterministic local test environment.

    State is one in-memory key-value map. Every action advances one
    logical step, observations never mutate, and no wall clock or
    host randomness enters any result, so equal action sequences
    produce equal final states and digests on every host.
    """

    driver_id = "environment-local-key-value"
    driver_version = "1"
    origin = "built_in"
    sandbox_isolated = True

    _ACTIONS = ("set", "delete", "append")

    async def setup(
        self, configuration: dict[str, Any],
    ) -> EnvironmentSession:
        initial = dict(configuration.get("initial_state") or {})
        session_id = "session-" + content_checksum(initial)[:16]
        return EnvironmentSession(session_id=session_id, state=initial)

    async def apply_action(
        self, session: EnvironmentSession, action: dict[str, Any],
    ) -> dict[str, Any]:
        if session.closed:
            raise EnvironmentDriverError("The session is closed")
        kind = str(action.get("action") or "")
        key = str(action.get("key") or "")
        if kind not in self._ACTIONS or not key:
            raise EnvironmentDriverError(
                f"Unknown environment action: {action!r}"
            )
        if kind == "set":
            session.state[key] = action.get("value")
        elif kind == "delete":
            session.state.pop(key, None)
        else:
            existing = str(session.state.get(key) or "")
            session.state[key] = existing + str(action.get("value") or "")
        session.logical_step += 1
        return await self.observe(session)

    async def observe(
        self, session: EnvironmentSession,
    ) -> dict[str, Any]:
        return {
            "logical_step": session.logical_step,
            "state": {
                key: session.state[key]
                for key in sorted(session.state)
            },
        }

    async def final_state(
        self, session: EnvironmentSession,
    ) -> dict[str, Any]:
        observation = await self.observe(session)
        return {
            **observation,
            "state_digest": content_checksum(observation["state"]),
        }

    async def cleanup(self, session: EnvironmentSession) -> None:
        session.state.clear()
        session.closed = True

    def success(
        self,
        final_state: dict[str, Any],
        expected: dict[str, Any],
    ) -> bool:
        """Grade the final state, never the final prose."""
        state = final_state.get("state") or {}
        return all(
            key in state and state[key] == value
            for key, value in (expected or {}).items()
        )


_REGISTRY: dict[str, EnvironmentDriver] = {}


def register_driver(driver: EnvironmentDriver) -> None:
    """Register one environment driver under the isolation policy.

    A third-party driver never registers without sandbox isolation,
    so unsandboxed external environment code never runs.
    """
    if driver.origin not in ("built_in", "third_party"):
        raise EnvironmentDriverError(
            f"Unknown driver origin: {driver.origin!r}"
        )
    if driver.origin == "third_party" and not driver.sandbox_isolated:
        raise EnvironmentDriverError(
            "A third-party environment driver requires sandbox "
            "isolation before registration"
        )
    existing = _REGISTRY.get(driver.driver_id)
    if existing is not None and (
        existing.driver_version != driver.driver_version
    ):
        raise EnvironmentDriverError(
            f"The driver {driver.driver_id} is already registered "
            "with a different version"
        )
    _REGISTRY[driver.driver_id] = driver


def get_driver(driver_id: str) -> EnvironmentDriver:
    driver = _REGISTRY.get(driver_id)
    if driver is None:
        raise EnvironmentDriverError(
            f"Unknown environment driver: {driver_id}"
        )
    return driver


def list_drivers() -> list[dict[str, Any]]:
    return [
        {
            "driver_id": driver.driver_id,
            "driver_version": driver.driver_version,
            "origin": driver.origin,
            "sandbox_isolated": driver.sandbox_isolated,
        }
        for _, driver in sorted(_REGISTRY.items())
    ]


register_driver(LocalKeyValueEnvironment())
