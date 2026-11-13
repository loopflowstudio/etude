"""Checkpoint contracts shared by every kind of run: identity, reference and record.

Training publishes `CheckpointRecord`s; evaluation seats their `CheckpointRef`s.
A checkpoint's identity is its key (`checkpoint_id`): its run and portable
name, or an outside address. Its digest verifies bytes and names nothing.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from urllib.parse import unquote, urlparse

from pydantic import BeforeValidator, Field, field_validator, model_validator

from etude.carriage import checkpoint_file_name, within
from etude.records import ArtifactRef, Name, Record


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


class _Inference[P](Protocol):
    """How a checkpoint plays (`etude.eval.records.Inference`); evaluation is not imported here."""

    def player(self, checkpoint: CheckpointRef) -> P: ...


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

    def player[P](self, inference: _Inference[P]) -> P:
        return inference.player(self.ref)

    def model[P](self, inference: _Inference[P]) -> P:
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
