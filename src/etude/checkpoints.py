"""Checkpoint contracts shared by every kind of run: identity, reference and record.

Training publishes `CheckpointRecord`s; evaluation seats their `CheckpointRef`s.
A checkpoint's identity is its key (`_checkpoint_id`): its experiment, run,
role and portable name, or an outside address. Its digest verifies bytes and names nothing.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self
from urllib.parse import unquote, urlparse

from pydantic import (
    BeforeValidator,
    Field,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from etude.carriage import checkpoint_file_name, within
from etude.records import ArtifactRef, Name, Record


def _checkpoint_id(
    *,
    experiment: str | None = None,
    run: str | None = None,
    name: str | None = None,
    address: str | None = None,
    role: str = "policy",
) -> str:
    """A checkpoint's identity by run and portable name, or an outside address."""
    if (run is None) != (name is None) or (run is None) == (address is None):
        raise ValueError("checkpoint identity needs a run and name, or an address")
    if address is not None:
        if not address:
            raise ValueError("checkpoint address must not be empty")
        return address
    assert run is not None and name is not None
    if experiment is None:
        raise ValueError("checkpoint identity needs experiment= for a run")
    if not within(run) or not within(name) or (experiment and not within(experiment)):
        raise ValueError("checkpoint identity must use portable relative names")
    key = name if name.startswith("out/") else "checkpoints/" + name
    if not role or "/" in role or not within(role):
        raise ValueError("checkpoint role must be a portable name")
    return (experiment + "/" if experiment else "") + in_role(run, role) + "/" + key


def in_role(run: str, role: str) -> str:
    """A run or line name as a role's: itself for the default role, `policy`."""
    return run if role == "policy" else f"{run}/roles/{role}"


def seen_from(ref: CheckpointRef, experiment: str | None) -> CheckpointRef:
    """`ref` as an evaluation of `experiment` seats it: another experiment's checkpoint
    names that experiment in its line, so a report never joins it to a learner of this one.
    The owner is the ref's `policy`; a ref without one is unchanged."""
    owner = ref.policy.experiment if ref.policy is not None else None
    if ref.line is None or not owner or not experiment or owner == experiment:
        return ref
    return ref.model_copy(update={"observer": experiment})


def qualified(key: str, experiment: str) -> str:
    """A released run key (`RUN/checkpoints/NAME` or `RUN/out/...`) of `experiment`'s eval
    run, spelled with that experiment; any other key unchanged."""
    parts = key.split("/", 2)
    released = len(parts) == 3 and parts[1] in ("checkpoints", "out")
    return f"{experiment}/{key}" if experiment and released else key


def _identity(value: object, info: ValidationInfo) -> object:
    # Released evaluations stored a structured key; identity remains its encoded text.
    if isinstance(value, dict):
        if set(value) - {"run", "name", "address"}:
            raise ValueError("unknown checkpoint identity fields")
        fields = {name: value.get(name) for name in ("run", "name", "address")}
        if not all(each is None or isinstance(each, str) for each in fields.values()):
            raise ValueError("checkpoint identity fields must be strings")
        value = _checkpoint_id(
            experiment="", run=fields["run"], name=fields["name"], address=fields["address"]
        )
    if isinstance(value, str) and info.context and info.context.get("experiment"):
        return qualified(value, str(info.context["experiment"]))
    return value


CheckpointId = Annotated[str, BeforeValidator(_identity), Field(min_length=1)]


class PolicyKey(Record):
    """One policy: an experiment, job, seed and role, with defaults filled by its state."""

    experiment: str | None = None
    job: str | None = None
    seed: int | None = None
    role: Name | None = None

    def checkpoint(
        self, *, name: str, artifact: ArtifactRef, at: dict[str, float] | None = None
    ) -> CheckpointRef:
        """Name a checkpoint of this policy; omitted parts resolve when held with lines."""
        return CheckpointRef.model_validate(
            {
                **self.model_dump(exclude_unset=True),
                "name": name,
                "artifact": artifact,
                "at": at or {},
            }
        )

    @property
    def _run(self) -> str:
        """The canonical run name; unresolved policy parts must be filled by a state."""
        if self.job is None:
            raise ValueError("checkpoint policy needs a resolved job")
        return self.job if self.seed is None else f"{self.job}@{self.seed}"


