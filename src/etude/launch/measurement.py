"""Opt-in profiling and elapsed seconds at external boundaries.

Spans contain only fixed operation names and durations, never arguments, payloads
or credentials. This module stays standard-library-only for the pod guardian.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


@contextmanager
def span(name: str) -> Iterator[None]:
    """Append one plain timing line to ETUDE_SPANS, when explicitly configured."""
    destination = os.environ.get("ETUDE_SPANS")
    if not destination:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        # Measurement failures must not change execution or hide its error.
        try:
            with Path(destination).open("a") as output:
                output.write(f"{name} seconds={time.perf_counter() - started:.9f}\n")
        except OSError:
            pass


def timed(name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def invoke(*args: P.args, **kwargs: P.kwargs) -> T:
            with span(name):
                return function(*args, **kwargs)

        return invoke

    return decorate
