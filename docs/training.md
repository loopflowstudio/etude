# Training

You write a file with a `train` function in it. Etude calls that function on a
machine, and calls it again on another when the first is taken away.
Everything it records is files in a directory or bucket you own.
For a complete small recipe, see [learning a coin’s bias](../examples/coin.py).

```python
# train.py
import torch
from pydantic import BaseModel

from etude.train.records import Step
from etude.train.tracking import track


class TrainConfig(BaseModel):  # yours: any fields you like, and no seed
    lr: float = 3e-4
    updates: int = 1000


def train(config: TrainConfig) -> None:
    with track(clocks=[Step("updates", "optimizer updates")], config=config) as run:
        torch.manual_seed(run.seed)
        model, optimizer = build(config)
        run.resume(model=model, optimizer=optimizer)  # loads the last save, if there is one
        for update in run.steps("updates", to=config.updates):
            metrics = one_update(model, optimizer)
            run.log(metrics)
            run.save()
            if update % 100 == 0:
                run.checkpoint(policy=model)
        run.checkpoint(policy=model)
```

```sh
etude run train.py                         # a new run on this computer; prints its id
etude run train.py --resume train-seed-0   # the same run, from its last save
etude launch job.py --rent l4              # new runs on rented machines
```

```text
$ etude run train.py --until updates=300
train-seed-0
^C                                    # finishes the pass, saves, exits 75
$ etude run train.py --resume train-seed-0 --until updates=300
train-seed-0
$ etude report train.py --open        # one run, updates 1 to 300, three checkpoints
```

Seven lines of that loop are Etude's. There is no state class to write, no
check for a stop and no branch for resuming. None of the calls does anything
you could not write yourself. They are worth using because the rest of Etude
reads what they write: the launcher carries the saved state to the next
machine, an evaluation seats a checkpoint as a player, another run starts from
it, and a report draws the rows.

Etude is not a trainer. It never sees your model's forward pass, your
optimizer step or your environment, and it has no callbacks. `one_update` and
`build` above are your code.

## One function, one config

Etude asks three things of `train` and its config:

- **The config is a Pydantic model.** There is no Etude base class and no
  required field. Its defaults are the recipe.
- **It survives a trip through JSON**, because that is how it reaches a
  machine. Numbers, strings, lists and nested models are fine; an open file or
  a function is not. `etude run` and `etude launch` check this before anything
  starts or is rented.
- **`train` takes that one parameter**, annotated with the config's type, and
  is defined at the top level of a file your project can import.

Calling `train` is one attempt at the whole job. It ends in one of three ways:

| `train` | Means | Etude then |
| --- | --- | --- |
| returns | the job is finished | records the run `finished` |
| raises `Stopped` | it saved and left on purpose | calls it again, here or on another machine, inside the run's limits |
| raises anything else | it failed | records the run `failed` |

`run.steps` raises `Stopped` for you. A loop written as above never names it.

## Jobs, runs and seeds

A **job** is what to run: your function and its config. A **run** is a job
with a seed: the thing that is launched, named, carried on and reported. So
the seed is not a config field. The config says what is being tried; the seed
says which repetition this is. Read it from the run:

```python
with track(clocks=[Step("updates", "optimizer updates")], config=config) as run:
    torch.manual_seed(run.seed)
```

Seeds are chosen where the job is launched:

```python
# job.py
from decimal import Decimal

from etude.jobs import training
from etude.job import Job

from train import TrainConfig, train


def runs() -> Job:
    return training(
        train,
        config=TrainConfig(),
        smoke=TrainConfig(updates=1),  # how to run it tiny, for `etude smoke`
        seeds=(0, 1, 2),
        name="baseline",
        max_hours=2,
        max_usd=Decimal(4),
    )
```

One job with three seeds is three runs: `baseline-seed-0`, `baseline-seed-1`
and `baseline-seed-2`. Without `seeds`, it is one run with seed 0. On this
computer, `etude run train.py --seed 7` chooses it.

