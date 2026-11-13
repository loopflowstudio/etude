"""Typed jobs: work described as Python functions, so nobody writes a command.

    training(train, config=TrainConfig(lr=3e-4), max_hours=6, max_usd=Decimal(30))
    evaluation(progress, experiment="ppo", play=play, checkpoint=weights, max_hours=0.5, max_usd=Decimal(1))
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
a further target, seeded from the parent's out, or a selected
checkpoint. This module reads the parent's tracking and hands over verified
file inputs and lineage in `ETUDE_CONTINUES`.

This module joins the launcher with evaluation and tracking, as `etude.cli`
does, so the launcher itself never imports either.
"""

from __future__ import annotations

import inspect
import os
import shlex
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Required, TypedDict, Unpack, cast

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from etude.call import Function, config_type
from etude.carriage import CHECKPOINTS, OUT_PRESENT
from etude.eval.match import Result, Tables
from etude.eval.records import (
    Benchmark,
    CheckpointKey,
    CheckpointRef,
    Match,
    Model,
    Player,
    RoundRobin,
    digest,
)
from etude.eval.tournament import hold
from etude.experiment import Checkpoint, ExperimentState, checkpoint_file, checkpoint_inputs
from etude.launch.errors import LauncherError
from etude.launch.records import (
    Input,
    Job,
    Preparation,
    Run,
    RunRecord,
    Runtime,
    SmokeForm,
    Variable,
    number,
    parse_target,
)
from etude.out import MEMBERSHIP, OutFile, OutFiles
from etude.run.errors import EtudeError
from etude.run.listing import as_storage, declared, run_folders
from etude.run.records import ArtifactRef, Record, SourceKey
from etude.run.storage import Storage
from etude.train.read import read
from etude.train.records import ParentRun


class JobOptions(TypedDict, total=False):
    """How a typed job is launched: every `Job` field except those it derives.

    It derives the command and config, the experiment (an evaluation's own,
    else the one `etude launch` reads from an experiment file's name) and the
    smoke form: a training job's `smoke` config; an evaluation's is Etude's.
    """

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
    prepare: Preparation | None
    setup: str | None
    cleanup: str | None
    stop_seconds: float | None
    cleanup_seconds: float | None
    max_attempts: int


def training[C: BaseModel](
    train: Callable[[C], None],
    *,
    config: C,
    smoke: C | None = None,
    **options: Unpack[JobOptions],
) -> Job:
    """A job that calls `train(config)` in the job's own environment.

    `train` takes one parameter annotated with `config`'s Pydantic type and is
    defined at the top of a module the job's source can import. `smoke` is the
    config of its smoke form (`etude smoke`), checked the same way. Raises
    `LauncherError` when it is not, or when a config is not of that type or
    would not validate back into it.
    """
    job = _job(train, config, origin=train, options=options)
    if smoke is None:
        return job
    return job.model_copy(update={"smoke": SmokeForm(config=_config_text(train, smoke))})


class Evaluation(Record):
    """An evaluation job's config: what `_hold` holds, with what, on the machine."""

    tournament: Benchmark | RoundRobin
    # None in a job an experiment's eval returns: `etude eval` names it for the file.
    experiment: str | None
    play: Function
    # A benchmark's checkpoint, as the job's input of that name.
    checkpoint: Input | None = None
    # Checkpoints an experiment's eval chose to hold the benchmark for.
    checkpoints: tuple[Checkpoint, ...] = ()
    # A round robin's entrants.
    entrants: tuple[Player, ...] = ()
    workers: int = Field(default=1, ge=1)
    # The matches a pass found new (`etude.experiment.advance`), and the
    # job input holding each checkpoint they seat, by its key encoded.
    matches: tuple[Match, ...] | None = None
    files: dict[str, str] = {}


