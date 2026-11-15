# Tracking records

This reference is for maintainers changing tracking records; authors start with [the guide](../tracking.md).

Rules a caller can rely on:

- **Clocks are declared by use.** `run.steps` declares its clock; each new
  log keyword declares another count clock, with its name as label. Optional
  `track(clocks=["updates", "learner_transitions"])` closes the set: another
  clock keyword is refused (an error under `strict=True`, else a warning and
  dropped row). Use `Time(...)` in that list only for custom seconds.
  Historical declarations keep their kinds and labels.
- **Every row carries every used clock.** A missing clock is an error under
  `strict=True`; otherwise it warns once and counts an incomplete row, charted
  only against coordinates it gave. There is no sparse-clock option.
- **Metrics may be sparse.** Put metrics in the mapping; a row can log any
  subset. A missing metric reads as missing (null), never zero or a previous
  value. Occasional evaluations belong to eval runs.
- **No ordering rule in storage.** A repeated or lower clock value is accepted
  and kept. A read returns the latest write per clock value. A clock that goes
  backwards within one process warns once, and raises under `strict=True`; a
  resume from earlier state is a new process and does neither.
- **A later attempt's rows replace earlier ones from its first row on.** The
  first row an attempt logs says where it resumed: logging `updates=1301`
  first replaces earlier attempts' rows from update 1301 on and keeps
  everything before. An attempt that logs nothing replaces nothing. The caller
  passes no resume point, whether it restored a checkpoint or its own state
  file. Attempts written before this rule keep the resume point they recorded.
  Remove `resumed_at=` from existing `track` calls; the argument is no longer
  accepted. Historical run files remain readable.