A run's id is its job's name and its seed. An unnamed job is named for its
file, so `etude run train.py` starts `train-seed-0`. Choose another with
`--name` (`etude run train.py --name wide --seed 7` starts `wide-seed-7`).
An id is unique within its experiment, and **a taken id is refused, never
resumed**: starting a run and carrying one on are different commands
([below](#running-it)).

An experiment is a label runs share. It comes from the launch, never from
`train`: `etude launch ppo.py` and `etude run ppo.py` both record experiment
`ppo`, and `--experiment NAME` on either gives another.

## The run and its clocks

```python
from etude.train.records import Step, Time

clocks = [
    Step("updates", "optimizer updates"),
    Time("training_seconds", "active collection and learning seconds"),
]
with track(clocks=clocks, config=config) as run:
    ...
```

`track` opens the run this call of `train` belongs to. `etude run` or the
launcher has already said which run that is, its seed and its experiment, so
`track` takes only what the loop knows: its clocks and its config.

A clock is an x-axis you declare once, with a line saying what it counts: a
`Step` (a count) or a `Time` (seconds). Every row and every checkpoint is
placed on every declared clock. Declare a second clock when one is not enough.
Clock values are absolute: a run carried on at update 400 logs 401 next, and a
continuation of a run that reached 3,200 starts at 3,201.

Etude keeps two more clocks on every row and checkpoint, in hours, which you
neither declare nor log: `running_hours`, how long the run has trained, and
`hours_since_start`, wall time since its first attempt began
([Etude's clocks](tracking.md#etudes-clocks)). `run.at(clock)` says where the
run stands on any clock, and `if run.checkpoint_due(1): run.checkpoint(policy=model)`
publishes a checkpoint every running hour.

`config=config` takes the model itself. Etude keeps it with the run, and
checks it against the run's first attempt each time the run is carried on
([Carrying on](#carrying-on)).

Leaving the block records how the attempt ended: `finished` when the block
ends, `stopped` when it raised `Stopped`, `failed` for any other exception.

## Counting

```python
for update in run.steps("updates", to=config.updates):
    ...
```

`run.steps(clock, to=)` yields the clock's values from where the run stands,
plus one, up to the target. On a new run that is 1, 2, 3, ...; on a run
carried on from a save at 400 it starts at 401.

- **The target is the launch's when it gave one**, else `to`. `etude run
  train.py --until updates=2000`, a job's `until` and `etude continue --run
  RUN --until updates=6400` all set it without changing the config.
- **Inside the loop the stepped clock is filled in**: `run.log(metrics)` and
  `run.checkpoint(policy=model)` are placed at the current value. Other
  declared clocks are yours to give: `run.log(metrics, training_seconds=t)`.
- **A stop ends the loop by raising.** When the attempt's time is nearly up or
  someone asks the run to stop, the pass in progress finishes, its
  `run.save()` writes, and asking for the next value raises `Stopped`. The
  loop never ends quietly short of its target, so code after it runs only when
  the target was reached. A second phase after the loop cannot start on a
  half-trained model.
- **A run already at its target yields nothing.** `train` called again on a
  finished job falls through the loop, publishes its final checkpoint again,
  which is harmless, and returns.

"Nearly up" is measured, not configured: the attempt's deadline is nearer than
the slowest pass so far. On this computer there is no deadline unless you give
one (`etude run train.py --seconds 600`).

## Carrying on

Every run can be carried on. You tell Etude which objects hold what has been
learned. It saves them, moves them to the next machine and loads them back.

```python
model, optimizer = build(config)
run.resume(model=model, optimizer=optimizer)
for update in run.steps("updates", to=config.updates):
    ...
    run.save()
```

`run.resume(**objects)` registers the objects under the names you give and, if
an earlier attempt saved, loads that save into them. On a new run it loads
nothing. Anything with `state_dict()` and `load_state_dict()` works: PyTorch
modules, optimizers and schedulers as they are. An object Etude cannot save is
refused at this call.

`run.save()` writes the registered objects. Call it every pass: Etude writes
when a save is due, and always on the last pass before a stop. A save is due
five minutes after the last one; `run.steps("updates", to=n, every=25)` makes
it every 25 steps instead.

Each save replaces the previous one whole or not at all. A save is a new
folder holding every registered object, complete in itself. Once it is
written, one small file is rewritten to name it, and `run.resume` loads the
save that file names. A machine lost while saving leaves the earlier save,
all of it, so the model and optimizer the next attempt loads always come from
one save.

Under a launch the bucket follows the same order: a save's files are
uploaded, then the file that names it. Each upload is a whole save, including
files whose bytes did not change, so choose an interval you would pay to
upload. Only the newest save is sent. With `every=` set shorter than an
upload takes, the saves in between stay on the machine and cost nothing to
store.

Etude removes replaced saves itself, on the machine and in the bucket, soon
after a newer one is in place. You may see an older folder for a short while;
nothing reads it ([What is on disk](#what-is-on-disk)).

Rules you can rely on:

- **Carrying on restores what was registered, and nothing else.** Weights and
  optimizer state come back exactly. Random streams, replay data and
  [games in flight](#games-in-flight) come back only if they are in an object
  you registered; otherwise each attempt starts them fresh. Rows logged after
  the last save are replaced by the next attempt's rows from where it resumes.
- **The config and seed must match.** A run carried on with a different config
  or seed is refused before `train` does any work, naming the fields that
  differ:

  ```text
  train-seed-0 was started with another config: lr was 0.0003, is 0.001.
  Start a new run, or change it on purpose with `etude continue --run train-seed-0 --config ...`.
  ```

- **Keeping your code compatible between attempts is yours.** Etude does not
  check that the model's shape, your file formats or your environment are the
  same. Source may change between attempts, which is what you want after
  fixing a bug; each attempt records its commit. Changing what a save means
  between attempts is undefined.

### Numbers of your own

Counts that are not a clock ride along as a Pydantic model:

```python
class Counts(BaseModel):
    games: int = 0
    transitions: int = 0


counts = run.resume(model=model, optimizer=optimizer, extra=Counts())
for update in run.steps("updates", to=config.updates):
    counts.games += play(model)
    run.save()
```

`run.resume` returns the `extra` you passed, with the saved values loaded into
it when there is a save. Etude saves that object as it stands, so change its
fields in place.

### Games in flight

A loop that plays games across passes can keep the unfinished ones, so an
attempt goes on with the games the last one was playing. Hold them in an
object that says how it is saved, and register it like a model:

```python
class Pending:
    """Games started and not yet finished."""

    def __init__(self) -> None:
        self.games: list[Game] = []

    def state_dict(self) -> dict[str, list[dict[str, object]]]:
        return {"games": [game.to_dict() for game in self.games]}

    def load_state_dict(self, state: dict[str, list[dict[str, object]]]) -> None:
        self.games = [Game.from_dict(saved) for saved in state["games"]]


pending = Pending()
run.resume(model=model, optimizer=optimizer, pending=pending)
for update in run.steps("updates", to=config.updates):
    finished = advance(model, pending.games)  # plays on; returns the games that ended
    run.log(learn(model, optimizer, finished))
    run.save()
```

`pending` is saved with the weights it was played with and comes back with
them. Leave it out and each attempt starts its games fresh, which costs the
games in flight at each stop and nothing else.

### State of another shape

If your state is not objects with `state_dict()`, write it yourself:

```python
found = run.resume()  # the last saved folder, or None on a new run
buffer = load_buffer(found / "replay.npz") if found else new_buffer()
for update in run.steps("updates", to=config.updates):
    ...
    run.save(lambda folder: save_buffer(buffer, folder / "replay.npz"))
```

`run.save(fn)` hands `fn` the new save's empty folder when a save is due and
keeps what it writes there, by the same whole-or-nothing rule: several files
written by one `fn` are restored together or not at all.

### Phases in one run

Two loops in one run keep their state apart with a name:

```python
run.resume(policy=policy, optimizer=policy_optimizer, name="policy")
for update in run.steps("updates", to=config.updates):
    ...
    run.save(name="policy")

run.resume(sampler=sampler, optimizer=sampler_optimizer, name="sampler")
for fit in run.steps("sampler_fits", to=config.fits):
    ...
    run.save(name="sampler")
```

A stop during the first loop raises `Stopped`, so the second never starts on a
policy short of its target. Called again, the first loop is already at its
target and yields nothing, and the second goes on from its own save.

## Rows

```python
run.log({"loss": 0.42, "entropy": 1.3})
run.log_table("games", rows)
```

A row is a dictionary of numbers. A table is many rows that belong to one
point in training, such as the outcome of every game since the last update.
Inside `run.steps` the stepped clock is filled in. Outside it, or for a second
clock, give the clocks yourself: `run.log(row, updates=400,
training_seconds=912.5)`. Metrics for several models are ordinary keys in one
row: `{"player/loss": ..., "drafter/loss": ...}`.

`log` cannot hurt training: it is a put on a bounded queue with no storage or
network I/O. [Tracking](tracking.md) has the rules for rows, tables, storage
and reading them back.

## Checkpoints

```python
run.checkpoint(policy=model)
```

A checkpoint is what someone else will use: an evaluation seats it as a
[player](evaluation.md), another run starts from it, a report places it on the
run's curve. It is recorded with where every clock stood, and it never
changes.

State and checkpoints are different things. State is for the run itself, is
replaced at each save, and holds optimizers and counters. A checkpoint is for
others, is kept, and holds what plays.

- **Named objects are written for you**, the same way state is: one file per
  name in the framework's own format, in a folder named for where the first
  clock stood (`updates-00000100/`), with a `checkpoint.json` naming them.
- **One call is one checkpoint**, however many objects it holds.
- **It is placed where the run stands**: the stepped clock's current value,
  and the latest value given for each other clock. After the loop, that is the
  target.
- **Publishing the same thing twice is harmless.** The same files at the same
  position return the checkpoint already held, on any attempt. Different files
  at a position already published are a second checkpoint beside the first
  (`updates-00000100.ordinal-2/`); nothing published is replaced.
- **It is in your storage before its record is**, so a reader that sees a
  checkpoint can read it.

### Your own file format

A project whose checkpoint has its own format records the file it wrote:

```python
path = write_policy(model, run.out / "policies" / f"update-{update}.pt")
run.checkpoint(path)
```

A file inside `run.out` is recorded where it is, without a second copy; write
it once and do not change it. A file anywhere else is copied into the run. For
a folder of your own making, `run.checkpoint(lambda folder: export(models,
folder))` hands you an empty folder to fill.

### Several models

Many loops train more than one model: a policy and a value model, or a
drafter, a deck builder and a player. Register all of them, and publish them
as one checkpoint:

```python
def train(config: LimitedConfig) -> None:
    with track(clocks=[Step("iterations", "draft, build, play rounds")], config=config) as run:
        models, optimizers = build(config, seed=run.seed)  # dictionaries by name
        run.resume(**models, **{f"{name}_optimizer": o for name, o in optimizers.items()})
        for iteration in run.steps("iterations", to=config.iterations):
            run.log(limited_iteration(models, optimizers))  # "player/loss", "drafter/loss", ...
            run.save()
            if iteration % 500 == 0:
                run.checkpoint(**models)
        run.checkpoint(**models)
```

```text
out/checkpoints/iterations-00000500/
  checkpoint.json     {"at": {"iterations": 500}, "files": {"drafter": "drafter.pt", "player": "player.pt", ...}}
  drafter.pt
  player.pt
  ...
```

The folder is one checkpoint and seats as one player. Which of its files a
game needs is up to the code that plays: one seating can load only
`player.pt`, another all of them ([Evaluation](evaluation.md)).

If your models do not step together, declare a clock for each count you want
as an x-axis and give the others when you log: `run.log(metrics,
player_updates=n)`. `run.steps` drives one clock, the one that decides when
the run is done. Loops nested inside it are plain Python.

### Earlier checkpoints

Self-play often trains against its own past. The run lists what it has
published, oldest first, across every attempt:

```python
past = run.checkpoints()
if past:
    rival = rng.choice(past)
    opponent.load_state_dict(torch.load(rival.path / "policy.pt"))
    run.log_table("trained_against", [{"role": "opponent", "updates": rival.at["updates"]}])
```

Each has where its clocks stood (`at`) and its folder or file on this machine
(`path`). These are the same records an evaluation and a report read, not a
second index. Writing down who a run trained against is a table like any
other.

## Starting from another run

A later stage that starts from an earlier one is a second job, given the
earlier run's checkpoint by name:

```python
# job.py
from etude.experiment import observe

imitation = observe("s3://my-bucket/runs", "imitate").run("imitate-seed-0").final


def runs() -> Job:
    return training(
        self_play,
        config=SelfPlayConfig(),
        start=imitation,
        seeds=(0, 1, 2),
        max_hours=6,
        max_usd=Decimal(20),
    )
```

```python
# train.py
def self_play(config: SelfPlayConfig) -> None:
    with track(clocks=[Step("updates", "optimizer updates")], config=config) as run:
        model, optimizer = build(config)
        model.load_state_dict(torch.load(run.input("start") / "policy.pt"))
        run.resume(model=model, optimizer=optimizer)  # a later attempt's save wins
        ...
```

`run.input(name)` is the path of a file or checkpoint folder the run was
given. The launcher delivers the exact bytes, checked by digest, to whatever
machine the run lands on, before `train` is called. The run records the
checkpoint it started from, so a report or an evaluation can say which run a
checkpoint descends from. A name the run was not given raises. Other files a
job is given (`inputs=`, [the launcher](launcher.md#progress-and-continuation))
are read the same way.

Split work into two runs when the stages want different machines or budgets,
when several runs start from one, or when the earlier result is evaluated on
its own. Keep [phases in one run](#phases-in-one-run) when they are cheap,
always run together and nobody uses the middle.

## What is on disk

This layout is a contract. Everything in it reads with `json` and your own
framework, without Etude installed.

```text
EXPERIMENT/RUN/
  run.json                 job, seed, config, clocks, parent run and inputs
  outcome.json             how the latest attempt ended
  metrics/  artifacts/     rows and tables (Tracking)
  out/                     run.out: follows the run to every machine
    state/
      current.json         {"save": "save-00000007"}: the save to load, written last
      save-00000007/       one save, complete in itself; never changes once written
        state.json         {"at": {"updates": 400}, "wall_ms": 1794600000000, "seed": 0, "commit": "8773d51",
                            "extra": {"games": 12800}, "files": {"model": "model.pt", "optimizer": "optimizer.pt"}}
        model.pt           torch.save(model.state_dict())
        optimizer.pt
    checkpoints/
      updates-00000100/    never changes
        checkpoint.json    {"at": {"updates": 100}, "files": {"policy": "policy.pt"}}, written last
        policy.pt
```

```python
import json
from pathlib import Path

import torch

folder = Path("~/.etude/track/train/train-seed-0/out/checkpoints/updates-00000100").expanduser()
files = json.loads((folder / "checkpoint.json").read_text())["files"]
weights = torch.load(folder / files["policy"])
```

The saved state is read the same way, through its pointer:

```python
state = Path("~/.etude/track/train/train-seed-0/out/state").expanduser()
save = state / json.loads((state / "current.json").read_text())["save"]
files = json.loads((save / "state.json").read_text())["files"]
weights = torch.load(save / files["model"])
```

Always go through `current.json`. A save folder it does not name is one
being written or one waiting to be removed, and may be incomplete. Each entry
of `files` names a file in its own save's folder; no save reads another's.
Named state is in `out/state/NAME/`, with its own `current.json`.

On this computer runs are under `~/.etude/track` (moved by `ETUDE_HOME`);
under a launch the same folder is in your bucket. Using Etude to write a loop
does not tie its results to Etude.

## Running it

Starting a run, carrying one on and branching from one are different
commands. Which one you mean is never guessed from a
directory that happens to exist.

| You want | Command | What it is |
| --- | --- | --- |
| a new run, here | `etude run train.py` | always a new run; prints its id |
| a new run, on machines | `etude launch job.py --rent l4` | always new runs |
| the same run, after a lost machine or `Stopped` | none: the launcher calls `train` again | another attempt of the same run |
| the same run, after it ended unfinished | `etude run train.py --resume RUN`, `etude launch job.py --resume RUN` | another attempt of the same run, on your current code |
| the same run, further | `etude run train.py --resume RUN --until updates=6400`, and the same on `etude launch` | another attempt of the same run, to a later target; a finished run too |
| a changed config, or a branch | `etude continue --run RUN --config ...` | a new run that starts from a copy of the first one's state and records it as its parent |

```sh
etude run train.py --config '{"lr": 0.001}' --seed 7 --name wide --until updates=2000
etude run train.py --seconds 600        # stop after ten minutes: saved, exit 75
etude run train.py --config '{"lr": 0.001}' --resume wide-seed-7
etude run train.py --json               # {"run": "train-seed-0", "experiment": "train", "state": "finished"}
etude smoke job.py                      # the smoke config, here, spending nothing
```

- **`etude run FILE`** calls the file's `train` on this computer with its
  config's defaults, changed by `--config`, a JSON object checked against the
  config type. It needs no provider key and no network. It exits 0 when the
  run finished, 75 when it stopped with its state saved, and otherwise
  nonzero. With `--json` the last line of output is one object whose `state`
  is `finished` or `stopped`, and anything `train` prints goes to standard
  error; a failed run prints no object.
- **Ctrl-C asks the run to stop**, as `--seconds` and a `kill` (SIGTERM) do:
  the pass in progress finishes, `run.save()` writes, and the command exits
  75. A stop is noticed by `run.steps` or `run.stopping()`, so a loop that
  asks neither runs on to its end.
- **`--resume RUN`** is for a run that ended without finishing: it failed and
  you fixed the bug, it ran out of hours, you cancelled it, or you pressed
  Ctrl-C. It takes the file because it runs your current code. It keeps the
  run's id, seed and target; `--name` does not apply. The config is worked
  out as for a new run, the file's defaults changed by `--config`, and must
  equal the run's: repeat the `--config` the run was started with, and expect
  a refusal naming the field if a default in the file has changed since.
- **`--resume RUN --until updates=6400`** takes the same run further. This
  also works on a finished run, which becomes unfinished again until it
  reaches the new target. Its rows, checkpoints and id go on as one run.
- **What `--resume` refuses**: a config or seed that no longer matches, and a
  finished run given no later target, which has nothing left to do. A refused
  or interrupted attempt changes nothing already recorded: a finished run
  stays finished.
- **A taken id is refused** before `train` is called, and under a launch
  before anything is rented. Choose another `--name` or `--seed`, or mean
  `--resume`.
- **`etude continue`** changes the config on purpose, or goes further while
  leaving the first run as it is. It makes a new run
  ([the launcher](launcher.md#progress-and-continuation)).

A run's hours, dollars and attempts span every attempt, whoever asked for it
([The run promise](launcher.md#the-run-promise)).

## The pieces underneath

`run.steps` and `run.save` are made of four smaller pieces. A loop that does
not count on one clock uses them directly:

```python
from etude.train.tracking import Stopped, track


def train(config: SearchConfig) -> None:
    with track(clocks=[Time("training_seconds", "active seconds")], config=config) as run:
        saved = run.out / "frontier.json"
        frontier = load(saved) if saved.exists() else new()
        budget = run.until("training_seconds", default=config.seconds)
        while frontier.seconds < budget and not run.stopping():
            frontier = expand(frontier)
            run.log({"open": len(frontier)}, training_seconds=frontier.seconds)
            save(frontier, saved)  # write beside it, then rename over it
        if frontier.seconds < budget:
            raise Stopped
```

| Piece | Is |
| --- | --- |
| `run.out` | a directory that follows the run: the same on every attempt, carried to storage as it is written and restored before each attempt on another machine |
| `run.until(clock, default=)` | the absolute target: the launch's for that clock, else the default |
| `run.stopping()` | true once a stop was asked for, or once the deadline is nearer than the slowest pass so far; ask once per pass |
| `Stopped` | raised after saving, short of the target: the attempt is recorded `stopped` and the run is called again |

Returning normally short of the target would read as finished. Files you
write in `run.out` yourself are carried one at a time, so write each
atomically and keep what one resume needs in one file: a machine lost between
two of them leaves one new and one old. `run.save(fn)` is the way to keep
several files together. A name ending `.partial` is never sent, and anything
under a folder whose name starts with `.` stays on the machine.

## Calls

| Call | Does |
| --- | --- |
| `track(clocks=, config=)` | opens the run for this call of `train` |
| `run.seed` | this run's seed |
| `run.resume(**objects, extra=, name=)` | registers what to keep and loads the last save into it |
| `run.steps(clock, to=, every=)` | counts from where the run stands to the target; raises `Stopped` on the pass after a stop is asked for |
| `run.save()`, `run.save(fn)` | writes the registered objects when due, and always before a stop |
| `run.log(row, **clocks)`, `run.log_table(name, rows, **clocks)` | rows on the declared clocks |
| `run.checkpoint(**objects)`, `run.checkpoint(path)`, `run.checkpoint(fn)` | publishes a checkpoint that never changes, with its clocks |
| `run.checkpoints()` | this run's published checkpoints, oldest first |
| `run.at(clock)`, `run.checkpoint_due(every, clock="running_hours")` | where the run stands on a clock; whether the clock passed a multiple of `every` since the latest checkpoint |
| `run.input(name)` | a file or checkpoint folder this run was given |
| `run.out`, `run.until()`, `run.stopping()`, `Stopped` | [the pieces underneath](#the-pieces-underneath) |

## Upgrading a loop

| Before | Now |
| --- | --- |
| `seed` in the config, `track(seed=config.seed)` | `training(..., seeds=(...))` or `etude run --seed`; read `run.seed` |
| `track(config=config.model_dump())` | `track(config=config)` |
| `with local_launch(out=DIR, experiment=NAME): train(config)` | `etude run train.py` |
| a state file in `run.out`, `while ... and not run.stopping()`, `raise Stopped` | `run.resume(...)`, `run.steps(...)`, `run.save()` |
| `run.checkpoint(path, updates=n)` inside the loop | `run.checkpoint(policy=model)`, or `run.checkpoint(path)` |
| `run.latest_checkpoint()` | `run.checkpoints()[-1]` |
| `Path(os.environ["ETUDE_INPUTS_DIR"]) / NAME` | `run.input(NAME)` |
| a re-used run name resumes | a taken id is refused; `--resume RUN` resumes |

`track(seed=)`, `local_launch` and `run.latest_checkpoint()` still work, warn
when used, and are removed in 0.5. Runs already stored
stay readable where they are, including their flat `RUN/` folders and a
`state.json` that records an outcome; nothing in a bucket is rewritten. A
state file an older loop kept in `run.out` is not read by `run.resume`: finish
or continue such a run on the version that wrote it.