def evaluation(
    tournament: Benchmark | RoundRobin,
    *,
    play: Callable[[Match], Tables | Result],
    experiment: str | None = None,
    checkpoint: Input | None = None,
    checkpoints: Sequence[Checkpoint] = (),
    enter: Sequence[Player] = (),
    workers: int = 1,
    **options: Unpack[JobOptions],
) -> Job:
    """A job that holds `tournament` with the author's `play`, as one eval run of `experiment`.

    A benchmark is held for `checkpoint`, which reaches the machine as the
    job's input and fills every inference seat; a round robin plays every pair
    of `enter`. Each inference seat's config is in its matchup
    (`etude.eval.records.Inference`), so the tournament is the whole of what is
    evaluated. The eval run is the launched run, so `etude logs` and `status`
    lead to it and a retry resumes it.

    An experiment's `eval(state)` returns one with `checkpoints` instead, the
    ones it chose, or none for matchups of chosen checkpoints alone, and no
    `experiment`: `etude eval` names it for the file, finds what is new and
    plays it here or launches a job for exactly that. Launched as given, such
    a job is refused: a machine reads no other eval run, so cannot tell what
    is new. Any other job names its `experiment`.
    """
    if isinstance(tournament, Benchmark) and enter:
        raise LauncherError(f"{tournament.name} is a benchmark: give it checkpoints, no entrants")
    if checkpoint is not None and checkpoints:
        raise LauncherError("give one checkpoint input, or the checkpoints an experiment chose")
    if isinstance(tournament, RoundRobin) and (checkpoint is not None or checkpoints or not enter):
        raise LauncherError(f"{tournament.name} is a round robin: give it entrants, no checkpoint")
    if experiment is None and (checkpoint is not None or isinstance(tournament, RoundRobin)):
        raise LauncherError(f"name the experiment {tournament.name}'s eval run belongs to")
    if checkpoint is not None:
        options["inputs"] = (*options.get("inputs", ()), checkpoint)
    spec = Evaluation(
        tournament=tournament,
        experiment=experiment,
        play=_function(play, _repo(play, options)),
        checkpoint=checkpoint,
        checkpoints=tuple(checkpoints),
        entrants=tuple(enter),
        workers=workers,
    )
    return _job(_hold, spec, origin=play, options=options, experiment=experiment)


def evaluation_spec(job: Job) -> Evaluation:
    """What an evaluation job holds; raises `EtudeError` for any other job."""
    if job.config is None or "etude.jobs:_hold" not in job.command:
        raise EtudeError("not_an_evaluation_job", 422)
    return Evaluation.model_validate_json(job.config)


def with_matches(job: Job, matches: Sequence[Match], state: ExperimentState) -> Job:
    """The job playing exactly `matches`, with each checkpoint they seat as an input.

    A checkpoint is a file of the training run that saved it, so the launcher
    fetches it from that run's bucket folder or relays it from its last
    machine, verified by digest.
    """
    spec = evaluation_spec(job)
    saved = checkpoint_inputs(matches, state)
    # One input per checkpoint key, even where two keys hold the same bytes.
    files = {key: f"checkpoint-{digest(key)[:16]}{PurePosixPath(each.name).suffix}"
             for key, each in saved.items()}  # fmt: skip
    inputs = tuple(
        Input(
            name=files[key],
            key=checkpoint_file(each),
            sha256=cast(str, each.artifact.sha256),  # observe keeps only stored files
            size_bytes=cast(int, each.artifact.size_bytes),
            run=each.run,
        )
        for key, each in sorted(saved.items())
    )
    spec = spec.model_copy(update={"matches": tuple(matches), "files": files})
    return job.model_copy(
        update={"config": spec.model_dump_json(), "inputs": (*job.inputs, *inputs)}
    )


# The job input that carries a continuation's checkpoint file, before its suffix.
_PARENT_CHECKPOINT = "parent-checkpoint"


def continue_from(
    parent: RunRecord,
    until: str,
    *,
    source: Storage | Path | str,
    ordinal: int | None = None,
    config: BaseModel | None = None,
) -> Run:
    """A new run seeded with the parent's out, or a selected checkpoint.

    File inputs freeze the bytes chosen here by digest; a parent changing them
    before delivery is refused. Config is validated against the typed job's
    function before queueing. The target is absolute and must advance its clock.
    """
    storage = as_storage(source)
    keys = storage.keys("")
    folders = run_folders(keys).get(parent.id, [])
    declaration = declared(storage, folders, set(keys))
    if declaration is None:
        raise LauncherError(f"run {parent.id} has no tracked folder in {storage.name}")
    data = read(storage, declaration.experiment, parent.id)
    clock, position = parse_target(until)
    previous = next((v.value for v in parent.spec.environment if v.name == "ETUDE_CONTINUES"), None)
    inherited_input = None if previous is None else ParentRun.model_validate_json(previous).input
    inputs = {i.name: i for i in parent.spec.inputs if i.name != inherited_input and i.out is None}
    parent_key = SourceKey(namespace=declaration.namespace, kind="run", source_id=parent.id)
    # An explicit checkpoint needs no out reads, even when the parent has out.
    out = None if ordinal is not None else _parent_out(storage, folders, keys)
    if out is not None:
        # A recipe may save a checkpoint without logging scalar rows.
        at = {**(data.checkpoints[-1].at if data.checkpoints else {}), **data.reached}
        for index, member in enumerate(out.files):
            name = _input_name(f"parent-out-{index}", inputs)
            inputs[name] = Input(
                name=name,
                out=member.key.removeprefix("out/"),
                key=member.key,
                sha256=member.sha256,
                size_bytes=member.size_bytes,
                run=parent.id,
            )
        handed = ParentRun(parent=parent_key, at=at)
        origin = "parent"
    else:
        record = next((r for r in reversed(data.checkpoints) if ordinal in (None, r.ordinal)), None)
        if record is None or record.artifact.sha256 is None or record.artifact.size_bytes is None:
            raise LauncherError(f"run {parent.id} recorded no stored checkpoint {ordinal or ''}")
        at = record.at
        checkpoint = record.model_copy(update={"tags": ()})
        given = None
        file = PurePosixPath(record.artifact.uri)
        if record.file is not None or file.parent.parts[-2:] == (parent.id, CHECKPOINTS):
            key = record.file or f"{CHECKPOINTS}/{file.name}"
            given = _input_name(_PARENT_CHECKPOINT + file.suffix, inputs)
            inputs[given] = Input(
                name=given,
                key=key,
                sha256=record.artifact.sha256,
                size_bytes=record.artifact.size_bytes,
                run=parent.id,
            )
            checkpoint = checkpoint.model_copy(
                update={"artifact": record.artifact.model_copy(update={"uri": key})}
            )
        handed = ParentRun(parent=parent_key, checkpoint=checkpoint, input=given)
        origin = "checkpoint"
    if clock not in at:
        raise LauncherError(
            f"a continuation needs a target on a clock its {origin} recorded: {', '.join(sorted(at))}"
        )
    if position <= at[clock]:
        raise LauncherError(
            f"the target {until} is not beyond the {origin}'s {clock}={number(at[clock])}"
        )
    environment = (
        *(v for v in parent.spec.environment if v.name != "ETUDE_CONTINUES"),
        Variable(name="ETUDE_CONTINUES", value=handed.model_dump_json()),
    )
    text = parent.spec.config if config is None else _config_text(_called(parent.spec), config)
    job = parent.spec.model_copy(
        update={
            "until": until,
            "inputs": tuple(inputs.values()),
            "environment": environment,
            "config": text,
        }
    )
    return Run(job=job, seed=parent.seed, params=dict(parent.params), source=parent.source)


