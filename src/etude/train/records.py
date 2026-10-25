"""File contracts for training: clock declarations and checkpoints.

Every logged row gives a value for every declared clock. There is no run-wide
counter and no ordering rule: a repeated coordinate is kept, and readers take
the latest write.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from etude.run.records import ArtifactRef, Clock, Name, Record, SourceKey


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
    at: dict[Name, float]
    wall_ms: int
    # The author's labels, such as "best". They may be added and removed later.
    tags: tuple[Name, ...] = Field(default=(), max_length=32)


class ParentCheckpoint(Record):
    """`ETUDE_CONTINUES`: the parent run and the checkpoint of it a continuation starts from.

    The checkpoint is the parent's record, without tags. A file stored in the
    parent's folder reaches the machine as the job's input named `input`,
    and the record addresses it within that folder (`checkpoints/NAME`). With
    no input, the record references a model elsewhere by its own address.
    """

    parent: SourceKey
    checkpoint: CheckpointRecord
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
