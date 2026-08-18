"""Operational health pressure tests."""

from routes.health import _has_runtime_pressure


def test_runtime_pressure_reports_blocked_recovery_and_open_circuits():
    queue = {
        "queued_tasks": 0,
        "queue_capacity": 10,
        "recovery_blocked_tasks": 1,
    }
    assert _has_runtime_pressure(queue, {"endpoint_requests": {}}) is True

    queue["recovery_blocked_tasks"] = 0
    runtime = {
        "endpoint_requests": {
            "http://agent": {"active": 0, "waiting": 2, "circuit": "open"}
        }
    }
    assert _has_runtime_pressure(queue, runtime) is True


def test_runtime_pressure_stays_clear_below_limits():
    queue = {
        "queued_tasks": 2,
        "queue_capacity": 10,
        "recovery_blocked_tasks": 0,
    }
    runtime = {
        "endpoint_requests": {
            "http://agent": {"active": 1, "waiting": 0, "circuit": "closed"}
        }
    }
    assert _has_runtime_pressure(queue, runtime) is False
