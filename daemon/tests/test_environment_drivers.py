"""Environment drivers: determinism, isolation policy, final state.

The deterministic local environment produces equal final states and
digests for equal action sequences, observations never mutate state,
the success predicate follows final state only, and a third-party
driver never registers without sandbox isolation.
"""

from __future__ import annotations

import pytest

from benchmarks import environment_drivers
from benchmarks.environment_drivers import (
    EnvironmentDriverError,
    LocalKeyValueEnvironment,
    get_driver,
    list_drivers,
    register_driver,
)

ACTIONS = [
    {"action": "set", "key": "greeting", "value": "hello"},
    {"action": "append", "key": "greeting", "value": " world"},
    {"action": "set", "key": "extra", "value": 1},
    {"action": "delete", "key": "extra"},
]


async def _run_actions(driver, actions):
    session = await driver.setup({"initial_state": {"seeded": True}})
    for action in actions:
        await driver.apply_action(session, action)
    final = await driver.final_state(session)
    await driver.cleanup(session)
    return final


@pytest.mark.asyncio
async def test_equal_action_sequences_produce_equal_digests():
    driver = get_driver("environment-local-key-value")
    first = await _run_actions(driver, ACTIONS)
    second = await _run_actions(driver, ACTIONS)
    assert first == second
    assert first["state_digest"] == second["state_digest"]
    assert first["state"] == {"greeting": "hello world", "seeded": True}
    assert first["logical_step"] == len(ACTIONS)


@pytest.mark.asyncio
async def test_observation_never_mutates_state():
    driver = LocalKeyValueEnvironment()
    session = await driver.setup({"initial_state": {"a": 1}})
    before = await driver.observe(session)
    again = await driver.observe(session)
    assert before == again
    assert session.logical_step == 0


@pytest.mark.asyncio
async def test_success_predicate_follows_final_state_only():
    driver = LocalKeyValueEnvironment()
    session = await driver.setup({"initial_state": {}})
    await driver.apply_action(
        session, {"action": "set", "key": "answer", "value": "42"},
    )
    final = await driver.final_state(session)
    assert driver.success(final, {"answer": "42"}) is True
    assert driver.success(final, {"answer": "41"}) is False
    assert driver.success(final, {"missing": "42"}) is False


@pytest.mark.asyncio
async def test_closed_session_rejects_actions():
    driver = LocalKeyValueEnvironment()
    session = await driver.setup({"initial_state": {}})
    await driver.cleanup(session)
    with pytest.raises(EnvironmentDriverError, match="closed"):
        await driver.apply_action(
            session, {"action": "set", "key": "a", "value": 1},
        )


@pytest.mark.asyncio
async def test_unknown_action_rejects():
    driver = LocalKeyValueEnvironment()
    session = await driver.setup({"initial_state": {}})
    with pytest.raises(EnvironmentDriverError, match="Unknown"):
        await driver.apply_action(
            session, {"action": "execute_shell", "key": "a"},
        )


def test_third_party_driver_requires_sandbox_isolation():
    class ThirdPartyDriver(LocalKeyValueEnvironment):
        driver_id = "environment-third-party"
        origin = "third_party"
        sandbox_isolated = False

    with pytest.raises(EnvironmentDriverError, match="sandbox"):
        register_driver(ThirdPartyDriver())

    class IsolatedDriver(ThirdPartyDriver):
        driver_id = "environment-third-party-isolated"
        sandbox_isolated = True

    register_driver(IsolatedDriver())
    listed = {
        driver["driver_id"]: driver
        for driver in environment_drivers.list_drivers()
    }
    assert listed["environment-third-party-isolated"]["origin"] == (
        "third_party"
    )


def test_registry_lists_the_built_in_driver():
    listed = {driver["driver_id"] for driver in list_drivers()}
    assert "environment-local-key-value" in listed
    with pytest.raises(EnvironmentDriverError, match="Unknown"):
        get_driver("environment-nonexistent")
