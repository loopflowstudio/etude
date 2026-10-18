"""Finite log reads share one monotonic deadline across transport requests."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("log_deadline", default=None)


def remaining(default: float = 60) -> float:
    deadline = _deadline.get()
    if deadline is None:
        return default
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("log retrieval time limit reached")
    return min(default, left)


@contextmanager
def read_deadline(seconds: float) -> Iterator[None]:
    if seconds <= 0:
        raise ValueError("log timeout must be positive")
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)
