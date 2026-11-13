"""File contracts for training: clocks, checkpoints and immutable JSON row tables.

Every logged row gives a value for every declared clock. There is no run-wide
counter and no ordering rule: a repeated coordinate is kept, and readers take
the latest write.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, JsonValue, field_validator

from etude.carriage import within
from etude.tracking.records import ArtifactRef, Clock, Name, Record, SourceKey


def Step(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock that counts something, such as updates or learner transitions."""
    return Clock(name=name, kind="step", meaning=meaning)


def Time(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock in seconds, such as active training time. Not wall time."""
    return Clock(name=name, kind="time", meaning=meaning)


class CheckpointRecord(Record):
    """A recorded model: what a run saved and where every one of its clocks stood.

    `ordinal` counts a run's checkpoints from one, across its attempts on
    every machine. When Etude stored the file,
    the artifact carries its digest and size and the file sits beside this
    record, in the author's directory or bucket. Everything here is fixed
    except `tags`.
    """

    schema_version: Literal[1] = 1
    ordinal: int = Field(ge=1)
    artifact: ArtifactRef
    # A stored file's key relative to the run folder. None for older records
    # (beside this record) and references to files outside the tracked folder.
    file: str | None = None
    at: dict[Name, float]
    wall_ms: int
    # The author's labels, such as "best". They may be added and removed later.
    tags: tuple[Name, ...] = Field(default=(), max_length=32)

    @field_validator("file")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        if value is not None and not within(value):
            raise ValueError("checkpoint file must be relative to its run")
        return value


class ParentRun(Record):
    """`ETUDE_CONTINUES`: lineage from a directory or a selected checkpoint.

    `at` owns the continuation position. An inherited checkpoint carries the
    parent's record without tags; its file arrives in out or as a named input.
    """

    parent: SourceKey
    at: dict[Name, float] = Field(min_length=1)
    checkpoint: CheckpointRecord | None = None
    input: str | None = None


def checkpoint_name(at: dict[str, float]) -> str:
    """A checkpoint's file name under its run: where the first declared clock stood.

    Safe in any bucket and shell: the clock's name keeps letters, digits, `.`,
    `_` and `-`, anything else becomes `_`, and the value is a plain number. A
    different checkpoint at a coordinate already recorded is kept as
    `NAME.ordinal-N` (`etude.train.run.Run.checkpoint`). The exact clocks are
    in the record itself.
    """
    clock, value = next(iter(at.items()))
    number = str(int(value)) if float(value).is_integer() else repr(float(value))
    return re.sub(r"[^A-Za-z0-9._-]", "_", clock) + "-" + number


class RowTable(Record):
    """Immutable JSON rows stored together as one content-addressed artifact."""

    rows: tuple[dict[str, JsonValue], ...]


class TablePoint(RowTable):
    """A selected row table at coordinate `x` on the reader's requested clock."""

    x: float
