# v0.2.1

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.1 has three changes:
- Evaluation can follow a run while it trains, because every recorded checkpoint is published while the run is still going.
- Storage can be Cloudflare R2 as well as S3, for the launcher and for tracking, and every storage location now names its provider.
- Each experiment's eval runs are queried from one kept DuckDB database that reads only what arrived since it was last opened.

This release breaks some callers; see **Upgrading** below.

## Every checkpoint is published while the run is still going

Tracking is now the only thing that writes checkpoints. Every checkpoint a run records with `run.checkpoint(...)` goes to storage as soon as it is recorded, so a follower can evaluate milestones before the run ends.

```sh
etude checkpoints --source s3://bucket/etude/launch   # lists each recorded checkpoint at its bucket address
etude continue --run RUN --until steps=63000 --source s3://bucket/etude/launch
```

- One set of carriage rules (`etude.carriage`) decides when a file in a run's folder is ready to send. The launcher's courier and `etude sync` both use it:
  - files ending `.open` or `.partial` wait until they are whole;
  - `.tail` files wait until the attempt ends;
  - dot-folders stay on the machine;
  - checkpoint files go before their records.
- Every file under the run is sent, not only Etude's named folders. Files an author writes there, such as evidence, are delivered with ordinary delivery and `etude sync`.
- A checkpoint is never overwritten:
  - recording the same model at the same coordinate returns the existing reference;
  - a different model at that coordinate becomes `CLOCK-VALUE.ordinal-N.EXT`.
- A retry on a fresh machine gets back the run's records and its latest checkpoint before the job starts, and checkpoint ordinals continue across attempts. Without a bucket, the controller relays them from the host that last ran the run; the retry waits while that host is unreachable.
- Continuation is now `etude.jobs.continue_from` and the top-level `etude continue`. It reads one of the parent's recorded checkpoints (the latest, or `--ordinal`). The checkpoint arrives on the new machine as an input verified by digest.
- A job's own `environment` variables are now set for it. Etude's `ETUDE_*` names take precedence over them.

## Cloudflare R2 storage

```sh
etude storage add r2 --bucket manabot --prefix launch/etude --provider r2
etude storage add west --bucket BUCKET --prefix etude/launch --provider s3 --region us-west-2
```

```python
from etude.run.storage import storage_from_uri

storage_from_uri("r2://manabot/runs")
```

- Each storage location names its `provider`, `s3` or `r2`. S3 also needs a `region`; R2 refuses one.
- Credentials come from the environment, and nothing about the account is stored:
  - the launcher reads `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_API_TOKEN`;
  - tracking reads the first three.
- A machine gets one grant per folder it writes: its session's `out/` and each of its runs.
  - On R2 a grant is temporary keys scoped to that prefix.
  - On S3 it is STS federation keys where AWS issues them, plus a POST form.
- If a grant can't be issued, the launch stops before anything is rented. A storage failure while preparing is reported as a refusal with no machine acquired.
- Cost estimates use dated rates for each provider and price reads and writes separately. R2 has no egress charge.

## Query an experiment's eval runs from one kept database

```python
from etude.eval.query import statsdb
from etude.eval.tournament import curves, hold

hold(progress, experiment="ppo", checkpoint=ref, play=play, storages=[bucket])
db = statsdb("ppo", bucket)  # or a directory, or nothing: ~/.etude/track
curves(db, "anchors", table="plays", value="score")
```

- `statsdb(experiment, store=None, *, rebuild=False)` keeps one DuckDB file per experiment and store under `ETUDE_HOME/statsdb`.
- Opening it lists the store once and reads only files it has not read before. Match records, table files, tournament and checkpoint files are written once, so nothing already held is fetched again.
- Duplicates are resolved the same way as before:
  - a done match record replaces a failed one;
  - a match's rows come from the latest file holding them.
- The store stays the authority. `rebuild=True`, or a change to the database's schema, reads everything again.
- Only one process can have the file open; another open gets `statsdb_in_use`.
- Experiments are never joined: each has its own database.
- A tournament now only decides which matches to play, and its results belong to the experiment it was held for. Benchmark results sit beside the training checkpoints they follow.
- A batch hold reuses only matches that the experiment's own eval runs delivered. This costs one `run.json` read per eval run.
- `etude reports eval` writes one page per experiment, `reports/eval/EXPERIMENT.html`, with an optional `--experiment`, like the training reports.

## Upgrading

- `etude.eval.query.connect` is removed. Use `statsdb(experiment, store)`.
- `hold(...)` and `etude.jobs.evaluation(...)` require `experiment=`. Before, an eval run's experiment was its tournament's name; pass that name to keep runs where they were.
- The eval report moves from `reports/eval/index.html` to `reports/eval/EXPERIMENT.html`.
- Job scripts must record checkpoints with tracking and resume with `run.latest_checkpoint()`:
  - `etude-result.json` now reports only clocks;
  - `ETUDE_CHECKPOINT` and `ETUDE_FROM_VALUE` are removed;
  - so are `etude.launch.client.continue_from` and `Scheduler.continue_from`.
- `etude continue` takes `--run` and `--until` flags instead of positional arguments.
- `etude storage add` requires `--provider`, and a launcher `Storage` requires `provider`. The launcher database schema is now version 10, and older databases are refused.

## Known limits

- After a non-kept rental's attempt finishes, the launcher deletes the pod as soon as it sees the finished record. The worker's final `out/ended` note and the tail of `worker.log` can be lost. Results and attempt records are published before that point.
- Files under a run that aren't metrics, tables or checkpoints are hashed again on every delivery pass. Runs with many such files pay for that after every match.
- Stored checkpoints are not yet removed by any rule.
