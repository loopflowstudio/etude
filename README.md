# Etude

Launch training runs on your own or rented machines, and track what they record.

```python
# job.py
from decimal import Decimal

from etude.jobs import TrainingJob

from train import TrainConfig, train


def training_jobs() -> list[TrainingJob]:
    return [
        TrainingJob(
            name="base",
            train=train,
            config=TrainConfig(lr=1e-3),
            smoke=TrainConfig(updates=1),  # how to run it tiny, for `etude smoke`
            seeds=(0, 1, 2),  # three runs of one job
            max_hours=2,
            max_usd=Decimal(4),
        )
    ]
```

```python
# train.py
import torch
from pydantic import BaseModel

from etude.train.tracking import track


class TrainConfig(BaseModel):
    lr: float = 3e-4
    updates: int = 1000


def train(config: TrainConfig) -> None:
    with track(config=config) as run:
        torch.manual_seed(run.seed)
        model, optimizer = build(config)
        run.resume(model=model, optimizer=optimizer)  # loads the last save, if there is one
        for update in run.steps("updates", to=config.updates):
            run.log(one_update(model, optimizer))
            run.save()
            if update % 500 == 0:
                run.checkpoint(policy=model)
        run.checkpoint(policy=model)
```

```sh
etude train train.py         # one new run on this computer, without the launcher
etude smoke job.py           # one tiny update here, spending nothing
etude database upgrade       # initialize or explicitly upgrade the launcher
etude launch job.py          # what has not been run: job/base@0, @1 and @2, on this
                             # computer; --smoke smokes first, --pool P for machines you
                             # own, --rent R to rent, or both
etude status                 # where runs stand
etude logs --run RUN         # preparation, every attempt, cleanup
etude report job.py --open   # experiment job's evals, perf and ML details, as one page
etude report . --serve       # every experiment file here, with a live index
```

The loop is yours: Etude owns no part of learning. `train` is one attempt at
the whole job, and it can be called again. A run that loses its machine, or
stops for a deadline, is called again on another with what `run.save()` kept,
and reads back as one run ([Training](docs/training.md)). `job.py` supplies
the experiment name `job`; `train` names none.

Etude decides the job's command: on the machine it imports `train` from your
checkout and calls it with the config, validated back into a `TrainConfig`. A
config `train` cannot take is refused where the job is written, before
anything launches.

The same file says what to evaluate. An eval job holds a tournament
([evaluation](docs/evaluation.md)) with your `play` for the checkpoints it
names, chosen from what the experiment's runs have stored so far:

```python
# job.py, continued
from etude.jobs import EvalJob, ExperimentState

from arena import play, progress  # play(match, agents) -> Tables; progress is a Benchmark


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    saved = [checkpoint for run in state.runs for checkpoint in run.checkpoints]
    return [
        EvalJob(
            play=play,
            tournament=progress,
            checkpoints=saved,
            max_hours=0.5,
            max_usd=Decimal(1),
        )
    ]
```

Each declaration returns every job that should have been run, or can be run,
by now. `etude launch job.py` reads both and gets machines for what has not
been run, so the same command launches training first and, typed again as
checkpoints arrive, the matches nobody has played. `etude eval job.py` plays
them on this computer instead.

Eval jobs that can run together share one machine. Pending matches are submitted
within `--max-usd`; existing capacity is reused and a rental target permits
renting when no machine has room. Training activity and historical game durations
do not delay admission. Write `load(checkpoint)` returning a deterministic
list-in/list-out forward (`EvalJob(..., agents=Agents(load=load))`). Every game
uses `play(match, agents)` and owns action selection and randomness. On visible
NVIDIA hardware, Etude batches calls across games through one agent process;
CPU games cache and call forwards directly. Author records compose players;
`build(player, leaves)` returns the objects the game uses
([agents](docs/evaluation.md#agents)).

Everything a run records goes to your own directory or S3 bucket. Saved state and checkpoints are JSON
and your framework's own files, [readable without Etude](docs/training.md#what-is-on-disk).

## Install

Etude needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install etude-sdk          # the `etude` command
uv add etude-sdk                   # the library, in a training project
uv add 'etude-sdk[s3]'             # with S3 or R2 storage
```

## Read more

- [Training](docs/training.md): the `train` function, seeds, carrying a run
  on, checkpoints, `etude train`.
- [Tracking](docs/tracking.md): rows, row tables, storage, reading runs back, reports.
- [Evaluation](docs/evaluation.md): players (checkpoints and code) playing each other in parallel, benchmarks and round robins, queried with DuckDB.
- [The launcher](docs/launcher.md): experiment files, typed jobs, pools, SSH
  hosts, rented machines, limits, recovery and
  [upgrading with runs active](docs/launcher.md#upgrading-etude).

## Develop

Read [TESTING.md](TESTING.md) before running checks. Never intentionally induce
load for testing.

```sh
uv sync
uv run pytest
uv run mypy
uv run ruff check . && uv run ruff format --check .
```

[AGENTS.md](AGENTS.md) holds the conventions, for people and coding agents.

For a small cooperative game, [train and evaluate Hanabi](docs/hanabi.md) with
the optional OpenSpiel and PyTorch dependencies.

## Licence

MIT. See [LICENSE](LICENSE).