class CheckpointRef(Record):
    """A policy's named checkpoint, or an outside address, and its verified bytes.

    `name` is a portable position. Records of continuations and historical runs
    retain their run-relative position (`RUN/checkpoints/NAME` or `RUN/out/...`)
    when it differs from the policy's canonical run. No policy parts are parsed
    from stored keys: released keys remain opaque addresses.
    """

    policy: PolicyKey | None = None
    name: str | None = None
    address: CheckpointId | None = None
    artifact: ArtifactRef
    at: dict[str, float] = {}
    # The observing experiment changes only the report line, never identity.
    observer: str | None = Field(default=None, exclude_if=lambda value: value is None)
    # Only outside references own a line; policy references derive theirs.
    outside_line: str | None = Field(default=None, validation_alias="line", exclude=True)

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            artifact: ArtifactRef,
            experiment: str | None = None,
            job: str | None = None,
            seed: int | None = None,
            role: str | None = None,
            name: str | None = None,
            address: str | None = None,
            at: dict[str, float] = {},
            line: str | None = None,
        ) -> None: ...

    @model_validator(mode="before")
    @classmethod
    def _parts(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        # Published references carried a key and redundant policy/line provenance.
        # Read that spelling once, retaining the exact identity as an address.
        if "key" in data:
            data["address"] = data.pop("key")
            data.pop("policy", None)
        elif "policy" in data:
            raise ValueError("give flat policy parts, not policy=")
        elif data.get("address") is None:
            data["policy"] = {
                part: data.pop(part)
                for part in ("experiment", "job", "seed", "role")
                if part in data
            }
        if data.get("policy") is not None and data.pop("line", None) is not None:
            raise ValueError("a policy checkpoint derives its line")
        return data

    @model_validator(mode="after")
    def verifiable(self) -> Self:
        if self.artifact.sha256 is None:
            raise ValueError("model_checkpoint_requires_digest")
        if (self.policy is None) == (self.address is None):
            raise ValueError("checkpoint needs policy parts and name, or address")
        if self.policy is not None:
            if self.name is None or not within(self.name):
                raise ValueError("checkpoint needs a portable name")
        elif self.name is not None:
            raise ValueError("outside checkpoint takes address, not name")
        return self

    @property
    def key(self) -> str:
        if self.address is not None:
            return self.address
        assert self.policy is not None and self.name is not None
        policy = self.policy
        if policy.experiment is None:
            raise ValueError("checkpoint policy needs a resolved experiment")
        # A run-relative position preserves continuation identity without a second key.
        parts = self.name.split("/", 2)
        positioned = (
            not self.name.startswith("out/")
            and len(parts) == 3
            and parts[1] in ("checkpoints", "out")
        )
        return _checkpoint_id(
            experiment=policy.experiment,
            run=parts[0] if positioned else policy._run,
            name="/".join(parts[1:]).removeprefix("checkpoints/") if positioned else self.name,
            role=policy.role or "policy",
        )

    @property
    def line(self) -> str | None:
        if self.policy is None:
            return self.outside_line
        line = in_role(self.policy._run, self.policy.role or "policy")
        owner = self.policy.experiment
        return f"{owner}/{line}" if owner and self.observer and owner != self.observer else line

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data: dict[str, object] = handler(self)
        data.pop("policy", None)
        if self.policy is not None:
            data.update(self.policy.model_dump(exclude_unset=True))
        elif self.outside_line is not None:
            data["line"] = self.outside_line
        return data

    @property
    def path(self) -> Path:
        """This checkpoint on the current machine; remote addresses need an input first."""
        parsed = urlparse(self.artifact.uri)
        if parsed.scheme not in ("", "file") or parsed.netloc:
            raise ValueError("checkpoint is not on this machine; give it to a job as an input")
        return Path(unquote(parsed.path))


class _Seat[P](Protocol):
    """A checkpoint seat (`etude.eval.records.Held`); evaluation is not imported here."""

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
    role: Name = "policy"
    policy: PolicyKey | None = None
    ordinal: int = Field(ge=1)
    artifact: ArtifactRef
    # A stored file's key relative to the run folder. None for older records
    # (beside this record) and references to files outside the tracked folder.
    file: str | None = None
    # Resolved source prefix, supplied by readers; stored checkpoint identity is unchanged.
    folder: str | None = Field(default=None, exclude=True)
    at: dict[Name, float]
    wall_ms: int | None = None
    # The author's labels, such as "best". They may be added and removed later.
    tags: tuple[Name, ...] = Field(default=(), max_length=32)

    def __eq__(self, other: object) -> bool:
        """Resolved source folders are read provenance, not persisted checkpoint data."""
        return isinstance(other, CheckpointRecord) and self.model_dump() == other.model_dump()

    @property
    def name(self) -> str:
        return (
            self.file
            if self.file is not None and self.file.startswith("out/")
            else checkpoint_file_name(self.artifact.uri)
        )

    @property
    def key(self) -> str:
        return _checkpoint_id(
            experiment=self.experiment, run=self.run, name=self.name, role=self.role
        )

    @property
    def ref(self) -> CheckpointRef:
        if self.policy is None:
            return CheckpointRef(
                address=self.key,
                artifact=self.artifact,
                at=self.at,
                line=in_role(self.line or self.run, self.role),
            )
        policy = self.policy.model_copy(update={"experiment": self.experiment, "role": self.role})
        position = self.name
        if self.run != policy._run:
            position = (
                self.run
                + "/"
                + (position if position.startswith("out/") else "checkpoints/" + position)
            )
        return policy.checkpoint(name=position, artifact=self.artifact, at=self.at)

    @property
    def path(self) -> Path:
        """This checkpoint on the current machine; remote addresses need an input first."""
        return self.ref.path

    def player[P](self, seat: _Seat[P]) -> P:
        return seat.player(self.ref)

    def model[P](self, seat: _Seat[P]) -> P:
        """Released spelling; use player()."""
        warnings.warn(
            "Checkpoint.model() is now player(); removed in 0.5", DeprecationWarning, stacklevel=2
        )
        return self.player(seat)

    @field_validator("file")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        if value is not None and not within(value):
            raise ValueError("checkpoint file must be relative to its run")
        return value
