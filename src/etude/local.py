"""Call a training recipe here, with an explicit new or resumed run identity.

`train` validates identity before calling user code. Its temporary process
environment and signal handlers belong to this one synchronous attempt.
"""

from __future__ import annotations

import json
import math
import os
import runpy
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import FrameType
from typing import cast

from pydantic import BaseModel

from etude.cache import Artifact, Content, ContentMismatch, Corrupt
from etude.call import changed_config, config_type
from etude.declarations import find_training_run, format_known_run, read_training_jobs, select_runs
from etude.errors import EtudeError, LauncherError
from etude.experiment import observe
from etude.experiment_file import defined_functions, refuse_renamed
from etude.identity import run_address
from etude.jobs import load_job_function
from etude.records import RunRecord
from etude.run import RunFiles, run_name_taken, validate_resume
from etude.spec import Input, Until, Variable
from etude.stopping import Stopped
from etude.storage import DirectoryStorage, default_root, place_artifact, run_directory
from etude.train.read import read
from etude.train.state import saved_states


def train(
    file: Path,
    *,
    config: str | None = None,
    seed: int | None = None,
    job: str | None = None,
    experiment: str | None = None,
    resume: bool = False,
    until: Until | None = None,
    seconds: float | None = None,
    json_output: bool = False,
) -> int:
    """Train the selected jobs' seeds here, stopping the selection after a saved stop.

    Files with only `train(config)` define one job with seed 0. Files with
    `training_jobs()` use exactly the same declarations and addresses as a launch.
    """
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("--seconds must be finite and positive")
    if file.suffix != ".py":
        raise ValueError("a Python file defining train(config) or training_jobs() is required")
    file = file.resolve()
    label = experiment or file.stem
    functions = defined_functions(file)
    refuse_renamed(file)
    if "training_jobs" in functions:
        state = observe(default_root(), label)
        requests = select_runs(
            read_training_jobs(file, experiment=experiment, local=True, state=state),
            job=job,
            seed=seed,
        )
        with TemporaryDirectory(prefix="etude-inputs-") as folder:
            prepared: list[_Training | _Finished] = []
            refusals: list[str] = []
            for request in requests:
                assert request.id is not None  # read_training_jobs addresses every run
                try:
                    prior = find_training_run(request, state)
                    known = format_known_run(request, prior)
                    if known is not None and job is None and seed is None and not resume:
                        print(known, file=sys.stderr if json_output else sys.stdout)
                        continue
                    identity = run_address(prior.name, label) if prior is not None else request.id
                    directory = Path(folder) / "runs" / request.id
                    attempt = _prepare(
                        file,
                        load_job_function(request.job, current=True),
                        label=request.job.experiment or label,  # a job may name its own
                        identity=identity,
                        job=request.job.name,
                        initial=request.job.config,
                        config=config,
                        seed=request.seed,
                        resume=resume,
                        until=until or request.job.until,
                        seconds=seconds,
                        known=prior is not None,
                        environment=request.job.environment,
                        inputs_directory=directory,
                    )
                    if isinstance(attempt, _Training):
                        _place_inputs(request.job.inputs, directory, Path(folder) / "artifacts")
                    prepared.append(attempt)
                except (EtudeError, LauncherError, ValueError, TypeError) as error:
                    if len(requests) == 1:
                        raise
                    refusals.append(f"{request.id}: {error}")
            if refusals:
                raise EtudeError("selection_refused", detail="\n".join(refusals))
            return _train_each(prepared, json_output=json_output)
    if job not in (None, file.stem) or seed not in (None, 0):
        raise ValueError(
            "select the file's sole job and seed 0; define training_jobs() for other jobs or seeds"
        )
    sys.path.insert(0, str(file.parent))
    try:
        found = runpy.run_path(str(file)).get("train")
        if not callable(found):
            raise ValueError(f"{file.name}: define train(config) or training_jobs()")
        return _train_each(
            [
                _prepare(
                    file,
                    cast(Callable[[BaseModel], object], found),
                    label=label,
                    identity=label,
                    job=file.stem,
                    initial=None,
                    config=config,
                    seed=0,
                    resume=resume,
                    until=until,
                    seconds=seconds,
                )
            ],
            json_output=json_output,
        )
    finally:
        sys.path.remove(str(file.parent))


