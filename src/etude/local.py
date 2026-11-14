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
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import FrameType
from typing import cast

from pydantic import BaseModel

from etude.call import changed_config, config_type
from etude.errors import EtudeError
from etude.experiment_file import defined_functions
from etude.identity import resume_conflicts, run_address
from etude.jobs import load_job_function
from etude.launch.job_file import read_job_file, select_runs
from etude.run import RunFiles, validate_resume
from etude.spec import Until
from etude.stopping import Stopped
from etude.storage import DirectoryStorage, default_root, run_directory
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
    `runs()` use exactly the same declarations and addresses as a launch.
    """
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("--seconds must be finite and positive")
    if file.suffix != ".py":
        raise ValueError("a Python file defining train(config) or runs() is required")
    file = file.resolve()
    label = experiment or file.stem
    if "runs" in defined_functions(file):
        requests = select_runs(read_job_file(file, experiment=experiment), job=job, seed=seed)
        for request in requests:
            recipe = load_job_function(request.job, current=True)
            if request.id is None:
                raise ValueError(
                    "local training needs an addressed request; return a Job or give RunRequest.id"
                )
            result = _train(
                file,
                recipe,
                label=request.job.experiment or label,
                identity=request.id,
                job=request.job.name,
                initial=request.job.config,
                config=config,
                seed=request.seed,
                resume=resume,
                until=until or request.job.until,
                seconds=seconds,
                json_output=json_output,
            )
            if result:
                return result
        return 0
    if job not in (None, file.stem) or seed not in (None, 0):
        raise ValueError(
            "select the file's sole job and seed 0; define runs() for other jobs or seeds"
        )
    sys.path.insert(0, str(file.parent))
    try:
        found = runpy.run_path(str(file)).get("train")
        if not callable(found):
            raise ValueError(f"{file.name}: define train(config)")
        return _train(
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
            json_output=json_output,
        )
    finally:
        sys.path.remove(str(file.parent))


def _train(
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
    json_output: bool,
) -> int:
    base = default_root()
    root = run_directory(base, label, identity)[1]
    prior = RunFiles(root).run_file()
    kind = config_type(recipe)
    if resume and prior is None:
        raise EtudeError("run_not_found", 404, detail=run_address(identity, label))
    stored = prior.config_json if resume and prior is not None else initial
    settings = (
        changed_config(kind, config, stored=initial)
        if config is not None
        else kind.model_validate_json(stored)
        if stored is not None
        else changed_config(kind, "{}")
    )
    text = settings.model_dump_json(round_trip=True)
    settings = kind.model_validate_json(text)
    if resume:
        seed, until = _resumed(base, label, identity, config=text, seed=seed, until=until)
    elif prior is not None:
        differences = resume_conflicts(
            config=text,
            seed=seed,
            prior_config=prior.config_json,
            prior_seed=prior.seed,
        )
        if differences:
            raise EtudeError(
                "run_identity_conflict",
                detail="; ".join(differences) + "; choose --experiment NAME",
            )
        raise EtudeError(
            "run_name_taken",
            detail=f"{run_address(identity, label)}; use --resume; to train further give --resume --until CLOCK=TARGET",
        )
    commit = subprocess.run(
        ["git", "-C", str(file.parent), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    values = {
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
    if seed is not None:  # a historical run may have declared none
        values["ETUDE_SEED"] = str(seed)
    if commit.returncode == 0:
        values["ETUDE_COMMIT"] = commit.stdout.strip()
    if until is not None:
        values.update(ETUDE_UNTIL_CLOCK=until.clock, ETUDE_UNTIL_VALUE=str(until.value))
    if seconds is not None:
        values["ETUDE_DEADLINE"] = datetime.fromtimestamp(time.time() + seconds, UTC).isoformat()
    result = 0
    with _attempt(values):
        if not json_output:
            print(run_address(identity, label), flush=True)
        try:
            with redirect_stdout(sys.stderr) if json_output else nullcontext():
                recipe(settings)
        except Stopped:
            result = 75
    if json_output:
        print(
            json.dumps(
                {
                    "run": run_address(identity, label),
                    "experiment": label,
                    "outcome": "stopped" if result else "finished",
                }
            )
        )
    return result


def _resumed(
    base: Path, label: str, run: str, *, config: str, seed: int | None, until: Until | None
) -> tuple[int | None, Until | None]:
    """The seed and target a resumed attempt uses, or the refusal that leaves the run as it is.

    An omitted seed or target is the stored one. A finished run resumes only
    towards a target beyond everything it has logged, saved or published.
    """
    root = run_directory(base, label, run)[1]
    prior = RunFiles(root).run_file()
    if prior is None:
        raise EtudeError("run_not_found", 404, detail=run_address(run, label))
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
            position = ", ".join(f"{clock}={value:g}" for clock, value in reached.items())
            raise EtudeError(
                "run_already_finished",
                detail=f"finished at {position}; give --until with a later target",
            )
    return seed, until


@contextmanager
def _attempt(values: dict[str, str]) -> Iterator[None]:
    # Clear inherited launch settings: a local call is its own attempt.
    prior = {key: value for key, value in os.environ.items() if key.startswith("ETUDE_")}
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
                if key.startswith("ETUDE_"):
                    os.environ.pop(key)
            os.environ.update(prior)
