"""Typed jobs: work described as Python functions, so nobody writes a command.

    training(train, config=TrainConfig(lr=3e-4), max_hours=6, max_usd=Decimal(30))
    evaluation(progress, play=play, checkpoint=weights, max_hours=0.5, max_usd=Decimal(1))
    continue_from(parent, "steps=63000", source="s3://bucket/etude/launch")

Each returns an `etude.launch.records.Job` whose command Etude decides: it runs
`python -m etude.call` (`etude.call`) in the job's own environment, which
imports the named function and calls it with the job's config. A training job
calls the author's `train(config)`. An evaluation job calls Etude's `_hold`,
which holds a tournament (`etude.eval.tournament.hold`) with the author's
`play` and the checkpoint the job is given as an input.

Functions are named by import path from the job's source checkout, which is the
git checkout their file is in unless `repo` says otherwise. Configs are
Pydantic models, stored as the job's config text and validated back into the
function's parameter type on the machine; a config that would not survive that
is refused here, before anything is launched.

A continuation (`continue_from`) is a new run of a parent's job and seed with
a further target, which starts from one of the parent's recorded checkpoints.
The launcher has no checkpoints: this module reads the parent's tracked
record and hands it to the job as an input and `ETUDE_CONTINUES`.

This module joins the launcher with evaluation and tracking, as `etude.cli`
does, so the launcher itself never imports either.
"""

from __future__ import annotations

import inspect
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Required, TypedDict, Unpack, cast

from pydantic import BaseModel, Field, ValidationError

from etude.call import Function, config_type
from etude.carriage import CHECKPOINTS
from etude.eval.match import Tables
from etude.eval.records import Agent, Benchmark, Match, RoundRobin
from etude.eval.tournament import hold
from etude.launch.errors import LauncherError
from etude.launch.records import (
    Input,
    Job,
    Run,
    RunRecord,
    Runtime,
    Variable,
    number,
    parse_target,
)
from etude.run.listing import as_storage, declared, places
from etude.run.records import ArtifactRef, Record, SourceKey
from etude.run.storage import Storage
from etude.train.read import read
from etude.train.records import ParentCheckpoint


class JobOptions(TypedDict, total=False):
    """How a typed job is launched: every `Job` field except the command and config it derives."""

    max_hours: Required[float]
    max_usd: Decimal | None
    name: str
    until: str | None
    # A local git repository; None runs in an empty directory. Omitted: the
    # checkout the function's file is in.
    repo: str | None
    revision: str | None
    inputs: tuple[Input, ...]
    environment: tuple[Variable, ...]
    runtime: Runtime | None
    setup: str | None
    cleanup: str | None
    stop_seconds: float | None
    cleanup_seconds: float | None
    max_attempts: int


def training[C: BaseModel](
    train: Callable[[C], None], *, config: C, **options: Unpack[JobOptions]
) -> Job:
    """A job that calls `train(config)` in the job's own environment.

    `train` takes one parameter annotated with `config`'s Pydantic type and is
    defined at the top of a module the job's source can import. Raises
    `LauncherError` when it is not, or when `config` is not of that type or
    would not validate back into it.
    """
    return _job(train, config, origin=train, options=options)


class Evaluation(Record):
    """An evaluation job's config: what `_hold` holds, with what, on the machine."""

    tournament: Benchmark | RoundRobin
    play: Function
    # A benchmark's checkpoint, as the job's input of that name.
    checkpoint: Input | None = None
    # A round robin's entrants.
    entrants: tuple[Agent, ...] = ()
    workers: int = Field(default=1, ge=1)


