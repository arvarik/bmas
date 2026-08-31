"""Foundation Stage 0D: named failpoints around durable boundaries.

A failpoint is one named crash site. Production code calls
``failpoint(name)`` at each durable boundary; the call does nothing
until a test arms the name. An armed failpoint raises
``InjectedFaultError`` a declared number of times, so a test can crash
the process path before and after every durable write and prove that
each transaction commits completely or not at all.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_ARMED: dict[str, int] = {}
_FIRED: dict[str, int] = {}


class InjectedFaultError(RuntimeError):
    """One armed failpoint fired."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Injected fault at {name}")
        self.name = name


def failpoint(name: str) -> None:
    """Fire one armed failpoint or do nothing."""
    remaining = _ARMED.get(name, 0)
    if remaining <= 0:
        return
    if remaining == 1:
        _ARMED.pop(name, None)
    else:
        _ARMED[name] = remaining - 1
    _FIRED[name] = _FIRED.get(name, 0) + 1
    raise InjectedFaultError(name)


def arm(name: str, times: int = 1) -> None:
    """Arm one failpoint for a number of firings."""
    if times < 1:
        raise ValueError("A failpoint arms for at least one firing")
    _ARMED[name] = times


def clear() -> None:
    """Disarm every failpoint and reset the firing counts."""
    _ARMED.clear()
    _FIRED.clear()


def armed_points() -> dict[str, int]:
    """Return the armed failpoints and their remaining firings."""
    return dict(_ARMED)


def fired_counts() -> dict[str, int]:
    """Return how often each failpoint fired since the last clear."""
    return dict(_FIRED)


@contextmanager
def armed(name: str, times: int = 1) -> Iterator[None]:
    """Arm one failpoint for the duration of a block."""
    arm(name, times)
    try:
        yield
    finally:
        _ARMED.pop(name, None)
