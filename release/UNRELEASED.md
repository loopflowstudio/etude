# Unreleased

## One experiment-file model

An experiment file declares its work in three functions with fixed names, and
each declaration returns every job that should have been run, or can be run,
by now:

```python
from etude.jobs import ExperimentState
from etude.jobs import EvalJob, TrainingJob


def training_jobs() -> list[TrainingJob]:  # may take state: ExperimentState
    return [
        TrainingJob(name="base", train=train, config=TrainConfig(), seeds=(0, 1, 2), max_hours=2)
    ]


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    saved = [checkpoint for run in state.runs for checkpoint in run.checkpoints]
    return [EvalJob(play=play, tournament=ladder, checkpoints=saved, max_hours=1)]
```

`etude train FILE` and `etude eval FILE` do the work on this computer.
`etude launch FILE` and `etude queue FILE` read both declarations and get
machines for what has not been run, printing a decision for every declared
run and tournament; typing the command again launches only what is new.

Eval-only files can seat checkpoints from any experiment, in benchmark
`checkpoints=` or round-robin `enter=`. Local evals fetch and verify their
checkpoint inputs too; local eval inherits this computer's environment. Eval-only
files smoke their declared evals and report against outside checkpoints. Different
artifacts under one checkpoint key are refused, including delivered matches.
Unselected local training passes report existing runs and train new stages with
their verified inputs and environment. Typed jobs select uv extras with
`extras=("eval",)`; remove command rewrites and `UV_NO_SYNC=1`.
A failed launched eval holds only its undelivered
matches: the pass hands down unaffected work and prints all decisions before
returning `launched_pass_failed`. `--quote` reports the hold without failing.

Launching has three levels, each built on the one below: the **controller**
makes that pass over an experiment file, the **scheduler** queues runs and
starts each on a machine with room, and the **provisioner** rents and
terminates machines. Limits sit on a job, on a pool or on-demand, and on the
experiment; cohorts, which sat between, are removed.

### Upgrade

| Before | Now |
| --- | --- |
| `etude.declarations.read_job_file` | `etude.declarations.read_training_jobs` |
| `def runs(...)` | `def training_jobs(...)` |
| `def eval(state)` | `def eval_jobs(state)` |
| `training(train, config=c, ...)` | `TrainingJob(name="...", train=train, config=c, ...)`; `name` is required |
| `evaluation(benchmark, play=play, ...)` | `EvalJob(play=play, tournament=benchmark, ...)` |
| `evaluation(arena, config=c, play=play, ...)` | `EvalJob(play=play, tournament=arena(c), ...)` |
| a standalone `evaluation(...)` returned beside training jobs | an `EvalJob` returned from `eval_jobs(state)` |
| `Job(command=...)` returned from the file | `launch` or `queue` from `etude.launch.client`, or a typed job |
| `etude eval FILE --pool P`, `--rent R`, `--queue` | `etude launch FILE --pool P --rent R`, `etude queue FILE` |
| `etude eval FILE --rent R --smoke` | `etude launch FILE --rent R --smoke` |
| `--cohort NAME`, `--parallel N` | removed; room is the pool's machines, the on-demand's `--max` and `--slots` |
| `launch_cohort(name, runs, ...)`, `queue_cohort(name, runs, ...)` | `launch(runs, ...)`, `queue(runs, ...)`; both return a tuple of run ids |
| `etude status COHORT`, `wait`, `cancel`, `logs --cohort` | `--experiment E`, `--job J`, or run addresses |
| `etude continue --run COHORT --cohort NEXT` | `etude continue --run RUN`, one run at a time |
| `ETUDE_COHORT`, `track(cohort=...)` | removed |
| `etude controller`, `controller set`, `unset`, `run` | `etude server`, `server set`, `unset`, `run` |

- **Old names are refused, not aliased.** Every command that reads the file
  stops at an old declaration whose replacement is absent, before anything
  runs or is stored: `job.py: rename runs() to training_jobs()`,
  `job.py: rename eval() to eval_jobs()`. `from etude.jobs import training`
  is an `ImportError`. Update Python calls such as `capacity.runs(hidden=512)`
  too. If both an old and a new declaration exist, only the new one is used.
