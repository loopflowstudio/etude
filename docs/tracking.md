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

Leave the view running in one terminal.

```sh
uv run etude view examples/prototype.py
```

In a second terminal, run `uv run etude train examples/prototype.py`. Open the
link it prints, `http://127.0.0.1:8765/runs/prototype/coin@0`, to see the
coin's training curve, config and checkpoint.

`etude view [PATH] [--source SOURCE] [--host HOST] [--port PORT]` serves a
read-only view. `PATH` is a Python file or folder containing the experiment's
`report()` declaration. Omit it to use the default report. `--source` chooses
where to read run files from. It defaults to this computer's runs directory
and accepts a directory, `s3://bucket/prefix` or `r2://bucket/prefix`.
Bucket sources need read credentials. Viewing requires no machine-provider key.
The view listens on `127.0.0.1:8765` by default and prints its address.

While the view advertises its address in `ETUDE_HOME/view`, `train`, `launch`,
`queue` and `status RUN` print a `view:` link for each run. A declared run
uses `EXPERIMENT/JOB@SEED`, such as `prototype/coin@0`. For a file defining
only `train(config)`, the address is the experiment name. `train --json`
prints the link on stderr.

| Path | What you see |
| --- | --- |
| `/` | Experiments grouped into Open and Completed, with run counts by state, latest write and links to open runs |
| `/status` | The experiment list used to refresh `/` |
| `/experiments/NAME` | Jobs and runs with their state, clock, last write and launcher placement or queue reason, plus the experiment report |
| `/experiments/NAME/status` | The run status table used to refresh the experiment page |
| `/experiments/NAME/report` | The experiment's report, with a **Refresh report** button |
| `/runs/ADDRESS` | One run's state, clock progress, curves, console, config and checkpoints |
| `/machines` | Held machines with their pool or on-demand, state, window and cost. Each occupied slot links to its run and shows its state and clock. |
| `/machines/status` | The inventory used to refresh the Machines page |

**Experiments.** The experiment page opens with its run status table. The
report loads in a separate frame so you can read status during a report build.
Status refreshes keep your place in the report. An experiment stays open
while it has unfinished launcher work or an active file-only run. After
10 minutes without a write, a file-only run with no recorded outcome reads
**no end recorded** and stops keeping the experiment open.

**Runs.** A training run draws its `report()` `perf` and `ml` panels, followed by the
remaining metrics in first-logged order. Checkpoints show the clock positions
at which they were saved. For a launched run, the page also shows placement,
hardware, cost, attempts and source revision when available. `Last write`
dates the run files, `last loop` dates the scheduler heartbeat and
`launcher observed` dates the view's last successful launcher read.

Opening a launched run requests the same retained console you can read with
`etude logs --run RUN`. The first visit can show **Waiting for retained console**
until the next launcher poll. The console displays gaps in the retained output
and dates the latest collection. Plain `etude train` writes its console to the
terminal.

**Refresh.** Visible run and experiment pages refresh every 5 seconds while
work is open. A launched run page also waits for its final console. The local
machine inventory refreshes every 5 seconds while visible, even when idle.
The view indexes run files in the background under `ETUDE_HOME/views`. Reading
a run graph uses the last published index, including during a bucket scan.
The files in your directory or bucket are the portable record.

Without a launcher database the view reads runs from files, and Machines says
“No launcher is used here.” With `etude server set`, the view polls the selected
server for run status and requested consoles at most once every 30 seconds.
Use `--source` to select the matching run files. The Machines page names the
selected server and directs you to `etude machines`.
