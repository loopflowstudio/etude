# Etude

Launch training runs on your own or rented machines, and track what they record.

```python
# job.py
from etude.launch.records import Job


def runs() -> Job:
    return Job(command="uv run python train.py", max_hours=2)
```

```python
# train.py
from etude.train.records import Step
from etude.train.run import track

run = track(experiment="capacity-pilot", clocks=[Step("updates", "optimizer updates")])
for update in range(1, 1001):
    loss = train_one_update()
    run.log({"loss": loss}, updates=update)
    if update % 500 == 0:
        run.checkpoint(save_model(), updates=update)
run.finish()
```

```sh
etude launch job.py          # run it on this computer, an SSH host or a rented GPU
etude status                 # where runs stand
etude logs --run RUN          # preparation, every attempt, cleanup
etude reports write --open   # charts of every run, as one HTML file per experiment
```

Everything a run records goes to your own directory or S3 bucket. There is no
server to run and no account to make. A run that loses its machine resumes from
its last checkpoint on another, and reads back as one run.

## Install

Etude needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install etude-sdk          # the `etude` command
uv add etude-sdk                   # the library, in a training project
uv add 'etude-sdk[s3]'             # with S3 storage
```

## Read more

- [Tracking](docs/tracking.md): runs, clocks, checkpoints, reports.
- [Tournaments](docs/evaluation.md): agents playing each other in parallel, queried with DuckDB.
- [The launcher](docs/launcher.md): pools, SSH hosts, rented machines, cohorts,
  limits and recovery.

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