def evaluation(
    tournament: Benchmark | RoundRobin,
    *,
    play: Callable[[Match], Tables],
    checkpoint: Input | None = None,
    enter: Sequence[Agent] = (),
    workers: int = 1,
    **options: Unpack[JobOptions],
) -> Job:
    """A job that holds `tournament` with the author's `play`, as one eval run.

    A benchmark is held for `checkpoint`, which reaches the machine as the
    job's input and fills every model seat; a round robin plays every pair of
    `enter`. Each model's config is in its seat (`etude.eval.records.Model`),
    so the tournament is the whole of what is evaluated. A launched job reads
    no other eval run, so holding nothing new (batch) is refused: give a
    checkpoint or entrants. The eval run is the launched run, so `etude logs`
    and `status` lead to it and a retry resumes it.
    """
    if isinstance(tournament, Benchmark) and (checkpoint is None or enter):
        raise LauncherError(f"{tournament.name} is a benchmark: give it a checkpoint, no entrants")
    if isinstance(tournament, RoundRobin) and (checkpoint is not None or not enter):
        raise LauncherError(f"{tournament.name} is a round robin: give it entrants, no checkpoint")
    if checkpoint is not None:
        options["inputs"] = (*options.get("inputs", ()), checkpoint)
    spec = Evaluation(
        tournament=tournament,
        play=_function(play, _repo(play, options)),
        checkpoint=checkpoint,
        entrants=tuple(enter),
        workers=workers,
    )
    return _job(_hold, spec, origin=play, options=options)


# The job input that carries a continuation's checkpoint file, before its suffix.
_PARENT_CHECKPOINT = "parent-checkpoint"


def continue_from(
    parent: RunRecord,
    until: str,
    *,
    source: Storage | Path | str,
    ordinal: int | None = None,
) -> Run:
    """The parent's job and seed again, from one of its recorded checkpoints, to `until`.

    `source` holds the parent's checkpoint records: the bucket its machine
    wrote to (`s3://bucket/prefix`), or a runs directory. The checkpoint is
    the parent's latest, or the one numbered `ordinal`. `until` is
    "clock=value" on a clock the checkpoint recorded, beyond where it stood;
    it is absolute. A file stored in the parent's folder reaches the machine
    as an input of the parent run (`Input.run`), verified by digest; a
    reference to a model elsewhere is handed as recorded. Raises
    `LauncherError` when the parent recorded no such checkpoint or the target
    is not beyond it.
    """
    storage = as_storage(source)
    folders = places(storage).get(parent.id)
    declaration = None if folders is None else declared(storage, folders)
    if folders is None or declaration is None:
        raise LauncherError(f"run {parent.id} has no tracked folder in {storage.name}")
    recorded = read(storage, declaration.experiment, parent.id).checkpoints
    record = next((r for r in reversed(recorded) if ordinal in (None, r.ordinal)), None)
    if record is None or record.artifact.sha256 is None or record.artifact.size_bytes is None:
        raise LauncherError(f"run {parent.id} recorded no stored checkpoint {ordinal or ''}")
    clock, position = parse_target(until)
    if clock not in record.at:
        raise LauncherError(
            f"a continuation needs a target on a clock its checkpoint recorded: "
            f"{', '.join(sorted(record.at))}"
        )
    if position <= record.at[clock]:
        raise LauncherError(
            f"the target {until} is not beyond the checkpoint's {clock}={number(record.at[clock])}"
        )
    inputs = tuple(i for i in parent.spec.inputs if not i.name.startswith(_PARENT_CHECKPOINT))
    # Without tags: they label the parent's checkpoint after the fact and decide
    # nothing about the continuation, so they stay out of its job identity.
    checkpoint = record.model_copy(update={"tags": ()})
    given = None
    file = PurePosixPath(record.artifact.uri)
    if file.parent.parts[-2:] == (parent.id, CHECKPOINTS):
        # Stored in the parent's folder: the launcher gives the machine that file
        # of the run, from its bucket or relayed from the parent's last machine.
        # Addressed within that folder, so where its records were read from does
        # not make the same continuation a different job.
        key = f"{CHECKPOINTS}/{file.name}"
        given = _PARENT_CHECKPOINT + file.suffix
        inputs += (
            Input(
                name=given,
                key=key,
                sha256=record.artifact.sha256,
                size_bytes=record.artifact.size_bytes,
                run=parent.id,
            ),
        )
        artifact = record.artifact.model_copy(update={"uri": key})
        checkpoint = checkpoint.model_copy(update={"artifact": artifact})
    handed = ParentCheckpoint(
        parent=SourceKey(namespace=declaration.namespace, kind="run", source_id=parent.id),
        checkpoint=checkpoint,
        input=given,
    )
    environment = (
        *(v for v in parent.spec.environment if v.name != "ETUDE_CONTINUES"),
        Variable(name="ETUDE_CONTINUES", value=handed.model_dump_json()),
    )
    job = parent.spec.model_copy(
        update={"until": until, "inputs": inputs, "environment": environment}
    )
    return Run(job=job, seed=parent.seed, params=dict(parent.params), source=parent.source)


