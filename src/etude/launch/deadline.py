"""Nested transport operations share their caller's optional monotonic deadline."""

import math
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from threading import Thread

_deadline: ContextVar[float | None] = ContextVar("operation_deadline", default=None)


def remaining(default: float | None = None) -> float | None:
    deadline = _deadline.get()
    if deadline is None:
        return default
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("operation time limit reached")
    return left if default is None else min(default, left)


@contextmanager
def limit_time(seconds: float | None) -> Iterator[None]:
    """Share an operation budget; None adds no limit and never extends an outer one."""
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("timeout must be positive and finite, or None")
    outer = _deadline.get()
    deadline = None if seconds is None else time.monotonic() + seconds
    if outer is not None:
        deadline = outer if deadline is None else min(outer, deadline)
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def call[T](operation: Callable[[], T]) -> T:
    """Bound a blocking call without assuming socket progress means completion.

    Expiry abandons the reply, not the operation: callers must retain uncertainty
    for writes. The daemon may finish its one request; it never retries it.
    """
    seconds = remaining()
    if seconds is None:
        return operation()
    result: Future[T] = Future()
    context = copy_context()

    def invoke() -> None:
        try:
            remaining()
            result.set_result(operation())
        except BaseException as error:
            result.set_exception(error)

    Thread(target=context.run, args=(invoke,), daemon=True).start()
    try:
        return result.result(timeout=remaining())
    except TimeoutError:
        raise TimeoutError("operation deadline reached; remote outcome unconfirmed") from None
