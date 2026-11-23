# Tracking and reports

This guide is for authors recording metrics, reading results and choosing a
report; [training](training.md) owns learning state and checkpoints.

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
uv run etude report . --serve
```

Each report lists the source again. For a large S3 or R2 source, Etude
remembers key boundaries in its local cache to divide later listings into
parallel requests across reports, watch/serve refreshes and `statsdb` reads.
Added, replaced and deleted records show up on the next build.

The report joins training progress with eval results. `Score` names the table
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
