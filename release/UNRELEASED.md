# Unreleased

These notes help existing Etude users upgrade their experiments to the next release.

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

Launching has four ownership boundaries: the **controller**
makes that pass over an experiment file, the **scheduler** queues runs and
starts each on a machine with room, the **provisioner** rents and terminates
machines, and the **worker** executes delivered packets independently of
placement. Limits sit on a job, on a pool or on-demand, and on the experiment;
cohorts, which sat between, are removed.

### Upgrade

| Before | Now |
| --- | --- |
| `etude.declarations.read_job_file` | `etude.controller.declarations.read_training_jobs` |
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
`report()`, typed flags on `training_jobs`, a job's command and
identity, and everything already stored.

## Eval jobs share one run, and Etude runs their agents

```python
# game.py: imports no model library
async def play(match: Match[Deal], agents: Sequence[Forward]) -> Tables:
    ...
    [logits] = await agents[seat]([observation])
    action = choose(logits, random)  # game-side randomness seeded from match.seed


# models.py: may import Torch
def load(
    checkpoint: CheckpointRef,
) -> ModelForward: ...  # deterministic list of inputs -> list of outputs


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
game as `async def play(match, agents)` or as a streaming `play_many(matches)`
async generator. `load(checkpoint)` returns a deterministic model forward.
Etude shares checkpoint inference on declared, detected NVIDIA or Apple Silicon
hardware (native arm64 Python). Direct game processes cache their own forwards.
The forward returns one output per input, in order, independently of batch
composition. Your game owns sampling, seeded from `match.seed`; player-record
fields describe greedy/sample choice and temperature. The build hook returns
any author object. Game code awaits the checkpoint forwards.
Code and API players can declare `Agents(build=build)` without a loader; a
code-only group starts no agent process
([Agents](../docs/evaluation.md#agents)).

`etude launch` and `etude queue` submit compatible pending eval matches as one
run within `--max-usd`. Existing capacity is reused; a rental target permits
renting when no machine has room, regardless of training activity or historical
match duration. Without a rental target, the run waits in the pool's queue
([Experiments](../docs/evaluation.md)).

### Upgrade

- **Make game functions async and await checkpoint forwards.** Define
  `async def play(match, agents)` or write an async generator
  `play_many(matches: MatchStream[Deal])` to step and refill vectorized slots.
  Import `MatchStream` from `etude.eval.game`. Remove `max_delay_seconds` from
  `Agents`. Ready requests flush immediately.
- **Remove `workers=`** from `EvalJob(...)` and `evaluating(...)`; it is
  refused. Etude sizes processes from CPU, memory and pending work. For
  `play` with checkpoint inference, Etude divides `max_batch` by the
  process limit, rounding up. Shared inference leaves a core for the agent
  process when possible. For `play_many`, use `matches.target_slots` to size
  the game pool and request matches as slots free up.
- **Guard Python scripts that play.** `evaluating(...)` without `workers=`
  played in its own process; its games now play in fresh processes, which
  import the script again. Put `run.play` under `if __name__ == "__main__":`.
- **Existing results remain readable and are not replayed.** Results stay keyed
  by tournament and match. Queued records still decode. To run a queued
  declaration using synchronous `play`, update its game function and submit
  the async declaration.
- **A launched eval run is `EXPERIMENT/evals-DIGEST`**, not
  `EXPERIMENT/TOURNAMENT-DIGEST`, and every tournament it plays prints that
  one name. Each tournament's tables are in its own eval run below that
  folder; `statsdb` and reports find them without change.
- **Small eval backlogs can rent immediately.** `--min-eval-hours` is
  removed from `launch` and `queue`. Pending matches no longer wait for an
  estimated amount of work or for training to finish. Existing capacity,
  `--max-usd`, rental limits and retry rules still apply; `queue` continues
  to record the request and return without renting itself.
- **A run's limits are its jobs' summed.** The eval run's `max_hours` and
  `max_usd` are the totals of the eval jobs it plays, and `--max-usd` admits
  it as one run.
- **Ordinary game errors fail one match.** In `play_many`, yield the match
  with its error to release and refill the slot. Invalid result tables stop
  new grants for their tournament. A failed checkpoint load applies to every
  undelivered scheduled match seating that checkpoint across the group's
  tournaments, including matches never dispatched. A load raising
  `RetryRequested` marks those matches with `retry=True`.
- **Some failed matches are replayed without `--retry-failed`.** A match the
  machine failed rather than the game (unfinished at the run's time limit or
  SIGTERM, a failed delivery, an agent process that exited, or code that
  raised `etude.errors.RetryRequested`) is failed with `retry=True`, and the
  next pass plays it again. An ordinary error from `play`, `load` or the forward
  is `retry=False` and waits for `--retry-failed`, as before.
- **`etude.jobs.with_matches` is removed.** The pass builds the run itself.

Match records retain execution seconds for reports. Completed matches are
sealed and offered to storage individually. Game processes write
`artifacts/agents/game-{pid}.json` with imports, matches and transport counts.
Shared inference records batch rows, request counts and stage timings in
`artifacts/agents/batches.json`. The loader selects its device, and
`ETUDE_SCRATCH_DIR` supplies temporary fast scratch.
The CPU placement choice follows a frozen 64-game Small-checkpoint comparison
on one 32-vCPU host. Direct Etude loading took 46.36 s (4,970 games/hour),
shared loading with 2/4/7 agent processes took 89.00/72.70/66.67 s, and the raw
pool took 57.07 s. All five arms matched all 10,044 decisions and trajectories.
Mean inference costs were 66.59 ms/decision direct versus 79.30 for the best
shared arm. This is one shuffled panel on one host; it does not establish a
universal CPU model-size crossover. GPU placement retains the earlier fair
Large-checkpoint results below.

On an Apple Silicon Mac (native arm64 Python), `etude eval` can batch checkpoint
inference through one shared agent process, with up to `cpus - 1` game processes
and merged batches up to `max_batch` rows. Before the move to async play and
immediate flushing, 128 held-out Manabot `w160l8` games on a 16-core M4 Max
reached 6,992 games/hour with 15 concurrent games and shared MPS (7.43 mean
rows/forward), 6,470 games/hour with 32 concurrent games and shared MPS,
and 6,183 games/hour with direct CPU inference. The single-thread shared
CPU comparison reached 1,222 games/hour. In a separate Apple Silicon check
using `examples/agents.py` with one match per process, immediate flushing
processed 24 rows in 20 batches, compared with two batches before the change.
Scores matched.

`Agents(load=load, devices=("cuda", "metal"))` is the default for checkpoint
loaders. Use `("cuda",)` or `("metal",)` to share only on the corresponding
hardware, or `("cpu",)` for direct inference on all hosts. Undeclared or
unavailable accelerators use direct inference. Startup output names the mode,
its reason, the game count and the thread budget. Older queued loader records
without a device declaration use the default.

`matches.retry` distinguishes retryable infrastructure failures from ordinary
game errors without adding another status. Standalone and grouped evals retain
unfinished matches for retry when the shared agent process fails. Older match
records default `retry` to false. The derived statsdb cache rebuilds from schema
14 or 15 to 16 automatically.
Game processes load play's defining module; for a job using shared inference, heavy
imports there produce one warning per run naming their importer.

Checkpoint seats now play directly: `CheckpointSlot()` replaces a bare inference seat;
relative seats take no `inference=`. Author records wrap seats to describe
search or other composition. `compose(record)` preserves the wrapper type and
fields in identity, replacing every leaf with its resolved checkpoint key, and
names the player by them, such as `game:Sampled(over=small, temperature=0.5)`.
The record declares the player and its identity; the built object is what `play` calls.
Qualified type names distinguish wrappers from different modules. Resuming a
run accepts relocated checkpoints throughout player trees and relative
pairings, while still checking their fingerprints and authored settings.
`Agents(load=load, build=build)` calls `load(checkpoint)` where Etude places inference
and `build(player, leaves)` in each game. The hook receives the author's tree
and checkpoint forwards; nested and bare players reuse those
networks and their batches where shared. `Agents.config` and `Inference` are removed.
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
See [Policies](../docs/evaluation.md#players-and-composition).

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


## Package levels and supported Python imports

```python
from etude.jobs import Agents, EvalJob, ExperimentState, TrainingJob
from etude.eval.result import Tables
from etude.launch.client import launch, queue
```

Experiment declarations keep their defining module, `etude.jobs`. Supported
modules mark their supported names with `__all__`; names outside those tuples
are implementation details. The [author guides](../docs/getting-started.md) show defining-module imports. Import `Tables` from
`etude.eval.result` rather than through `etude.eval.match` or `etude.eval.run`.

Scripts using moved implementation modules must update their imports directly:

| Previous module | Module to use |
| --- | --- |
| `etude.controller` (`advance`) | `etude.controller.advance` |
| `etude.declarations` | `etude.controller.declarations` |
| `etude.smoke` | `etude.controller.smoke` |
| `etude.local` | `etude.controller.local` |
| `etude.launch.scheduler` (`Scheduler`) | `etude.launch.scheduler.loop` |
| `etude.launch.images` | `etude.launch.provisioner.images` |
| `etude.launch.machines` | `etude.launch.provisioner.machines` |
| `etude.launch.providers.*` | `etude.launch.provisioner.providers.*` |

There are no forwarding aliases at replaced paths. If an import fails after
upgrading, update that source import, including module names in your own scripts
or test fixtures. `Scheduler`, executor and controller helpers remain internal;
image preparation functions remain supported in `etude.launch.provisioner.images`. See
[launcher ownership](../docs/dev/launcher.md#ownership-and-failure-behavior) for the
four boundaries and shared modules.

This reorganization changes Python source paths, not CLI spellings, run
identities, stored records or database schemas. It requires no data conversion.
`etude.call` and the job commands retain their entry paths.

## Indexed evaluation matches

Use `Seating(config=..., base_seed=42, games=100)` to generate one match per
seed index. Seeds depend only on the base and index, shared across players and
seat orders. Increasing the count preserves completed match keys. Existing
multi-game/config-panel records remain readable under their original keys; use
a fresh tournament name when changing those panels to the indexed format.

`CheckpointSlot()` replaces `Held()` for generator placeholders. Use
`checkpoint_player(ref)` to seat a fixed checkpoint. Composed players retain
whole-player result identity; checkpoint leaves are loading details.

The [first experiment](../docs/getting-started.md) now runs training, eval and
reporting from one complete file. Author guides cover usage and limits;
implementation and persistence references live under `docs/dev/`.

## Removed transitional Python APIs

The adapters previously marked for 0.5 removal are deleted now. Import training
from `etude.train.tracking`; `etude.train.run` and `local_launch` are gone.
Use `evaluating(...)` and `run.play(...)` instead of `evaluate` or `hold`.
Import `Tables`, `Result` and `EvalResult` from `etude.eval.result`.
Use `CheckpointPlayer`, `CheckpointRef` and `RunRequest` from their defining
modules, `TrainingRunSummary` from `etude.jobs`, `RunData.writers` and
`RunData.outcome`, `checkpoint_player(checkpoint.ref)`, and `run.checkpoints()` for this run's published models.
Continuation code restores registered state or reads its assigned start input;
checkpoints() does not claim the parent's models as newly published by the child.
Count clocks are declared by use (or explicit `Clock` records); `Step` is gone.
Use config/configs fields directly instead of conditions properties.
Readers for released record formats remain, including conditions, legacy player
keys, old worker journals and already queued evaluations.

`track` now records only an Etude-assigned run inside train(config). Run identity,
seed, source, continuation and paths come from `etude train` or the launcher;
standalone `track(experiment=..., run=...)` is removed.

Eval declarations stay as Python objects through match selection. Only the
selected matches, tournament provenance and verified artifact inputs enter the
worker payload. Grouping combines those payloads rather than resolving another
checkpoint/entrant selection on the worker. Previously queued payloads normalize
at their reader boundary. Remote administration and continuation run the same
CLI over SSH; Python submissions and verified file transfers keep their wire
envelopes. All providers and the optional SSH scheduler remain supported.

Tracking's queue/block sizing and metric-name limit are implementation policy;
`track` no longer exposes those tuning parameters. Rental attribution always
uses retained billed coverage; its unused alternate arithmetic switch is gone.

## Live view

```sh
uv run etude view examples/prototype.py
```

Run `uv run etude train examples/prototype.py` in another terminal and open
its `view:` link. `launch`, `queue` and `status RUN` also print run links
while the view is running. The experiment table shows each run’s state, clock,
latest training metric, machine and spend. Select a row to inspect its console, curves,
config or checkpoints, or share a link to the selected tab. Open the Report
tab to load the experiment report. The Machines page shows held machines,
their windows and costs, and which runs occupy their slots.

`etude report` writes printable HTML and folder indexes. For live pages, use
`etude view --source SOURCE` to select a run directory or bucket. Existing launchers
need `etude database upgrade` for the new machine inventory indexes.
See the [live view guide](../docs/tracking.md#live-view).

## Removed commands, options and helpers

These had no caller in Etude and are outside what the guides teach. Each line
names what to use now.

| Removed | Use |
| --- | --- |
| `etude --profile FILE COMMAND`, `ETUDE_SPANS` | `uv run python -m cProfile -m etude COMMAND` |
| `etude status RUN --phases` | `etude logs --run RUN`; a prepared environment's outcome is on the attempt record |
| `etude runs --contains REV`, `--pool`, `--rent` | `etude runs --experiment E --job J` |
| `etude machines --images` | the image selection is in the machine's bootstrap log |
| `etude image prepare REF` | `etude ondemand image NAME REF`, or `resolve_image(REF)` |
| `prepare_pytorch(...)`, `PreparedRuntime` | `resolve_image("pytorch/pytorch:TAG")` and an explicit `Runtime(...)` |
| `Usage.estimate(rates)`, `Rates`, `S3_US_WEST_2`, `R2` | price `Usage.received_bytes` and `Usage.requests` yourself |
| `standings(db, ...)`, `Standing`, `Standings` | `compare(db, table=..., value=...)` |
| `evaluating(..., share=Share(...))` | removed; eval runs play a whole hold. Stored shares still read |
| `read_batches(...)`, `Batch` | `read(source, experiment, run).points(metric, clock)` |
| `launch(..., smoke=True)`, `queue(..., smoke=True)` in Python | `etude launch FILE --smoke` |
| `run.untag(checkpoint, ...)` | tags are added with `run.tag` and at `run.checkpoint(tags=...)` |
| `run.checkpoint_due(every)` | compare `run.at("running_hours")` with your own next mark |
| `rent`, `terminate`, `availability`, `diagnostics` in `etude.launch.client` | the `etude rent`, `terminate` and `availability` commands; `logs(...).diagnostics` |
| `Planned`, `PlannedPoint` report panels | removed; plot a schedule as a logged metric with `Series` |
| `etude report PATH --serve [--host] [--port]` | `etude view [PATH] [--source SOURCE] [--host HOST] [--port PORT]` |
