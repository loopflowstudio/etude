"""Declared command expectations; current costs are pinned by counting tests.

These declarations describe the intended interface, not enforced execution policy.
The performance Task owns moving today's remote refreshes out of reads and asks.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar

F = TypeVar("F", bound=Callable[..., object])


class Kind(StrEnum):
    read = "read"
    ask = "ask"
    wait = "wait"


def kind(value: Kind) -> Callable[[F], F]:
    def declare(function: F) -> F:
        setattr(function, "command_kind", value)
        return function

    return declare
