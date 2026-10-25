# Etude

Launch training runs on your own or rented machines, and track what they record.

```python
# job.py
from decimal import Decimal

from etude.jobs import training
from etude.launch.records import Job

from train import TrainConfig, train


def runs() -> Job:
    return training(train, config=TrainConfig(lr=1e-3), max_hours=2, max_usd=Decimal(4))
```

```python
# train.py
from pydantic import BaseModel

from etude.train.records import Step
from etude.train.run import track


class TrainConfig(BaseModel):
    lr: float = 3e-4
    updates: int = 1000


def train(config: TrainConfig) -> None:
    run = track(experiment="capacity-pilot", clocks=[Step("updates", "optimizer updates")])
    for update in range(1, config.updates + 1):
        loss = train_one_update(config.lr)
        run.log({"loss": loss}, updates=update)
        if update % 500 == 0:
            run.checkpoint(save_model(), updates=update)
    run.finish()
```

```sh
etude launch job.py          # on this computer; --pool P for machines you own,
                             # --rent R to rent CPU or GPU machines, or both
etude status                 # where runs stand
etude logs --run RUN         # preparation, every attempt, cleanup
etude report ppo.py --open   # experiment ppo's evals, perf and ML details, as one page
```

Etude decides the job's command: on the machine it imports `train` from your
checkout and calls it with the config, validated back into a `TrainConfig`. A
config `train` cannot take is refused before anything launches. An evaluation
job holds a tournament ([evaluation](docs/evaluation.md)) for one checkpoint
the same way, with your `play`:

```python
from etude.jobs import evaluation

evaluation(
    progress, experiment="ppo", play=play, checkpoint=weights, max_hours=0.5, max_usd=Decimal(1)
)
```

Everything a run records goes to your own directory or S3 bucket. There is no
server to run and no account to make. A run that loses its machine resumes from
its last checkpoint on another, and reads back as one run.

## Install

Etude needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install etude-sdk          # the `etude` command
uv add etude-sdk                   # the library, in a training project
uv add 'etude-sdk[s3]'             # with S3 or R2 storage
```

## Read more

- [Tracking](docs/tracking.md): runs, clocks, checkpoints, reports.
- [Evaluation](docs/evaluation.md): agents playing each other in parallel, benchmarks and round robins, queried with DuckDB.
- [The launcher](docs/launcher.md): typed jobs, pools, SSH hosts, rented
  machines, cohorts, limits and recovery.

## Develop

```sh
uv sync
uv run pytest
uv run mypy
uv run ruff check . && uv run ruff format --check .
```

[AGENTS.md](AGENTS.md) holds the conventions, for people and coding agents.

## Licence

MIT. See [LICENSE](LICENSE).