- **A run's name is taken once.** Declaring a name its experiment already
  holds is refused; nothing is inferred from a folder that exists. Another
  attempt of the same run is asked for by selection (`etude train FILE --resume`,
  or the launcher's own retry), and must give the config and seed the run
  started with.
- **Continuation is lineage.** A run may name a parent run, a clock and the
  value it continues from. The parent is unchanged. The report does not join
  the two yet. A launched continuation (`etude continue`) declares this
  itself, from where its parent stood (`ETUDE_CONTINUES`).
- **Reads are bucketed and keep extremes**: per bucket first, last, min, max,
  mean, count and a count of NaN or infinite values. A range with fewer points
  than buckets returns the points themselves.
- **A job's runs across seeds are its seed sample.** A job is the launcher's
  Job: one function called with one config, run once per seed. The report draws the mean of its seeds, banded
  by their range, and shows how many seeds each point has.
- **`log` cannot hurt training by default.** It is a put on a bounded queue:
  no storage or network I/O, no waiting for delivery. It warns once about bad
  coordinates or a full queue; only `strict=True` raises for bad coordinates.
  A full queue drops the row and counts it in
  `run.dropped`. `track` is strict and raises on a bad declaration.
- **The files are the record.** Everything about a run is in one folder named
  for its address, `EXPERIMENT/JOB@SEED/`, in the runs directory and in
  each storage it delivers to: `run.json` (its job, seed, config, clocks, parent and inputs),
  `outcome.json` (how its latest attempt ended), `metrics/` (row blocks and
  their indexes, from every attempt), `artifacts/` (immutable files,
  including logged row tables) and `out/` (`run.out`), which holds the saved
  state in `out/state/`, one folder per save with `current.json` naming the
  one to load, and each published checkpoint in `out/checkpoints/`,
  named for where its first clock stood (`updates-00001000/`); a checkpoint
  file copied from outside `out` is in `checkpoints/` with its record.
  [Training](training.md#what-is-on-disk) shows the layout. Under the launcher
  the same folder in the bucket also holds `attempts/ATTEMPT.json` and the
  pieces of the job's log it names, under `attempts/ATTEMPT.log/`.
  The allocation's `RunAttempt.writers` holds each tracking context's metadata,
  including its run and first-row cutoff. Several writers still use one attempt
  and one share of the machine's cost. `read(...).writers` gives the contexts
  that wrote a run; historical `read(...).attempts` warns until 0.5.
  A run's id is unique within its experiment. Runs stored before this
  layout, in a flat `RUN/` folder with a `state.json` outcome, read the same.
  Config, outcome, state and checkpoint records are JSON. Rows are columnar block files (the
  layout is described at the top of `src/etude/train/block.py`),
  sealed every few seconds and merged as they accumulate, so a chart reads two
  arrays. Rows logged since the last seal are in a local log and are not yet
  read; `sync --seal` seals what a dead process left.
- **Your own files travel with the run.** Any other file in the run's folder
  is delivered too. A file that may change is hashed on every delivery pass
  and sent again when it does. A file under `artifacts/`, at any depth, is
  sent once and never read again, so write each one once and name it by its
  content (such as `artifacts/evidence/SHA256.json.gz`); a later change to
  it is never delivered.
  An eval run keeps a match's bytes as blobs instead (`Result(blobs=...)`,
  [Evaluation](evaluation.md)); its `artifacts/` files are the old layout,
  which `etude convert-eval` packs into blobs.
- **Completion belongs to an attempt.** Resuming a run makes its previous
  completion inapplicable. Until the new writer finishes, the report says
  “running or unfinished.” Older completion records without a writer remain
  readable, but cannot establish completion after another attempt starts.
  An attempt that left its block by raising `Stopped` is recorded `stopped`,
  not `failed`, and the report keeps saying “running or unfinished.”
  An attempt refused before it began, such as a resume with another config,
  records nothing: a finished run stays finished.

### Row tables for training results

<!-- implementation-reference -->
```python
from etude.train.tracking import track

with track(
    experiment="self-play",
    run="policy-seed-0",
) as run:
    run.log_table(
        "decks",
        [
            {"deck": "red", "opponent": "blue", "seat": 0, "wins": 12, "games": 20},
            {"deck": "blue", "opponent": "red", "seat": 1, "wins": 8, "games": 20},
        ],
        updates=400,
    )
```

This records `decks` at optimizer update 400. Adding a deck pair adds a row,
keeping metric names bounded. Use `run.log` for scalar summaries such as loss
and `run.log_table(name, rows, **clocks)` for JSON rows, including nested objects
and lists. Give every declared clock, using absolute continuation coordinates.
Keep table names separate from scalar and ordinary text names.

`log_table` writes locally before returning; you can then change the input rows.
Invalid rows or clocks and local write failures raise. Its queued reference
follows `log`'s overflow behavior: a dropped reference increments `run.dropped`.

Each table is an immutable JSON file under `artifacts/`, named by its content
digest; the metric row holds only that digest. Identical content reuses the file.
Writes stage as `.partial` and rename atomically before recording the reference.

Tracking delivery, `etude sync` and the launcher's courier carry these files.
When copying a run, include `artifacts/`. Readers resolve references beside the
selected metric block, so relocated runs keep working.

<!-- implementation-reference -->
```python
from etude.storage import default_root
from etude.train.read import read

history = read(default_root(), "self-play", "policy-seed-0")
for table in history.read_table("decks", "updates"):
    print(table.x, table.rows)  # selected update coordinate and its JSON rows
```

`read_table` returns `TablePoint` records in clock order as a tuple. An empty
table has `rows=()`; no surviving references returns `()`. `RunData.columns`
gives each metric block's rows by column (`etude.train.block.Columns`) for raw
inspection; these are not JSON row tables.

The reader applies attempt and update supersession before fetching artifacts.
A later table at the same update replaces the earlier one. A resumed attempt
cuts off earlier attempts' history from its first logged row on, tables and
scalar points alike. Unreferenced
artifacts yield no rows. Do not reconstruct history by listing `artifacts/`.

A missing or damaged selected table raises. After interrupted delivery, run
`uv run etude sync --directory DIR --to s3://my-bucket/runs` and retry. Restore
damaged artifacts from an intact copy, never different rows under the same digest.

Etude warns once per writer when distinct numeric metric names exceed
`track(..., metric_name_limit=1000)` (the default), pointing to `log_table`.
Set another nonnegative limit, or `None` to disable. Earlier local attempts
count; clocks, flags, text and table references do not. Store dimensions such
as deck × opponent × seat × outcome in rows and keep scalar names for charts.

Index refreshes reuse block descriptions and latest values, rebuilt once on
writer start or resume. Adding a permanent block rewrites the accumulated
manifest without rereading old blocks. Wider rows and more metric names still
cost more. Existing blocks and manifests remain readable without migration.

### Storage outages and recovery

```sh
# On a computer holding the retained runs directory, after storage recovers:
uv run etude sync --directory /data/retained-runs --to s3://my-bucket/runs
```

This delivers retained files and is safe to repeat. Use the runs directory
containing the `RUN/` folders. It needs the same bucket credentials and
`etude-sdk[s3]` installation as other bucket operations; it cannot recover files
from a machine whose disk is gone.

Failed delivery leaves local files available for a later pass; background
delivery resumes when storage recovers. Reports reading the bucket show only
what has arrived there.

SDK-created boto3 clients use 60-second connection and read socket timeouts,
so a dead connection releases the uploader once boto3's own retries are spent.
Set `ETUDE_STORAGE_TIMEOUT_SECONDS` to a positive finite number of seconds before
creating storage to override both. Boto3 streams large uploads in chunks and
uses multipart transfers; these are socket limits, not a total checkpoint
transfer deadline. A supplied `S3Storage(client=...)` keeps that client's
`botocore.config.Config` connection/read timeouts and retry settings. Transport
failures raise `etude.storage_timeout.StorageUnavailable`; streamed reads retry
up to three times, and background delivery retries retained files on a later
pass. Worker and launcher uploads use the [same storage timeout policy](launcher.md#timeout-ownership).

`run.finish(timeout=10)` seals the run and retries final delivery within its
allowance. Increase `timeout` in seconds to allow longer for final delivery.
An upload already in progress keeps its socket timeout; `finish()` can return
before that upload ends, so expiry does not confirm cancellation or delivery.
The Python process exits only when boto3's transfer threads end. boto3 retries
each request itself (five attempts by default; set `AWS_MAX_ATTEMPTS`), so on a
dead connection exit can wait attempts × `ETUDE_STORAGE_TIMEOUT_SECONDS`: five
minutes with the defaults.
If files remain undelivered, it reports their local location for `etude sync`.
For launched runs, the worker owns delivery and its final allowance is capped
by the rental deadline. Inspect `etude status RUN` and `etude logs --run RUN` for
delivery failures and the age of the retained evidence; preserve the local files
before the machine is deleted if storage is still unavailable.

### Checkpoints

```sh
uv run etude checkpoints --source s3://my-bucket/runs
```

[Training](../training.md#checkpoints) covers publishing: `run.checkpoint(policy=model)`
for named objects, `run.checkpoint(path)` for a file of your own format.

<!-- implementation-reference -->
```python
model = run.checkpoint(
    "out/latest.pt", updates=63_000, learner_transitions=n, training_seconds=t, tags=["periodic"]
)
run.tag(model, "best")  # tags can change later
```

- **A checkpoint is a published model**: a folder of named files, or one
  file, and where every declared clock stood, numbered within the run from
  one, across every attempt on every machine. `checkpoint` returns its
  reference; pass that to whatever evaluates it.
- **Etude stores it, in your storage.** Named objects are written to
  `out/checkpoints/CLOCK-VALUE/` with a `checkpoint.json`. A file already in
  `run.out` is recorded in place and must stay unchanged. Any other local file
  is copied to `checkpoints/CLOCK-VALUE.EXT` before `checkpoint` returns, so
  the caller can overwrite its source. All are uploaded before their records.
  A folder's digest is over a manifest of its files' digests. An address such
  as `s3://...` records a reference only. Removing stored files by rule is
  not built.
- **Nothing recorded is replaced.** Recording the same model at the same
  `at` again returns the existing reference and sends nothing. A
  different model at a recorded `at` is a new checkpoint,
  `CLOCK-VALUE.ordinal-N.EXT` beside the first. A name some file already
  holds is never reused, even one an interrupted attempt kept without its
  record. Only tags change later.
- **A later attempt sees every checkpoint.** `run.checkpoints()` lists them,
  oldest first, and ordinals continue. They are part of `out`, so they are
  restored with it on another machine and copied with it into a continuation
  ([the launcher](launcher.md#the-run-promise)). Carrying a run on uses its
  saved state, not a checkpoint.
- **Readers give storage addresses.** `etude checkpoints --source` and
  `etude.train.read` report each file where it is in the source read, not
  where the machine that wrote it kept it; a record whose file has not arrived
  is listed as recorded.

### Etude's clocks

<!-- implementation-reference -->
```python
with track(config=config) as run:
    run.resume(model=model, optimizer=optimizer)
    for update in run.steps("updates", to=config.updates):
        run.log({"rl/loss": one_update(model, optimizer)})
        run.save()
        if run.checkpoint_due(1):  # every running hour
            run.checkpoint(policy=model)
```

Every row and checkpoint of a training run carries two clocks Etude keeps,
in hours, which no run declares or logs:

- **`running_hours`** is how long the learner line has run. It starts at
  `track(...)`, so installing, booting and queueing never count, and goes on
  from its value at the save the attempt resumed, or at the checkpoint a
  continuation started from, as `updates` does. What a crashed attempt ran
  past its last save is dropped on resume, with its rows.
- **`hours_since_start`** is wall time since the line's first attempt began,
  gaps between attempts included.

A checkpoint saved before Etude recorded them gets both from its wall time
and its run's attempt records (`metrics/WRITER.json`); rows logged before
have neither. Where a checkpoint has neither, such as a
run copied without those records, the report says so at its top, naming the
runs: charts and seats on these clocks leave such a checkpoint out.

`run.at(clock)` says where the run stands on any clock;
`run.checkpoint_due(every, clock="running_hours")` whether the clock has
passed a multiple of `every` since the latest checkpoint. Either clock can be
a job's `until`, the page's x axis, or a seat's clock
(`Earlier(clock="running_hours", by=2, at_or_before=True, ...)`,
[evaluation](evaluation.md)). A loop that runs to an hour target rather than
a count asks for it with `run.until("running_hours", default=10)`
([the pieces underneath](training.md#the-pieces-underneath)). Metric timers
such as `time/collection` stay in seconds.

### Reading a long run's metrics

<!-- implementation-reference -->
```python
from etude.train.read import read_batches

for batch in read_batches(
    "r2://my-bucket/runs",
    "capacity-pilot",
    "capacity-64-seed-0",
    clock="updates",
    metrics=["rl/loss", "collection/size"],
):
    loss = batch.points["rl/loss"]  # Points: x (updates, ascending) and y
    for update, value in zip(loss.x, loss.y):
        print(int(update), value)
```

This prints every retained `rl/loss` value, one optimizer update per line,
from the first update to the last. The run's history arrives one batch at a
time, so a run of any length can be read on a small machine. Memory holds
one batch, the selected metrics' arrays for the blocks in it, plus a few
numbers per block. The batch size sets the limit, not the run's length or
how many other metrics it logged.

`read_batches` follows the same rules as `read(...).points(metric, clock)`.
Concatenate one metric's `x` and `y` across all batches and you get exactly
what `points` returns. Batches come in ascending clock order and their ranges
never overlap. A later write at the same clock value replaces an earlier one,
and a resumed attempt's rows replace earlier attempts' from where it resumed.
Rows that did not give the clock are left out, and so are rows that did not
log the metric. A batch can therefore hold a different number of points for
each metric, or none. A name no block logged as a metric raises
`EtudeError("metric_not_logged")` before the first batch, so a typo is not
read as an empty history; `points` gives empty points for it instead.
`metrics` is a list, even for one name. `clock` defaults to `hours_since_start`.

A batch is normally one block. Blocks whose clock ranges overlap are read
together, because the latest write must win. This happens when a process
logged the same clock value again or its clock went backwards. Each such
overlap makes that one batch larger.

`read_batches` reads each block twice: once to plan the batches, keeping a few
numbers per block, and again for its clock and selected arrays. From a bucket
the block arrives once and the second read comes from Etude's storage cache.
It never loads a writer's metric index, so it also works on runs whose index
is too large to load. It checks each array's CRC before yielding it. Rows not yet sealed are not read, as
with `read`.

A gap in a writer's block sequence, a block that stays missing after fresh
listings, or a damaged array raises during iteration. When that happens the
batches already yielded are correct, but the history is incomplete; don't
treat it as finished. A block a compaction replaced while you were reading is
found again from a fresh listing, however slowly you read. Without the index
`read_batches` can't tell that a writer's newest blocks are missing: if they
never reached the bucket, the history ends early, as it does for a run still
being delivered. If delivery was interrupted, run `uv run etude sync --directory DIR
--to s3://my-bucket/runs` and read again.

`read(source, experiment, run)` returns the whole run at once, every metric
included, for inspection and short runs. For a long run with many metrics,
use `read_batches`.

