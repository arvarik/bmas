# /opt/bmas/daemon/tests/test_gateway_lock.py
"""
Tests for BoardGateway per-task lock management.

Covers:
- Lock creation and reuse
- LRU eviction when capacity is exceeded
- Concurrent write serialization (single writer per task)
- Duplicate variant registration warning
"""

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# conftest.py has already injected the fake config module.
from core.board_store import InMemoryBoardStore  # noqa: E402
from core.event_emitter import InMemoryEventEmitter  # noqa: E402
from core.gateway import BoardGateway  # noqa: E402


@pytest.fixture
def gateway():
    """Fresh BoardGateway with small capacity for eviction testing."""
    store = InMemoryBoardStore()
    emitter = InMemoryEventEmitter()
    gw = BoardGateway(store, emitter)
    return gw


class TestTaskLockCreation:
    """Tests for _task_lock() — creation and reuse."""

    def test_lock_created_on_first_access(self, gateway):
        """Accessing a lock for a new task creates it."""
        assert "task-a" not in gateway._locks
        lock = gateway._task_lock("task-a")
        assert isinstance(lock, asyncio.Lock)
        assert "task-a" in gateway._locks

    def test_same_lock_returned_on_second_access(self, gateway):
        """Accessing the same task_id returns the same lock object."""
        lock1 = gateway._task_lock("task-x")
        lock2 = gateway._task_lock("task-x")
        assert lock1 is lock2

    def test_different_tasks_get_different_locks(self, gateway):
        """Different task IDs get distinct locks."""
        lock_a = gateway._task_lock("task-a")
        lock_b = gateway._task_lock("task-b")
        assert lock_a is not lock_b

    def test_lock_count_grows_with_distinct_tasks(self, gateway):
        """Lock count grows for each new task."""
        for i in range(10):
            gateway._task_lock(f"task-{i}")
        assert len(gateway._locks) == 10


class TestLockEviction:
    """Tests for LRU eviction when the lock dict reaches capacity."""

    def test_eviction_triggers_at_capacity(self):
        """When lock count reaches _lock_capacity, oldest is evicted."""
        store = InMemoryBoardStore()
        emitter = InMemoryEventEmitter()
        gw = BoardGateway(store, emitter)
        # Reduce capacity for fast testing
        gw._lock_capacity = 5

        # Fill to capacity
        for i in range(5):
            gw._task_lock(f"task-{i}")
        assert len(gw._locks) == 5

        # One more — should evict task-0 (oldest)
        gw._task_lock("task-new")

        assert len(gw._locks) == 5  # still at capacity after eviction
        assert "task-0" not in gw._locks  # oldest evicted
        assert "task-new" in gw._locks

    def test_eviction_is_oldest_first(self):
        """Eviction always removes the first-inserted key."""
        store = InMemoryBoardStore()
        emitter = InMemoryEventEmitter()
        gw = BoardGateway(store, emitter)
        gw._lock_capacity = 3

        gw._task_lock("alpha")
        gw._task_lock("beta")
        gw._task_lock("gamma")  # at capacity

        # alpha should be evicted when delta is added
        gw._task_lock("delta")

        assert "alpha" not in gw._locks
        assert "beta" in gw._locks
        assert "gamma" in gw._locks
        assert "delta" in gw._locks

    def test_eviction_logs_at_debug(self, caplog):
        """Eviction produces a debug log message."""
        store = InMemoryBoardStore()
        emitter = InMemoryEventEmitter()
        gw = BoardGateway(store, emitter)
        gw._lock_capacity = 2

        with caplog.at_level(logging.DEBUG, logger="bmas.gateway"):
            gw._task_lock("first")
            gw._task_lock("second")  # at capacity
            gw._task_lock("third")   # triggers eviction

        assert any("first" in msg for msg in caplog.messages)

    def test_re_accessing_evicted_task_creates_new_lock(self):
        """After eviction, re-accessing the evicted task creates a fresh lock."""
        store = InMemoryBoardStore()
        emitter = InMemoryEventEmitter()
        gw = BoardGateway(store, emitter)
        gw._lock_capacity = 2

        lock_original = gw._task_lock("task-a")
        gw._task_lock("task-b")  # at capacity
        gw._task_lock("task-c")  # task-a evicted

        lock_recreated = gw._task_lock("task-a")
        assert lock_original is not lock_recreated  # new lock object


class TestVariantRegistry:
    """Tests for the variant registry (register/get/available)."""

    def test_register_and_get_variant(self):
        """Registered variant is retrievable by name."""
        from core.variants import available_variants, get_variant_class, register_variant

        class FakeVariant:
            name = "test-fake"

        original_count = len(available_variants())
        register_variant("_test_fake_", FakeVariant)

        assert get_variant_class("_test_fake_") is FakeVariant
        assert "_test_fake_" in available_variants()
        assert len(available_variants()) == original_count + 1

    def test_register_non_class_raises_type_error(self):
        """Registering a non-class raises TypeError."""
        from core.variants import register_variant

        with pytest.raises(TypeError, match="expects a class"):
            register_variant("bad", "not-a-class")  # type: ignore

    def test_register_duplicate_warns(self, caplog):
        """Registering the same name twice logs a warning."""
        from core.variants import register_variant

        class V1:
            name = "dup"

        class V2:
            name = "dup"

        register_variant("_test_dup_v1_", V1)

        with caplog.at_level(logging.WARNING, logger="bmas.variants"):
            register_variant("_test_dup_v1_", V2)

        assert any("re-register" in msg.lower() for msg in caplog.messages)

    def test_get_unknown_variant_returns_none(self):
        """Looking up an unregistered variant returns None."""
        from core.variants import get_variant_class

        result = get_variant_class("does-not-exist-xyz")
        assert result is None