def _hold(evaluation: Evaluation) -> None:
    """On the machine: hold the tournament, with the checkpoint input in the model's seats."""
    # Loaded by import path: the function `evaluation` was given, typed there.
    play = cast(Callable[[Match], Tables], evaluation.play.load())
    checkpoint = None
    if evaluation.checkpoint is not None:
        given = evaluation.checkpoint
        folder = os.environ.get("ETUDE_INPUTS_DIR")
        if not folder:
            raise SystemExit("etude: an evaluation job's checkpoint arrives as an input")
        checkpoint = ArtifactRef(
            uri=(Path(folder) / given.name).as_uri(),
            sha256=given.sha256,
            size_bytes=given.size_bytes,
        )
    hold(
        evaluation.tournament,
        play=play,
        checkpoint=checkpoint,
        enter=evaluation.entrants,
        workers=evaluation.workers,
    )


def _job(
    function: Callable[..., object],
    config: BaseModel,
    *,
    origin: Callable[..., object],
    options: JobOptions,
) -> Job:
    """The job that calls `function(config)`; `origin` is the author's function it brings."""
    try:
        kind = config_type(function)
    except TypeError as error:
        raise LauncherError(str(error)) from None
    if not isinstance(config, kind):
        raise LauncherError(
            f"{function.__qualname__} takes a {kind.__name__} config, not {type(config).__name__}"
        )
    text = config.model_dump_json()
    try:
        kind.model_validate_json(text)
    except ValidationError as error:
        raise LauncherError(
            f"the {kind.__name__} config would not validate on the machine: {error}"
        ) from None
    repo = _repo(origin, options)
    called = _function(function, repo)
    # A preinstalled runtime names its interpreter; otherwise the checkout is a uv project.
    python = '"$ETUDE_PYTHON"' if options.get("runtime") is not None else "uv run python"
    command = f"{python} -m etude.call --function {shlex.quote(str(called))}"
    if called.path:
        command += f" --path {shlex.quote(called.path)}"
    sourced: JobOptions = {**options, "repo": None if repo is None else str(repo)}
    return Job(**sourced, command=command, config=text)


def _repo(origin: Callable[..., object], options: JobOptions) -> Path | None:
    """The job's source: `repo` when given, else the git checkout `origin`'s file is in."""
    if "repo" in options:
        given = options["repo"]
        return None if given is None else Path(given).resolve()
    file = Path(inspect.getfile(origin)).resolve()
    for folder in file.parents:
        if (folder / ".git").exists():
            return folder
    raise LauncherError(f"{file} is in no git checkout; name the job's source with repo=")


def _function(function: Callable[..., object], repo: Path | None) -> Function:
    """Name `function` by import path, from where its module sits in `repo`.

    A module installed in this environment is imported by name alone, as in
    the job's; one in the checkout is imported from the folder its name starts in.
    """
    module, name = function.__module__, function.__qualname__
    if module in ("__main__", "<run_path>") or "<" in name:
        raise LauncherError(
            f"{name} must be defined at the top of an importable module, so the job can import it"
        )
    file = getattr(sys.modules.get(module), "__file__", None)
    if repo is None or file is None:
        return Function(module=module, name=name)
    where = Path(file).resolve()
    if where.is_relative_to(Path(sys.prefix).resolve()) or not where.is_relative_to(repo):
        return Function(module=module, name=name)
    relative = where.relative_to(repo).parts
    named = (*module.split("."), "__init__.py") if where.name == "__init__.py" else None
    expected = named or (*module.split(".")[:-1], module.split(".")[-1] + ".py")
    if relative[-len(expected) :] != expected:
        raise LauncherError(
            f"{module} is at {'/'.join(relative)} in {repo}, where the job cannot import it"
            f" as {module}"
        )
    return Function(module=module, name=name, path="/".join(relative[: -len(expected)]))