def _parent_out(storage: Storage, folders: Sequence[str], keys: Sequence[str]) -> OutFiles | None:
    """Choose one physical out; absent membership requires reading and hashing its files.

    None means no out exists, so continuation may use a checkpoint. Empty out
    is a deliberate state and must not resurrect retained checkpoint bytes.
    """
    for folder in folders:
        if folder + MEMBERSHIP in keys:
            return OutFiles.model_validate_json(storage.get(folder + MEMBERSHIP))
        out_keys = sorted(key for key in keys if key.startswith(folder + "out/"))
        if not out_keys and folder + OUT_PRESENT not in keys:
            continue
        files: list[OutFile] = []
        for full_key in out_keys:
            key = full_key[len(folder) :]
            if any(part.startswith(".") for part in key.split("/")) or key.endswith(
                (".partial", ".open")
            ):
                continue
            body = storage.get(full_key)
            files.append(OutFile(key=key, sha256=sha256(body).hexdigest(), size_bytes=len(body)))
        return OutFiles(files=tuple(files))
    return None


def _input_name(name: str, inputs: Mapping[str, Input]) -> str:
    """Choose a transport name without taking any name the job already uses."""
    while name in inputs:
        name += "-"
    return name


def _called(job: Job) -> Callable[..., object]:
    """Load the function of a typed job; command jobs have no config model."""
    command = shlex.split(job.command)
    if "etude.call" not in command or "--function" not in command:
        raise LauncherError("config changes require a typed job")
    function = command[command.index("--function") + 1]
    path = command[command.index("--path") + 1] if "--path" in command else ""
    module, _, name = function.partition(":")
    return Function(module=module, name=name, path=path).load(job.repo)


def continued_config(parent: RunRecord, changes: str) -> BaseModel:
    """Validate a JSON object of changes against the parent's typed config."""
    kind = config_type(_called(parent.spec))
    values = kind.model_validate_json(parent.spec.config or "{}").model_dump(mode="json")
    try:
        fields = TypeAdapter(dict[str, JsonValue]).validate_json(changes)
        unknown = fields.keys() - kind.model_fields.keys()
        if unknown:
            raise LauncherError(f"unknown continuation config fields: {', '.join(sorted(unknown))}")
        return kind.model_validate({**values, **fields})
    except ValidationError as error:
        raise LauncherError(f"invalid continuation config: {error}") from None


