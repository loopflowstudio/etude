# Tracking and reports

This guide is for authors recording metrics, reading results, watching live runs
and choosing a report; [training](training.md) owns learning state and
checkpoints.

```python
from etude.train.tracking import track

with track(config=config) as run:
    run.log({"loss": 0.42, "entropy": 1.3}, updates=1, learner_transitions=128)
    run.log_table("episodes", [{"reward": 4.0}, {"reward": 7.0}])
```

Call track inside `train(config)`. Etude supplies experiment, job, seed and
output paths. A process without an Etude assignment is refused; standalone
tracking is not a supported entry point.

## Rows and clocks

Metrics go in the mapping; clock values are keyword arguments. `run.steps`
declares and fills its clock. New log keywords declare other count clocks.
Use `Time` from `etude.train.records` only for a clock measured in seconds.
Keep optimizer updates, learner transitions and training steps distinct.

Every row gives every used clock. Metrics can be sparse: absence is missing,
not zero or the previous value. `strict=True` turns validation warnings into
errors. Logging queues rows without network I/O; a slow writer can lose rows
when its bounded queue fills, reported by the run's incomplete counter.

`log_table` attaches a JSON row table to the current training point. It is for
collections such as episode outcomes; eval results belong to the separate
match table path in [evaluation](evaluation.md).

## Storage

Local results live below `~/.etude/track`; `ETUDE_HOME` relocates Etude's home.
Add destinations with
`storages=[storage_from_uri("s3://bucket/runs")]` (from `etude.storage`).
Install `etude-sdk[s3]` for S3 or R2. S3 uses normal AWS configuration; R2 uses
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`. Inject secrets
from your secret store; never put them in a declaration or checkpoint.

Storage writes files, not an Etude-hosted database. A launched run's courier
carries those files to its bucket. Readers see only delivered results. Retain
local files when delivery fails, then retry:

```sh
uv run etude sync --directory /data/retained-runs --to s3://bucket/runs
uv run etude checkpoints --source s3://bucket/runs
```

Sync is repeatable and delivers retained files; it cannot recover a lost disk.
`run.finish(timeout=10)` allows final delivery ten seconds; an active network
request can outlive that wait. See [limits](limits.md) and the developer
[storage reference](dev/tracking.md#storage-outages-and-recovery).

## Reading

```python
from etude.storage import default_root
from etude.train.read import read_all

runs = read_all(default_root(), "prototype")
for run in runs:
    print(run.name, run.reached, len(run.checkpoints))
```

`read` and `read_all` read directories or buckets directly;
there is no launcher in this path. A later attempt replaces earlier metrics
from its first resumed coordinate, while old files remain inspectable.
`etude.experiment.observe(storage, experiment)` supplies the `ExperimentState`
used in declarations. [Evaluation](evaluation.md#querying) owns match SQL.

## Reports

```python
from etude.report import Curve, Report, Score, Series


def report() -> Report:
    return Report(
        score=Score(table="scores", value="won", reference=0.5),
        evals=(
            Curve(title="Win rate"),
            Curve(
                title="Actions taken", score=Score(table="actions", value="taken", per="offered")
            ),
        ),
        ml=(Series(title="Loss", keys=("loss",)),),
    )
