"""File contracts for training: clocks, lineage and immutable JSON row tables.

Every logged row gives a value for every declared clock. There is no run-wide
counter and no ordering rule: a repeated coordinate is kept, and readers take
the latest write.
"""

from __future__ import annotations

import re
import warnings

from pydantic import Field, JsonValue

from etude.checkpoints import CheckpointRecord
from etude.records import Clock, Name, Record, SourceKey


def Step(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """Released declaration spelling; count clocks are now declared by use."""
    warnings.warn(
        "Step is no longer needed: run.steps or run.log keywords declare count clocks; removed in 0.5",
        DeprecationWarning,
        stacklevel=2,
    )
    return Clock(name=name, kind="step", meaning=meaning)


def Time(name: str, meaning: str | None = None) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock in seconds, such as active training time. Not wall time."""
    return Clock(name=name, kind="time", meaning=name if meaning is None else meaning)


class ParentRun(Record):
    """`ETUDE_CONTINUES`: lineage from a directory or a selected checkpoint.

    `at` owns the continuation position. An inherited checkpoint carries the
    parent's record without tags; its file arrives in out or as a named input.
    Clocks retain the parent's kinds and labels when the child declares by use.
    """

    parent: SourceKey
    at: dict[Name, float] = Field(min_length=1)
    clocks: tuple[Clock, ...] = ()
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
