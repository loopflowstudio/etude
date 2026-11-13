"""File contracts for training: clocks, checkpoints and immutable JSON row tables.

Every logged row gives a value for every declared clock. There is no run-wide
counter and no ordering rule: a repeated coordinate is kept, and readers take
the latest write.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self
from urllib.parse import unquote, urlparse

from pydantic import BeforeValidator, Field, JsonValue, field_validator, model_validator

from etude.carriage import checkpoint_file_name, within
from etude.tracking.records import ArtifactRef, Clock, Name, Record, SourceKey

if TYPE_CHECKING:
    from etude.eval.records import CheckpointPlayer, Inference


def Step(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock that counts something, such as updates or learner transitions."""
    return Clock(name=name, kind="step", meaning=meaning)


def Time(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock in seconds, such as active training time. Not wall time."""
    return Clock(name=name, kind="time", meaning=meaning)


def checkpoint_id(
    *, run: str | None = None, name: str | None = None, address: str | None = None
) -> str:
    """A checkpoint's identity by run and portable name, or an outside address."""
    if (run is None) != (name is None) or (run is None) == (address is None):
        raise ValueError("checkpoint identity needs a run and name, or an address")
    if address is not None:
        if not address:
            raise ValueError("checkpoint address must not be empty")
        return address
    assert run is not None and name is not None
    if not within(run) or not within(name):
        raise ValueError("checkpoint identity must use portable relative names")
    key = name if name.startswith("out/") else "checkpoints/" + name
    return run + "/" + key


def _identity(value: object) -> object:
    # Released evaluations stored a structured key; identity remains its encoded text.
    if isinstance(value, dict):
        if set(value) - {"run", "name", "address"}:
            raise ValueError("unknown checkpoint identity fields")
        fields = {name: value.get(name) for name in ("run", "name", "address")}
        if not all(each is None or isinstance(each, str) for each in fields.values()):
            raise ValueError("checkpoint identity fields must be strings")
        return checkpoint_id(run=fields["run"], name=fields["name"], address=fields["address"])
    return value


CheckpointId = Annotated[str, BeforeValidator(_identity), Field(min_length=1)]


class CheckpointRef(Record):
    """A checkpoint to seat: its key, and the file holding it, which must carry its digest.

    `at` is where it stood on each clock, such as `{"updates": 600}`: a
    declared fact the seat carries to its matches so a report can place it,
    and part of no key. `line` names the learner it belongs to, such as a
    seed or a recipe, so a report compares it with that learner's other
    checkpoints; also part of no key. A tracked checkpoint's ref fills both
    from its run; give them yourself for a checkpoint no Etude run recorded.
    """

    key: CheckpointId
    artifact: ArtifactRef
    at: dict[str, float] = {}
    line: str | None = None

    @model_validator(mode="after")
    def verifiable(self) -> Self:
        if self.artifact.sha256 is None:
            raise ValueError("model_checkpoint_requires_digest")
        return self


class CheckpointRecord(Record):
    """A published checkpoint: its files and where every one of its clocks stood.

    `ordinal` counts a run's checkpoints from one, across its attempts on
    every machine. When Etude stored the file,
    the artifact carries its digest and size and the file sits beside this
    record, in the author's directory or bucket. Everything here is fixed
    except `tags`.
    """

    schema_version: Literal[1] = 1
    run: str = ""
    experiment: str = ""
    line: str = ""
    ordinal: int = Field(ge=1)
    artifact: ArtifactRef
    # A stored file's key relative to the run folder. None for older records
    # (beside this record) and references to files outside the tracked folder.
    file: str | None = None
    at: dict[Name, float]
    wall_ms: int | None = None
    # The author's labels, such as "best". They may be added and removed later.
    tags: tuple[Name, ...] = Field(default=(), max_length=32)

    @property
    def name(self) -> str:
        return (
            self.file
            if self.file is not None and self.file.startswith("out/")
            else checkpoint_file_name(self.artifact.uri)
        )

    @property
    def key(self) -> str:
        return checkpoint_id(run=self.run, name=self.name)

    @property
    def ref(self) -> CheckpointRef:
        return CheckpointRef(
            key=self.key, artifact=self.artifact, at=self.at, line=self.line or self.run
        )

    @property
    def path(self) -> Path:
        """This checkpoint on the current machine; remote addresses need an input first."""
        parsed = urlparse(self.artifact.uri)
        if parsed.scheme not in ("", "file") or parsed.netloc:
            raise ValueError("checkpoint is not on this machine; give it to a job as an input")
        return Path(unquote(parsed.path))

    def player(self, inference: Inference) -> CheckpointPlayer:
        return inference.player(self.ref)

    def model(self, inference: Inference) -> CheckpointPlayer:
        """Released spelling; use player()."""
        warnings.warn(
            "Checkpoint.model() is now player(); removed in 0.5", DeprecationWarning, stacklevel=2
        )
        return self.player(inference)

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


def checkpoint_name(at: dict[str, float], *, padded: bool = False) -> str:
    """A checkpoint's file name under its run: where the first declared clock stood.

    Safe in any bucket and shell: the clock's name keeps letters, digits, `.`,
    `_` and `-`, anything else becomes `_`, and the value is a plain number. A
    different checkpoint at a coordinate already recorded is kept as
    `NAME.ordinal-N` (`etude.train.run.Run.checkpoint`). The exact clocks are
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
