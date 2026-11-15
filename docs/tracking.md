# Tracking and reports

This guide is for experiment authors recording metrics and reading training and evaluation results from their own storage.

```python
from etude.train.tracking import track

with track(experiment="pilot", run="baseline", config={"width": 64}) as run:
    run.log({"loss": 0.25}, updates=1, learner_transitions=128)
```

Inside a function started by Etude, use `track(config=config)` instead:
[training](training.md) owns run identity and continuation. Metric names belong
in the mapping; clocks belong in keywords. Sparse metrics are allowed; each
row must supply every declared clock. Missing values stay missing, never zero.

## A report for your experiment

```python
from etude.report import Curve, Matches, Report, Score


def report() -> Report:
    return Report(
        score=Score(table="scores", value="score", reference=0.5),
        evals=(Curve(title="Against the anchors", matches=Matches(tag="anchor")),),
        other_metrics=True,
    )
```

Put `report()` in the experiment file. The score names a table and numeric
column returned by [evaluation](evaluation.md). Tags select seatings; start
with `Curve()` for all of them. Without `report()`, the default page shows
logged metrics.

```sh
uv run etude report prototype.py --open
uv run etude report prototype.py --source s3://bucket/runs --out reports
uv run etude report experiments/ --serve
```

A normal report is a standalone HTML snapshot; rerun to update it. `--serve`
provides a Refresh button; it does not poll unless you opt into `--watch SECONDS`.
Reports read results and never start training or rent compute.
`Matrix`, `Ranking` and `Anchors` from `etude.report` add comparisons;
[report options](dev/tracking.md#reports) gives the complete field reference.

## Storage and querying

Local runs default to `~/.etude/track`, or `$ETUDE_HOME/track`.
A report takes a local directory, `s3://bucket/prefix` or `r2://bucket/prefix`
as `--source`. Install `etude-sdk[s3]` for buckets; S3 uses normal AWS
configuration, R2 uses `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`.

```sh
uv run etude sync --directory DIR --to s3://bucket/runs
uv run etude checkpoints --source s3://bucket/runs
```

`track(storages=[storage_from_uri(uri)])` delivers directly; import
`storage_from_uri` from `etude.storage`. Outages leave local data to sync later.
Keep that disk until delivery succeeds.

```python
from etude.eval.query import statsdb

db = statsdb("prototype", "s3://bucket/runs")
print(db.sql("select seat, avg(score) from scores group by seat").fetchall())
db.close()
```

Eval tables add run/match coordinates; join `seats` and `players` for labels.
See the [query reference](dev/evaluation.md#querying) for table names and joins.
Storage layouts, clocks, replacement rules and transfer measurements live in
[the tracking reference](dev/tracking.md).
