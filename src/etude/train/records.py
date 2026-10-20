"""File contracts for training: clock declarations, checkpoints and seed intervals.

Every logged row gives a value for every declared clock. There is no run-wide
counter and no ordering rule: a repeated coordinate is kept, and readers take
the latest write.
"""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import Field

from etude.run.records import ArtifactRef, Clock, Name, Record


def Step(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock that counts something, such as updates or learner transitions."""
    return Clock(name=name, kind="step", meaning=meaning)


def Time(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock in seconds, such as active training time. Not wall time."""
    return Clock(name=name, kind="time", meaning=meaning)


# Two-sided 95% Student's t quantiles for 1 to 30 degrees of freedom.
_T95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip


def interval95(mean: float, std: float, n: int) -> tuple[float, float] | None:
    """The 95% interval for a mean of `n` seeds with sample deviation `std`.

    One seed has no interval. The seeds run are a sample of those that could
    have been, so the deviation divides by n - 1 and the width uses Student's t.
    """
    if n < 2:
        return None
    half = (_T95[n - 2] if n - 2 < len(_T95) else 1.960) * std / math.sqrt(n)
    return mean - half, mean + half


class CheckpointRecord(Record):
    """A recorded model: what a run saved and where every one of its clocks stood.

    `ordinal` counts a run's checkpoints from one. When Etude stored the file,
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


def checkpoint_name(at: dict[str, float]) -> str:
    """A checkpoint's file name under its run: where the first declared clock stood.

    Safe in any bucket and shell: the clock's name keeps letters, digits, `.`,
    `_` and `-`, anything else becomes `_`, and the value is a plain number. A
    second checkpoint at the same coordinate, such as one saved again after a
    resume, takes the same name. The exact clocks are in the record itself.
    """
    clock, value = next(iter(at.items()))
    number = str(int(value)) if float(value).is_integer() else repr(float(value))
    return re.sub(r"[^A-Za-z0-9._-]", "_", clock) + "-" + number
