"""File contracts for training: clocks, lineage and immutable JSON row tables.

Every logged row gives a value for every declared clock. There is no run-wide
counter and no ordering rule: a repeated coordinate is kept, and readers take
the latest write.
"""

from __future__ import annotations

import re

from pydantic import Field, JsonValue

from etude.checkpoints import CheckpointRecord
from etude.records import Clock, Name, Record, SourceKey


def Step(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock that counts something, such as updates or learner transitions."""
    return Clock(name=name, kind="step", meaning=meaning)


def Time(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock in seconds, such as active training time. Not wall time."""
    return Clock(name=name, kind="time", meaning=meaning)


class ParentRun(Record):
    """`ETUDE_CONTINUES`: lineage from a directory or a selected checkpoint.

    `at` owns the continuation position. An inherited checkpoint carries the
    parent's record without tags; its file arrives in out or as a named input.
    """

    parent: SourceKey
    at: dict[Name, float] = Field(min_length=1)
    checkpoint: CheckpointRecord | None = None
    input: str | None = None


def checkpoint_name(at: dict[str, float], *, padded: bool = False) -> str:
    """A checkpoint's file name under its run: where the first declared clock stood.

    Safe in any bucket and shell: the clock's name keeps letters, digits, `.`,
    `_` and `-`, anything else becomes `_`, and the value is a plain number. A
    different checkpoint at a coordinate already recorded is kept as
    `NAME.ordinal-N` (`etude.train.tracking.Run.checkpoint`). The exact clocks are
    in the record itself.
    """
    clock, value = next(iter(at.items()))
    number = (
        (f"{int(value):08d}" if padded else str(int(value)))
        if float(value).is_integer()
        else repr(float(value))
    )
    return re.sub(r"[^A-Za-z0-9._-]", "_", clock) + "-" + number


class RowTable(Record):
    """Immutable JSON rows stored together as one content-addressed artifact."""

    rows: tuple[dict[str, JsonValue], ...]


class TablePoint(RowTable):
    """A selected row table at coordinate `x` on the reader's requested clock."""

    x: float