def _hold(evaluation: Evaluation) -> None:
    """On the machine: hold the tournament, with the checkpoint inputs in their seats."""
    # Loaded by import path: the function `evaluation` was given, typed there.
    play = cast(Callable[[Match], Tables | Result], evaluation.play.load())
    if evaluation.experiment is None or (evaluation.checkpoints and evaluation.matches is None):
        raise SystemExit("etude: an experiment's evaluation job is launched by `etude eval`")
    folder = os.environ.get("ETUDE_INPUTS_DIR")
    if (evaluation.checkpoint is not None or evaluation.files) and not folder:
        raise SystemExit("etude: an evaluation job's checkpoints arrive as inputs")
    checkpoint = None
    if evaluation.checkpoint is not None:
        given = evaluation.checkpoint
        # A training run's file is keyed by its run and name; any other, by where it lies.
        key = (
            CheckpointKey(
                run=given.run,
                name=given.key if given.key.startswith("out/") else PurePosixPath(given.key).name,
            )
            if given.run is not None
            else CheckpointKey(address=given.key)
        )
        artifact = ArtifactRef(
            uri=(Path(str(folder)) / given.name).as_uri(),
            sha256=given.sha256,
            size_bytes=given.size_bytes,
        )
        checkpoint = CheckpointRef(key=key, artifact=artifact)
    matches = None
    if evaluation.matches is not None:
        here = {key: (Path(str(folder)) / name).as_uri() for key, name in evaluation.files.items()}
        matches = [_located(match, here) for match in evaluation.matches]
    hold(
        evaluation.tournament,
        experiment=evaluation.experiment,
        play=play,
        checkpoint=checkpoint,
        checkpoints=tuple(each.ref for each in evaluation.checkpoints),
        enter=evaluation.entrants,
        matches=matches,
        workers=evaluation.workers,
    )


def _located(match: Match, here: Mapping[str, str]) -> Match:
    """The match with each model's checkpoint file where it lies on this machine."""
    players: list[Player] = []
    for player in match.players:
        if not isinstance(player, Model) or str(player.checkpoint.key) not in here:
            players.append(player)
            continue
        checkpoint = player.checkpoint
        moved = checkpoint.artifact.model_copy(update={"uri": here[str(checkpoint.key)]})
        players.append(
            player.model_copy(
                update={"checkpoint": checkpoint.model_copy(update={"artifact": moved})}
            )
        )
    return match.model_copy(update={"players": tuple(players)})


def _job(
    function: Callable[..., object],
    config: BaseModel,
    *,
    origin: Callable[..., object],
    options: JobOptions,
    experiment: str | None = None,
) -> Job:
    """The job that calls `function(config)`; `origin` is the author's function it brings."""
    text = _config_text(function, config)
    repo = _repo(origin, options)
    called = _function(function, repo)
    sourced: JobOptions = {**options, "repo": None if repo is None else str(repo)}
    if "prepare" not in options and options.get("runtime") is None and repo is not None:
        derived = _uv_preparation(repo)
        if derived is not None:
            sourced["prepare"] = derived
    prepared = sourced.get("prepare")
    # A named interpreter is selected as ETUDE_PYTHON; otherwise the checkout is a uv project.
    named = options.get("runtime") is not None or (
        prepared is not None and prepared.python is not None
    )
    python = '"$ETUDE_PYTHON"' if named else "uv run python"
    command = f"{python} -m etude.call --function {shlex.quote(str(called))}"
    if called.path:
        command += f" --path {shlex.quote(called.path)}"
    return Job(**sourced, command=command, config=text, experiment=experiment)


def _config_text(function: Callable[..., object], config: BaseModel) -> str:
    """`config` as the job's text, refused unless `function` takes it and it validates back."""
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
    return text


def _uv_preparation(repo: Path) -> Preparation | None:
    """The dependency environment of an unpackaged uv project, prepared once per lock.

    uv installs only the dependencies of a project it does not package (no build
    system, or `tool.uv.package = false`), so a venv keyed by `pyproject.toml`
    and `uv.lock` (and `.python-version`) is exactly what `uv run` would use,
    and the job imports its own code from the checkout as before. A packaged
    project, a workspace or a path source needs more of the source, so it keeps
    `uv run` unless the job declares its own `prepare`. Reads the working tree's
    `pyproject.toml`; the key covers the launched commit's content.
    """
    project, lock = repo / "pyproject.toml", repo / "uv.lock"
    if not (project.is_file() and lock.is_file()):
        return None
    data = tomllib.loads(project.read_text())
    uv = data.get("tool", {}).get("uv", {})
    package = uv.get("package")
    if (package if package is not None else "build-system" in data) or "workspace" in uv:
        return None
    for source in uv.get("sources", {}).values():
        given = source if isinstance(source, list) else [source]
        if any("path" in entry or "workspace" in entry for entry in given):
            return None
    return Preparation(
        command=(
            'UV_PROJECT_ENVIRONMENT="$ETUDE_PREPARED_DIR/.venv"'
            " uv sync --frozen --no-install-project"
        ),
        # uv chooses the interpreter from .python-version, so it decides the result too.
        paths=("pyproject.toml", "uv.lock", *_present(repo, ".python-version")),
        python=".venv/bin/python",
    )


def _present(repo: Path, *names: str) -> tuple[str, ...]:
    return tuple(name for name in names if (repo / name).is_file())


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
