# Training

This guide is for authors adding saves, checkpoints and clocks to their own
learning loop; [the first experiment](getting-started.md) runs without a model library.

```python
from etude.train.tracking import track


def train(config: Config) -> None:
    with track(config=config) as run:
        model, optimizer = build(config, seed=run.seed)
        run.resume(model=model, optimizer=optimizer)
        for update in run.steps("updates", to=config.updates, save_every=25):
            run.log(one_update(model, optimizer))
            run.save()
            if update % 500 == 0:
                run.checkpoint(policy=model)
        run.checkpoint(policy=model)
```

Etude calls `train(config)` with its annotated Pydantic config. Your code owns
learning, model libraries and devices. Use Python 3.12 and install those libraries
in the job's environment. A plain file with just `train` can run directly:

```sh
uv run etude train train.py
uv run etude train train.py --resume --until updates=2000
```

## Jobs, runs and seeds

```python
from decimal import Decimal

from etude.jobs import TrainingJob


def training_jobs() -> list[TrainingJob]:
    return [
        TrainingJob(
            name="base",
            train=train,
            config=Config(),
            smoke=Config(updates=1),
            seeds=(0, 1, 2),
            max_hours=2,
            max_usd=Decimal("4"),
        )
    ]
```

Each seed makes an independent run: `experiment/base@0`, `@1`, `@2`.
The file stem names the experiment; `--experiment` overrides it. A plain
`train(config)` file uses the experiment name alone. Reusable training code
names no experiment. `run.seed` comes from the command; seed every library you
use and register random state when continuation must preserve a random stream.

`etude train FILE.py --job base --seed 0` runs here. `etude launch FILE.py`
reconciles every declaration with retained results and allocates machines; see
[the launcher](launcher.md). Config and seed must agree when resuming a run.
Changing the target with `--until` preserves absolute coordinates.

## Carrying on

`run.resume(**objects)` registers objects with `state_dict` and `load_state_dict`,
including PyTorch models, optimizers and schedulers. `run.save()` writes the
registered state when due: by default every five minutes, or at `save_every`
steps / `save_every_seconds` seconds. Call save each iteration; completion and
planned stop save too. Register everything required to continue learning.

For frameworks without `state_dict`, or custom files beside JSON counters,
register `export` and `restore` callbacks in `run.resume(...)` without PyTorch:

```python
from pathlib import Path

from pydantic import BaseModel

from etude.train.tracking import track


class Counts(BaseModel):
    games: int = 0


def train(config: Config) -> None:
    with track(config=config) as run:
        learner = build_learner(config, seed=run.seed)

        def export_weights(folder: Path) -> None:
            learner.write(folder / "weights.bin")

        def restore_weights(folder: Path) -> None:
            learner.read(folder / "weights.bin")

        counts = run.resume(
            export=export_weights,
            restore=restore_weights,
            extra=Counts(),
            name="learner",
        )
        for update in run.steps("updates", to=config.updates, save_every=25):
            counts.games += learner.step()
            run.save(name="learner")
```

Both callbacks take a `Path` and return `None`. `export(folder)` writes regular
files (including nested relative paths) into an empty staging directory;
`restore(folder)` reads from the selected committed save folder and applies them
to live state. On a fresh run, `restore` is skipped and `counts` starts as the
supplied `Counts()` instance. On a resumed run (`etude train train.py --resume`),
Etude checks the seed, manifest and exported files, restores any registered
`**objects`, populates `extra` in place, calls `restore(folder)` and returns
that same `extra` instance. Omitting `extra` returns the restored save `Path`,
or `None` on first use.

Use `name=` to isolate independent state namespaces, matched by
`run.save(name=...)`. `export`, `restore`, `extra` and `name` are reserved
keyword arguments and cannot name `**objects`. Calling `run.save(export=fn, name=...)`
updates a namespace's exporter before checking whether a save is due;
registering `restore=` without any `export=` raises `ValueError` on save so
`current.json` is never overwritten without your files. Exported files are
recorded in the save manifest and must not be symlinks, `state.json`,
dot-prefixed paths, `.open`/`.partial` files or names colliding with registered
`<object>.pt` files. If `restore` raises, registration does not complete,
though objects or `extra` restored before it may already be updated. The
[persistence reference](dev/training.md) describes the stored layout.

A save is a whole snapshot, selected by a pointer written last. A crash while
saving leaves the previous complete save. A second attempt reloads that save;
rows past its continuation coordinate are superseded. Scientific coordinates
remain steps, transitions or optimizer updates, not rental time.

Ctrl-C once requests a saved stop at the next safe boundary; a second interrupt
is immediate. Stopping before any declared clock moves writes no step-0 save;
stopping after progress saves every registered namespace whose clocks moved.
Respect `run.should_stop` in loops that do not use `run.steps`. A stopped attempt
exits with code 75 and can continue elsewhere. Never launch a new process that
deletes saves: delivery owns acknowledgment and retention.

## Checkpoints

```python
run.checkpoint(policy=model)
run.checkpoint(policy=policy, belief=belief)
run.checkpoint(path_to_your_file)
```

State is for the run itself; a checkpoint is an immutable model for evaluation
or another run. Named objects use the framework's own files in a folder with
`checkpoint.json`. One call creates one checkpoint even with several objects.
An external file is copied; a file inside `run.out` is recorded in place and
must not be changed after publication. For a custom directory export, pass a
callable that writes into the provided empty folder.

A checkpoint records every clock's current coordinate. Roles distinguish
policies from one training run: `run.checkpoint(role="drafter", policy=drafter)`.
`CheckpointRecord.ref` names the saved player independent of its byte location;
`PolicyKey(experiment=..., job=..., seed=..., role=...)` selects its lineage.

## Starting from another run

A declaration can use `TrainingJob(start=checkpoint, ...)`; it supplies the
checkpoint as the named start input. Read inputs with `run.input("name")`.
Declare other files through `Input` on the job, including their SHA256 and byte
length; an `Input.run` names the producer for relay, while `uri` locates bytes.
Do not hardcode another machine's local path.

For a new continuation from an existing launched run:

```sh
uv run etude continue --run experiment/base@0 --until updates=4000
```

A continuation copies the parent's retained out state (or a selected checkpoint)
and keeps absolute training coordinates. It is a new run with parent lineage.
Use `--ordinal` when deliberately starting from an earlier checkpoint. Resuming
an interrupted run continues its identity instead. See [limits](limits.md) for
lost unsaved work and [tracking](tracking.md) for rows, tables and reports.
