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
from etude.identity import run_address
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
    name: str | None = None,
    experiment: str | None = None,
    resume: str | None = None,
    until: Until | None = None,
    seconds: float | None = None,
    json_output: bool = False,
) -> int:
    """Call the file's `train(config)` once; return 75 after a saved stop, 0 after success.

    Exceptions from the recipe propagate. Config, seed and finished-run
    refusals occur before calling it, so they cannot change a stored outcome.
    """
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("--seconds must be finite and positive")
    if resume is not None and name is not None:
        raise ValueError("--name and --resume cannot be combined")
    if file.suffix != ".py":
        raise ValueError("a Python file defining train(config) is required")
    file = file.resolve()
    sys.path.insert(0, str(file.parent))
    try:
        found = runpy.run_path(str(file)).get("train")
        if not callable(found):
            raise ValueError(f"{file.name}: define train(config)")
        recipe = cast(Callable[[BaseModel], object], found)
        label = experiment or file.stem
        base = default_root()
        stored_config: str | None = None
        if resume is not None:
            if experiment is None:  # EXPERIMENT/RUN names its own experiment
                label = resume.rpartition("/")[0] or label
            identity = run_address(resume, label).removeprefix(f"{label}/")
            if config is None:  # the run's own config, like its seed and target
                stored = RunFiles(run_directory(base, label, identity)[1]).run_file()
                if stored is None:
                    raise EtudeError("run_not_found", 404, detail=run_address(resume, label))
                stored_config = stored.config_json
        kind = config_type(recipe)
        settings = (
            kind.model_validate_json(stored_config)
            if stored_config is not None
            else changed_config(kind, config if config is not None else "{}")
        )
        # Validate the actual JSON that another machine would receive.
        text = settings.model_dump_json(round_trip=True)
        settings = kind.model_validate_json(text)
        if resume is not None:
            seed, until = _resumed(base, label, identity, config=text, seed=seed, until=until)
        else:
            seed = 0 if seed is None else seed
            identity = f"{name or file.stem}-seed-{seed}"
        root = run_directory(base, label, identity)[1]
        if resume is None and (root / "run.json").exists():
            raise EtudeError(
                "run_name_taken", detail=f"{identity}; use --resume {run_address(identity, label)}"
            )
        commit = subprocess.run(
            ["git", "-C", str(file.parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        values = {
            "ETUDE_EXPERIMENT": label,
            "ETUDE_RUN": identity,
            "ETUDE_JOB_NAME": name or file.stem,
            "ETUDE_TRACK_DIR": str(base),
            "ETUDE_OUT": str(root / "out"),
            "ETUDE_RESUME": "1" if resume is not None else "0",
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
            values["ETUDE_DEADLINE"] = datetime.fromtimestamp(
                time.time() + seconds, UTC
            ).isoformat()
        result = 0
        with _attempt(values):
            if not json_output:
                print(identity, flush=True)
            try:
                with redirect_stdout(sys.stderr) if json_output else nullcontext():
                    recipe(settings)
            except Stopped:
                result = 75
        if json_output:
            print(
                json.dumps(
                    {
                        "run": identity,
                        "experiment": label,
                        "outcome": "stopped" if result else "finished",
                    }
                )
            )
        return result
    finally:
        sys.path.remove(str(file.parent))


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