- **Each declaration returns its own kind.** `training_jobs` returns
  `TrainingJob`s and `eval_jobs` returns `EvalJob`s; anything else is refused
  naming the declaration and its type. A command job is launched from Python.
- **Jobs are named where they are written.** An unnamed `TrainingJob` is
  refused; the file's stem no longer names a sole job. An `EvalJob` is named
  by its tournament.
- **Addresses are always `EXPERIMENT/JOB@SEED`.** A sole job or seed is no
  longer left out, so adding one never renames a run. Stored runs keep their
  addresses and need no conversion: a run stored as `ppo` or `ppo@1` is
  recognised by the job name and seed it recorded, so name the job as it was
  named (`name="ppo"` for a job the file's stem named) and it is not trained
  again. A plain `train(config)` file trained with `etude train` is still
  addressed `EXPERIMENT`.
- **`etude eval` only plays here.** Renting and queueing evals moved to
  `etude launch` and `etude queue`, which take `--source`, `--watch` and
  `--retry-failed`. Without `--source` they read state where the target's
  machines deliver.
- **`training_jobs` may take `state`**, and `ExperimentState.eval_runs` lists
  the experiment's eval runs beside `runs`. A parameter named `state` is no
  longer a launch flag.
- **Cohorts are gone.** A launch places what its pool and on-demand have
  room for and queues the rest, which start by themselves as room appears;
  one launch may rent up to the on-demand's `--max` machines. Select several
  runs with `--experiment` and `--job`.
- **`--max-usd D` bounds the experiment**, on `etude launch FILE` and
  `etude queue FILE`: the cost of its ended runs plus the `max_usd` of those
  queued and running, plus a new run's, must fit, or the run is not queued
  and its line says so. The limit is not stored; each pass applies the one it
  is given.
- **The SSH controller is the server.** `etude server set HOST`; the setting
  file is `~/.etude/server`, and a selection is set again after upgrading.
  "Controller" now means the pass over an experiment file.
- **Launcher upgrades are explicit.** Install the SDK, inspect with `etude database
  status`, then run `etude database upgrade`. Ordinary reads do not migrate or
  select copied queues. All compatible clients share `launcher.sqlite`. Workers
  and guardians keep running during upgrades. Cohort evidence remains stored.
  Unmerged SQL requires an empty
  development fixture. See [Upgrading Etude](../docs/launcher.md#upgrading-etude).
- **`~/.etude/experiments/launched.json` is gone.** In-flight eval matches
  are read from the launcher's run records; delete the file.

Unchanged: `Job.runs(...)`, `ExperimentState.runs`, the `etude runs` command,
`report()`, `play(match)`, typed flags on `training_jobs`, a job's command and
identity, and everything already stored.

## Eval jobs share one run, and Etude runs their agents

```python
# game.py: imports no model library
def play(match: Match[Deal], agents: Sequence[Agent]) -> Tables:
    ...
    [action] = agents[seat].act([row])  # the row carries its own seed


# models.py: may import Torch
def load(checkpoint: CheckpointRef) -> Agent: ...  # anything with act(rows)


# capacity.py
def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    ...
    net = Agents(load=load)
    return [
        EvalJob(play=play, tournament=ladder, checkpoints=rated, agents=net, max_hours=1),
        EvalJob(play=play, tournament=gauntlet, checkpoints=large, agents=net, max_hours=1),
    ]
```

```text
$ etude launch capacity.py --rent l4
...
eval ladder: 48 new, 0 running, 0 failed, running capacity/evals-1a2b3c4d5e6f
eval gauntlet: 24 new, 0 running, 0 failed, running capacity/evals-1a2b3c4d5e6f
```

A pass hands down the eval jobs that can run together as one run on one
machine, instead of one run per tournament, and their tournaments play at
once.

An eval job may declare `agents=Agents(load=..., build=...)`. You write the
game, `play(match, agents)`, and how a checkpoint is loaded, `load(checkpoint)`, which returns anything with `act(rows)`. Etude runs the rest: light
game processes that import only `play`'s module, one agent process that loads
each checkpoint once, and batches of rows per checkpoint (32 rows or 0.005 seconds
by default), drawn from every game and tournament in the run. `act` must
answer each row as if it were alone. An eval job without `agents=` calls
`play(match)` as before, in the same game processes
([Agents](../docs/evaluation.md#agents)).

`etude launch` and `etude queue` no longer rent for every few new matches.
The eval run is handed down when a machine that already exists has room, when
estimated new work reaches `--min-eval-hours H` (default: 2 hours), or when
no training run of the experiment is queued or running. Otherwise each tournament's line says how
many matches wait and why, and a later pass finds them again
([Experiments](../docs/evaluation.md#experiments)).

### Upgrade

- **Remove `workers=`** from `EvalJob(...)` and `evaluating(...)`; it is
  refused. Etude plays one game per CPU the run may use, fewer when agents
  or memory need the room. The deprecated `evaluate(workers=)` accepts and
  ignores it until 0.5. Eval jobs already queued with `workers` still run.
- **Existing results remain readable and are not replayed.** Results stay keyed
  by tournament and match; eval runs already stored, queued or running are
  read as before. Declaring, changing or removing `agents` replays nothing.
- **A launched eval run is `EXPERIMENT/evals-DIGEST`**, not
  `EXPERIMENT/TOURNAMENT-DIGEST`, and every tournament it plays prints that
  one name. Each tournament's tables are in its own eval run below that
  folder; `statsdb` and reports find them without change.
- **Evals may now wait while training runs.** A command that used to rent
  for evals at once prints `waiting: ...` instead when no machine has room.
  With no recorded match durations the pass rents immediately.
  Pass `--min-eval-hours 0` to admit any estimated work, or use
  `--watch` so the pass after training ends launches them.
- **A run's limits are its jobs' summed.** The eval run's `max_hours` and
  `max_usd` are the totals of the eval jobs it plays, and `--max-usd` admits
  it as one run.
- **A failed match stops its tournament.** Matches already in flight retain
  their outcomes, so successful matches are not replayed and failed ones keep
  their retry flags. The other tournaments keep playing, and the run ends
  failed. A loader failure instead fails every
  undelivered scheduled match containing that checkpoint across the group's
  tournaments, including matches never dispatched, with the load error.
  Matches without that checkpoint continue; delivered results stay unchanged.
  A load raising `RetryRequested` applies the same scope with `retry=True`.
- **Some failed matches are replayed without `--retry-failed`.** A match the
  machine failed rather than the game (unfinished at the run's time limit or
  SIGTERM, a failed delivery, an agent process that exited, or code that
  raised `etude.errors.RetryRequested`) is failed with `retry=True`, and the
  next pass plays it again. An ordinary error from `play`, `load` or `act`
  is `retry=False` and waits for `--retry-failed`, as before.
- **`etude.jobs.with_matches` is removed.** The pass builds the run itself.

Match records now retain execution seconds for later machine-time estimates.
Completed matches are sealed and offered to storage individually. Container
CPU quotas set how many games play and their thread budgets, and
`ETUDE_SCRATCH_DIR` supplies temporary fast scratch. A run with agents keeps
which rows shared each batch in `artifacts/agents/batches.json`.
On CPUs, a small model plays faster without `agents=`: with a frozen Manabot
Small checkpoint on 32 CPUs, one agent process reached at most about half the
rate of a process per game holding the model. Agents on a GPU are not yet
measured. In a separate frozen 64-game comparison, the default agentless Etude
path reached 0.8744 times raw 28-process pool throughput (91.459 versus 79.971
seconds including startup), with identical trajectories. CPU parity remains
unmet and requires review.

`matches.retry` distinguishes retryable infrastructure failures from ordinary
game errors without adding another status. Older match records default it to
false. The derived statsdb cache rebuilds from schema 14 or 15 to 16 automatically.
Game processes load play's defining module; for a job with agents, heavy
imports there produce one warning per run naming their importer.

Checkpoint seats now play directly: `Held()` replaces a bare inference seat;
relative seats take no `inference=`. Author records wrap seats to describe
search or other composition. `compose(record)` preserves the wrapper type and
fields in identity, replacing every leaf with its resolved checkpoint key, and
names the player by them, such as `game:Shift(over=small, amount=1)`.
Qualified type names distinguish wrappers from different modules. Resuming a
run accepts relocated checkpoints throughout player trees and relative
pairings, while still checking their fingerprints and authored settings.
`Agents(load=load, build=build)` calls `load(checkpoint)` in the agent process
and `build(player, leaves)` in each game. The hook receives the author's tree
and shared remote checkpoint agents; nested and bare players reuse those
networks and their batches. `Agents.config` and `Inference` are removed.
Released inference-configured results retain their keys and stay delivered.
## Policies by name

A policy, what one run of a job trained, has a name, and a seat can name one
in any experiment:

```python
from etude.checkpoints import PolicyKey

PolicyKey(experiment="tla-frontier", job="small", seed=0, role="policy")
PolicyKey(experiment="tla-frontier", job="small")  # the same policy, by the defaults

Final(experiment="tla-frontier", job="small")  # every seed of the job
Final(policy=PolicyKey(experiment="tla-frontier", job="small"))  # one
```

Every part of a key has a default: this file's experiment, the experiment's
only training job, the job's lowest seed, the run's only role. Relative seats
(`Final`, `At`, `Earlier`, `Aligned`, `Fraction`) take `experiment=`, `seed=`
and `role=` beside `job=`, or `policy=` a key; with no seed a seat still fills
from every seed. `state.policy(key).checkpoints` is `Checkpoints` of records,
with the same `every`, `at`, `tagged` and `last` selectors as a run.
`EvalJob(checkpoints=...)` takes records; held records without an experiment
are refused. Reports resolve other experiments' seats from the same source;
smoke names those seats as not smoked. A run publishes a second policy with
`run.checkpoint(..., role="exploiter")`.
See [Policies](../docs/evaluation.md#policies).

A checkpoint's key now includes its experiment:
`EXPERIMENT/RUN/checkpoints/NAME` (nondefault roles add `/roles/ROLE`
after the run). Two experiments' `base@0` are different
checkpoints and are seated together, and the same players meeting under the
same config are one match in every experiment that seats them.

Use `key.checkpoint(name=n, artifact=saved)` to name a checkpoint from a
`PolicyKey`. References take the key's fields flat, with the same defaults;
`policy=` and `key=` are no longer constructor arguments. Outside references
may supply `line=`; policy references derive it. Released records stay readable.

### Upgrade

| Before | Now |
| --- | --- |
| `CheckpointRef(key=checkpoint_id(...), artifact=saved)` | `CheckpointRef(experiment="ppo", job="base", seed=1, name=n, artifact=saved)`; `.policy` owns identity and `.key` is derived |
| `CheckpointRef(key=checkpoint_id(address=url), artifact=saved)` | `CheckpointRef(address=url, artifact=saved)` |
| `Final(job="small", inference=direct)` beside `direct` | `Final(job="small")`; wrap the seat in an author record to compare different play |
| `EvalJob(..., experiment="other")` | removed and refused; seat the other experiment's runs with `experiment=` or `policy=` on a seat, or with checkpoints read by `observe` |
| `evaluating(..., lines=observe(source, e).lines)` | the same, plus the lines of every other experiment a seat names |

- **Stored results keep counting.** Eval runs that recorded keys without an
  experiment are read as their own experiment's runs: the same points, and
  the next pass plays nothing again. Nothing stored is rewritten.
- **A kept `statsdb` database rebuilds once** from storage on its first
  read.
- **Queries that match on the key text** (`players.player`, `seats.player`
  or a checkpoint key compared with a literal) see the longer spelling for
  every checkpoint, old or new.
- **A hand-built key for a run of another experiment**, seated before this
  change, is read as the evaluating experiment's run. Seat it again by its
  full key if that matters.