```

```sh
uv run etude report examples/prototype.py --open
uv run etude report examples/prototype.py --source s3://bucket/runs
uv run etude report examples --open
```

`etude report` writes a self-contained, printable HTML page you can archive or
commit to git (for a folder, one page per experiment file and an `index.html`
with their scores); `--watch SECONDS` rewrites on change. The report joins training progress with eval results. `Score` names the table
and value; add `per="opportunities"` for a sum(value)/sum(per) rate.
Each curve can pin its own score. Curves without one use the report score.
Curves draw training progress; `Matrix(rows=..., columns=...)` and `Ranking(by=...)`
use your table columns. `Matches(tournament=..., seating=...)` selects a panel's
matches; `tag=` selects tagged seatings. Omit seating to show scores for any
player chosen in the report. A composed player remains a whole player, even if
its leaves share models with another composition.

Use `perf` and `ml` to choose metric panels for your own learning system.
`Report()` defaults assume RL metric names; absent data leaves gaps, not invented
measurements. A checkpoint with no training record can still be an anchor.
See the [report declarations](../src/etude/report.py) for the supported panel
fields and [record semantics](dev/tracking.md) when developing Etude itself.

## Live view

```text
$ uv run etude view examples &
http://127.0.0.1:8765/
$ uv run etude launch examples/prototype.py
prototype: state from file:///.../.etude/track: 0 training runs, 0 eval runs
prototype/coin@0  new, running
view: http://127.0.0.1:8765/runs/prototype/coin@0
$ uv run etude status prototype/coin@0
prototype/coin@0  coin  running, updates 40, machine this computer, 1 of 3 attempts used
view: http://127.0.0.1:8765/runs/prototype/coin@0
```

`etude view [PATH]` serves live pages for runs, experiments, reports and
machines. Pass a file or folder as `PATH` so experiment reports and run-page
metric panels follow your `report()` declarations; without `PATH`, pages use the
default report. Use `--source s3://bucket/runs` (or `r2://...` or another
directory) to watch runs in a bucket; by default it reads this computer's runs
directory.

While `etude view` is running, it writes its address to `ETUDE_HOME/view` and
removes it on exit. `train`, `launch`, `queue` and `status RUN` then print a
`view:` link to each run's page (`/runs/EXPERIMENT/JOB@SEED`, or
`/runs/EXPERIMENT` for a plain `train(config)` file); when no view is running,
those commands print nothing extra.

| Path | Page |
| --- | --- |
| `/` | Experiments: open first, then completed |
| `/experiments/NAME` | One experiment: live status (jobs, runs, machines, clocks) and its `report()` charts loaded asynchronously on the same page |
| `/runs/ADDRESS` | One run, live: state, curves, console, config and checkpoints |
| `/machines` | Machines held now, their windows and costs, and the runs on each |

An experiment page (`/experiments/NAME`) combines operational status (what has
run or completed, where each active run is, and clocks) with the experiment's
`report()` charts on one page. The two components load asynchronously so a
slow report build never blocks live status.

A run page (`/runs/ADDRESS`) shows its state (`queued`, `running`, `stopped`, `finished`,
`failed`) with launcher detail when launched (such as `running · setting up on
gpu-41840a` or `failed · out_of_time`), clock progress against its target,
machine and hardware, dollars, attempts, and source ages (`last write` from run
files and `last loop` from the launcher). Its metric curves draw the
experiment's `report()` `ml` and `perf` panels for that run, then every other
logged metric in first-logged order; nothing is rearranged in the browser, so
everyone on the same link sees the same page. Below the curves are the retained
launcher console (with its age and any gaps; plain `etude train` runs note that
no console was recorded), the stored config and source commit, and saved
checkpoints.

An experiment stays open while the launcher holds an unfinished run of it, or
while a run with no launcher record and no recorded end wrote within the last
10 minutes. A run with no launcher record and no recorded end that has been
silent longer reads `no end recorded · last write ...`, leaves open, and stops
keeping pages refreshing.

Live status and run views refresh every 5 seconds while anything shown is open
and the browser tab is visible, stopping once everything shown has ended or the
tab is hidden; the report component loads asynchronously and refreshes on
demand. Your run files in your directory or bucket remain the portable record
you own; any local cache or index the view keeps is derived for sub-second
chart loads.

`etude view` binds `127.0.0.1:8765` (`--host`, `--port` override it) and has no
login. It advances nothing, rents nothing, never migrates the launcher database,
never connects to running machines, and needs no provider keys (bucket sources
still need bucket read credentials). Without a launcher database, runs are shown
from files and `/machines` says no launcher is used here. With a remote server
selected (`etude server set`), runs and experiments appear in full (asking the
server at most every 30 seconds) while `/machines` points to `etude machines`.
Machine system metrics (GPU, CPU and memory samples) are not yet recorded.