def _prepare(
    file: Path,
    recipe: Callable[..., object],
    *,
    label: str,
    identity: str,
    job: str,
    initial: str | None,
    config: str | None,
    seed: int | None,
    resume: bool,
    until: Until | None,
    seconds: float | None,
    known: bool = False,
    environment: tuple[Variable, ...] = (),
    inputs_directory: Path | None = None,
) -> _Training | _Finished:
    """Check one run's identity and build its attempt, touching nothing stored.

    A known run that stored nothing here (a launch cancelled or lost before its
    first save) has no attempt to resume, so `resume` starts its first one.
    """
    base = default_root()
    root = run_directory(base, label, identity)[1]
    prior = RunFiles(root).run_file()
    kind = config_type(recipe)
    if resume and prior is None:
        if not known:
            raise EtudeError("run_not_found", 404, detail=run_address(identity, label))
        resume = False
    # Given fields change the declared config; an omitted config resumes the stored one.
    stored = prior.config_json if prior is not None and resume and config is None else initial
    settings = changed_config(kind, "{}" if config is None else config, stored=stored)
    # Validate the actual JSON that another machine would receive.
    text = settings.model_dump_json(round_trip=True)
    settings = kind.model_validate_json(text)
    if prior is not None and resume:
        resumed = _resumed(base, root, prior, label, identity, config=text, seed=seed, until=until)
        if isinstance(resumed, _Finished):
            return resumed
        seed, until = resumed
    elif prior is not None:
        validate_resume(prior, config=text, seed=seed)
        raise run_name_taken(run_address(identity, label))
    commit = subprocess.run(
        ["git", "-C", str(file.parent), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    values = {
        **{v.name: v.value for v in environment},
        "ETUDE_EXPERIMENT": label,
        "ETUDE_RUN": identity.rsplit("/", 1)[-1],
        "ETUDE_RUN_ADDRESS": run_address(identity, label),
        "ETUDE_JOB_NAME": job,
        "ETUDE_TRACK_DIR": str(base),
        "ETUDE_OUT": str(root / "out"),
        "ETUDE_RESUME": "1" if resume else "0",
    }
    if "ETUDE_HOME" in os.environ:
        values["ETUDE_HOME"] = os.environ["ETUDE_HOME"]
    if inputs_directory is not None:
        values["ETUDE_INPUTS_DIR"] = str(inputs_directory)
    if seed is not None:  # a historical run may have declared none
        values["ETUDE_SEED"] = str(seed)
    if commit.returncode == 0:
        values["ETUDE_COMMIT"] = commit.stdout.strip()
    if until is not None:
        values.update(ETUDE_UNTIL_CLOCK=until.clock, ETUDE_UNTIL_VALUE=str(until.value))
    return _Training(
        recipe=recipe,
        settings=settings,
        values=values,
        address=run_address(identity, label),
        experiment=label,
        seconds=seconds,
    )


@dataclass(frozen=True)
class _Finished:
    """A finished run with no later target: skipped in a selection, refused alone."""

    address: str
    experiment: str
    position: str  # every clock's furthest value, as `updates=6`


@dataclass(frozen=True)
class _Training:
    recipe: Callable[..., object]
    settings: BaseModel
    values: dict[str, str]
    address: str
    experiment: str
    seconds: float | None


def _train_each(attempts: list[_Training | _Finished], *, json_output: bool) -> int:
    """Train the checked runs in order, ending at the first saved stop."""
    for attempt in attempts:
        if isinstance(attempt, _Training):
            if _train(attempt, json_output=json_output):
                return 75
            continue
        finished = f"finished at {attempt.position}"
        if len(attempts) == 1:
            raise EtudeError(
                "run_already_finished", detail=f"{finished}; give --until with a later target"
            )
        print(f"{attempt.address} {finished}", file=sys.stderr if json_output else sys.stdout)
        if json_output:
            _print_outcome(attempt.address, attempt.experiment, stopped=False)
    return 0


def _train(attempt: _Training, *, json_output: bool) -> bool:
    """Call the recipe as one attempt; true when it stopped after saving."""
    values = dict(attempt.values)
    if attempt.seconds is not None:
        values["ETUDE_DEADLINE"] = datetime.fromtimestamp(
            time.time() + attempt.seconds, UTC
        ).isoformat()
    stopped = False
    with _attempt(values):
        if not json_output:
            print(attempt.address, flush=True)
        try:
            with redirect_stdout(sys.stderr) if json_output else nullcontext():
                attempt.recipe(attempt.settings)
        except Stopped:
            stopped = True
    if json_output:
        _print_outcome(attempt.address, attempt.experiment, stopped=stopped)
    return stopped


def _place_inputs(inputs: tuple[Input, ...], directory: Path, artifacts: Path) -> None:
    """Verify each distinct input once per pass and link the run's names to it."""
    directory.mkdir(parents=True)
    for given in inputs:
        uri = given.uri
        if uri is None:
            if given.run is None:
                raise EtudeError(
                    "input_not_located",
                    detail=f"{given.name}: bucket key {given.key!r}; give uri or run for local training",
                )
            uri = DirectoryStorage(default_root()).uri(f"{given.run}/{given.key}")
        content = Content(sha256=given.sha256, size_bytes=given.size_bytes)
        path = artifacts / content.name
        try:
            if not path.exists():
                place_artifact(
                    Artifact(location=uri, sha256=given.sha256, size_bytes=given.size_bytes),
                    path,
                )
                path.chmod(0o444)
        except FileNotFoundError:
            raise EtudeError(
                "input_undelivered", 404, detail=f"{given.name}: {uri}: file is missing"
            ) from None
        except (ContentMismatch, Corrupt):
            raise EtudeError(
                "input_undelivered", 409, detail=f"{given.name}: {uri}: bytes differ"
            ) from None
        link = directory / given.name  # a checkpoint folder's members are FOLDER/MEMBER
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(path)


def _print_outcome(address: str, experiment: str, *, stopped: bool) -> None:
    print(
        json.dumps(
            {
                "run": address,
                "experiment": experiment,
                "outcome": "stopped" if stopped else "finished",
            }
        )
    )


def _resumed(
    base: Path,
    root: Path,
    prior: RunRecord,
    label: str,
    run: str,
    *,
    config: str,
    seed: int | None,
    until: Until | None,
) -> tuple[int | None, Until | None] | _Finished:
    """The seed and target a resumed attempt uses, or where the run already finished.

    An omitted seed or target is the stored one. A finished run resumes only
    towards a target beyond everything it has logged, saved or published.
    """
    if prior.experiment != label:
        raise EtudeError("run_name_belongs_to_another_experiment")
    if seed is None:
        seed = prior.seed
    validate_resume(prior, config=config, seed=seed)
    data = read(DirectoryStorage(base), label, run)
    if until is None and data.writers and data.writers[-1].until is not None:
        until = Until.model_validate(data.writers[-1].until)
    if until is not None and until.clock not in {clock.name for clock in prior.clocks}:
        raise ValueError(f"undeclared clock: {until.clock!r}")
    if data.outcome is not None and data.outcome.state == "finished":
        positions = [
            data.reached,
            *(item.at for item in saved_states(root / "out")),
            *(item.at for item in data.checkpoints),
        ]
        reached = {
            clock.name: max(at.get(clock.name, 0) for at in positions) for clock in prior.clocks
        }
        if until is None or until.value <= reached[until.clock]:
            return _Finished(
                address=run_address(run, label),
                experiment=label,
                position=", ".join(f"{clock}={value:g}" for clock, value in reached.items()),
            )
    return seed, until


@contextmanager
def _attempt(values: dict[str, str]) -> Iterator[None]:
    # Clear inherited launch settings: a local call is its own attempt.
    names = set(values) | {key for key in os.environ if key.startswith("ETUDE_")}
    prior = {key: os.environ[key] for key in names if key in os.environ}
    signals = (signal.SIGINT, signal.SIGTERM)
    handlers = {sig: signal.getsignal(sig) for sig in signals}
    with TemporaryDirectory(prefix="etude-stop-") as folder:
        stop = Path(folder) / "stop"

        def request_stop(signum: int, frame: FrameType | None) -> None:
            if signum == signal.SIGINT and stop.exists():
                raise KeyboardInterrupt()
            stop.touch()

        try:
            for key in prior:
                os.environ.pop(key)
            os.environ.update(values, ETUDE_STOP_FILE=str(stop))
            for sig in signals:
                signal.signal(sig, request_stop)
            yield
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            for key in list(os.environ):
                if key in names or key.startswith("ETUDE_"):
                    os.environ.pop(key)
            os.environ.update(prior)
