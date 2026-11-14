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
checkpoint inputs too. A failed launched eval holds only its undelivered
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
- **The launcher database is a new version.** An older file is refused and
  left untouched; let its runs finish under the version that made it.
  Stored runs, including a `run.json` that names a cohort, still read.
- **`~/.etude/experiments/launched.json` is gone.** In-flight eval matches
  are read from the launcher's run records; delete the file.

Unchanged: `Job.runs(...)`, `ExperimentState.runs`, the `etude runs` command,
`report()`, `play(match)`, typed flags on `training_jobs`, a job's command and
identity, and everything already stored.

## Eval jobs share one run and one inference server

```python
from etude.jobs import EvalJob, ExperimentState, InferenceServer

net = InferenceServer(name="net", serve=serve, config=ServeConfig(max_batch=64))


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    ...
    return [
        EvalJob(
            play=play, tournament=ladder, checkpoints=rated, server=net, workers=8, max_hours=1
        ),
        EvalJob(
            play=play, tournament=gauntlet, checkpoints=large, server=net, workers=8, max_hours=1
        ),
    ]


def play(match: Match[Deal], inference: Endpoint) -> Tables: ...
```

```text
$ etude launch capacity.py --rent l4
...
eval ladder: 48 new, 0 running, 0 failed, running capacity/evals-1a2b3c4d5e6f
eval gauntlet: 24 new, 0 running, 0 failed, running capacity/evals-1a2b3c4d5e6f
```

A pass hands down compatible eval jobs sharing a server (or no server) as
one run on one machine, instead of one run per tournament. An eval job may name an
`InferenceServer`: a name, a `serve(config)` context manager and its config.
Etude enters `serve` once before the run's first game, gives what it yields
to every `play(match, inference)`, and leaves it after the last game, on
failure and on SIGTERM. Batching, devices and model loading belong to `serve`
and its config and to nothing else. A job names zero or one server; one
server can host several models. Jobs naming different servers form separate
groups. Tournaments in a group play concurrently so requests can batch
together. An eval job that names no server calls `play(match)` as before
([Inference servers](../docs/evaluation.md#inference-servers)).

`etude launch` and `etude queue` no longer rent for every few new matches.
The eval run is handed down when a machine that already exists has room, when
estimated new work reaches `--min-eval-hours H` (default: 2 hours), or when
no training run of the experiment is queued or running. Otherwise each tournament's line says how
many matches wait and why, and a later pass finds them again
([Experiments](../docs/evaluation.md#experiments)).

### Upgrade

- **Existing results remain readable and are not replayed.** Results stay keyed
  by tournament and match; eval runs already stored, queued or running are
  read as before.
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
- **A failed match stops its tournament.** The other tournaments keep
  playing, and the run ends failed.
- **`etude.jobs.with_matches` is removed.** The pass builds the run itself.

The shared worker pool uses the largest `workers` declared by its members,
so adding another eval job does not multiply serving concurrency. Match
records now retain execution seconds for later machine-time estimates.
`etude.errors.RetryRequested` means an infrastructure refusal, rather than a
player failure: the run fails and unfinished matches remain new on the next
pass. Serving adapters use it for infrastructure failures. Completed matches are
sealed and offered to storage individually. Container CPU quotas set thread
budgets, `ETUDE_SCRATCH_DIR` supplies temporary fast scratch, and
`ETUDE_RECEIPTS_DIR` retains server receipts with the run.

`matches.retry` distinguishes retryable infrastructure failures from ordinary
game errors without adding another status. Older match records default it to
false. The derived statsdb cache rebuilds from schema 14 to 15 automatically.
Game processes load play's defining module and retain startup import counts;
heavy imports produce one warning per group naming their importer.
