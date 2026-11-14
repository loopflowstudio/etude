# Tracking

Log from any process wherever it runs, and read it back. Tracking writes
files to your own directory or bucket and reads from them. There is no server
in this path, and nothing here starts compute or holds a provider credential.

A training loop Etude calls is written as [Training](training.md) describes:
`etude train` or the launcher says which run it is, and `track` takes only
a config; steps and log keywords declare its clocks. This page covers what every tracked process shares: rows,
tables, storage, reading and reports. A process Etude did not start, such as a
notebook or another scheduler's job, names its run itself:

```python
from etude.storage import storage_from_uri
from etude.train.tracking import track

run = track(
    experiment="capacity-pilot",
    run="capacity-64-seed-0",  # new within its experiment: a taken name is refused
    job="capacity-64",  # what its siblings share
    config=open("config.json").read(),  # kept byte for byte
    storages=[storage_from_uri("s3://my-bucket/runs")],  # optional
)
for update in training():
    run.log({"rl/loss": loss}, updates=update, learner_transitions=n)
run.finish()
```

```sh
uv run etude report capacity-pilot.py --open   # the experiment's page (see Reports below)
uv run etude sync --directory DIR --to s3://my-bucket/runs   # deliver later, or again; idempotent
uv run etude checkpoints --source s3://my-bucket/runs
```

Runs are written to one place per computer, `~/.etude/track` (moved by
`ETUDE_HOME`, or named with `track(directory=...)`), each in
`EXPERIMENT[/JOB][@SEED]/`, and `report` reads that place when given no source. So writing never chooses how it will be read.
`report` and `checkpoints` take `--source`, a directory or
`s3://bucket/prefix` or `r2://bucket/prefix`, and find runs in it at any depth. Attempts on rented
machines write to the same run folder; pointing `--source` at the
storage's prefix reads the whole run.

One `etude` command provides compute, reports, sync and checkpoints. The
launcher (`etude.launch`) and tracking share one package and lock file; neither
imports the other. Tracking is `etude.train` for training runs, built on
what every run shares: `etude.storage`, `etude.records`, `etude.checkpoints`
and `etude.errors`, with its declaration, listing and delivery in
`etude.run`.
Install `etude-sdk[s3]` for tracking against a bucket; local tracking does not
need boto3. `s3://` uses boto3's own configuration. `r2://` reaches Cloudflare
R2 at `https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com` with `R2_ACCESS_KEY_ID`
and `R2_SECRET_ACCESS_KEY`, as DuckDB's `r2://` does. Tracking commands read the paths supplied on this computer, even
when a launcher controller is selected. Each command loads its own dependencies;
help and the worker do not load NumPy.

Under `etude train` and `etude launch` the run has already been named, so the
loop gives only its clocks and config:

```python
with track(config=config) as run:
    ...
```

The run, job, seed and cohort come from the command (`ETUDE_RUN`,
`ETUDE_JOB`, `ETUDE_SEED`, `ETUDE_COHORT`), and `run.seed` reads the seed.
The experiment is the launched file's stem (`ETUDE_EXPERIMENT`):
`etude launch ppo.py` supplies `ppo`, including when the file defines only
`runs()`. An explicit `--experiment` overrides the file and its job labels;
without it a plain job file preserves an explicit `Job.experiment`. Naming another
experiment in `track` is refused (`experiment_differs_from_launch`), so a
reusable training function names none. A process started by another
scheduler sets the same variables to say which run it is.
The files are written to `ETUDE_TRACK_DIR`, one place on the machine for every
run and attempt, and the launcher carries them to the machine's bucket as each
file is finished, by the same rules as `sync` (`etude.carriage`): every
checkpoint is in the bucket while the job still runs. Tracking reports the
highest value logged on each clock to the launcher's `etude-result.json`, for
`etude status`; checkpoints are the run's alone. Attempts share the run's files,
including in the launcher's bucket. Tracking never imports the launcher and works without it.

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
  Job: a run without its seed. The report draws the mean of its seeds, banded
  by their range, and shows how many seeds each point has.
- **`log` cannot hurt training by default.** It is a put on a bounded queue:
  no storage or network I/O, no waiting for delivery. It warns once about bad
  coordinates or a full queue; only `strict=True` raises for bad coordinates.
  A full queue drops the row and counts it in
  `run.dropped`. `track` is strict and raises on a bad declaration.
- **The files are the record.** Everything about a run is in one folder named
  for its experiment and id, `EXPERIMENT[/JOB][@SEED]/`, in the runs directory and in
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
pass. Worker/controller uploads use the [same storage timeout policy](launcher.md#timeout-ownership).

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

[Training](training.md#checkpoints) covers publishing: `run.checkpoint(policy=model)`
for named objects, `run.checkpoint(path)` for a file of your own format.

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

## Reports

```sh
uv run etude report ppo.py --source r2://bucket/runs --serve --open
```

This opens the experiment's Evals, Perf and ML details at
`http://127.0.0.1:8765/`. Click **Refresh** to read newly delivered results or
an edited `report()`. Leaving the page open does not poll storage or reload
on a timer. The page shows its source and generation time, so you can tell
which results you are reading.

Install `etude-sdk[eval]` to report evaluations and `etude-sdk[s3]` for a
bucket source; install `etude-sdk[eval,s3]` for both. The experiment file must
be importable with its own dependencies. Without `--source`, the report reads
the local tracking directory described above.

```python
# ppo.py: experiment "ppo", beside its train() and eval(state)
from etude.report import Anchors, Curve, Matches, Matrix, Ranking, Report, Score

from arena import LADDER  # the Benchmark eval(state) holds


def report() -> Report:
    return Report(
        score=Score(table="plays", value="won", reference=0.5),
        evals=(
            Curve(title="Win rate against the anchors", matches=Matches(tag="anchor")),
            Curve(title="Against the checkpoint before", matches=Matches(seating="vs previous")),
            Matrix(rows="deal", columns="on_play", matches=Matches(tournament=LADDER.name)),
            Ranking(by="on_play"),
            Anchors(),
        ),
        baseline="ppo",
    )
```

```sh
uv run etude report ppo.py --source r2://bucket/runs --open   # writes ./reports/ppo.html
uv run etude report ppo.py --watch 5 --open   # opt in to checks and page reloads every 5 s
uv run etude report ppo.py --serve            # refresh on demand, writing nothing
uv run etude report experiments/ --serve      # every experiment there, with an index at /
```

Without `--serve`, the result is one HTML file per experiment, with its data
inlined: no server and no network to view it. Rerun the command to update that
snapshot; reloading a saved HTML file cannot fetch new training results.
The experiment is named for the file, and the page follows the
file's `report()`; a file without one gets `Report(other_metrics=True)`, every
logged metric, and the page says so at its top. Reports read files and never
launch or rent anything.
From a directory the page goes to
`reports/` beside its runs, from a bucket to `./reports`; `--out DIR` chooses.
`--serve` has no login and defaults to this computer only
(`--host 127.0.0.1 --port 8765`). Keep the command running to use Refresh;
Ctrl-C stops it.

**Refresh** checks the experiment file, the modules it imports from its
repository, and its source, then replaces the served page when a generation
succeeds; an edited helper or [report helper](#report-helpers) module is
read afresh without a restart. The repository is the nearest folder above the
experiment file holding `pyproject.toml` or `.git`, outside hidden folders and
installed packages. Requests for the same report while
that generation is running join it, so several clicks or viewers do not
start duplicate storage reads. Ordinary views reuse the generated page.
If refreshing fails, the last successful page and its generation time remain
available and the failure is reported. Fix the reported source access or
experiment-file error and click Refresh again; an old timestamp is not
evidence of a successful update.

**Automatic refresh is optional.** `--watch SECONDS` checks for changes at
that interval and reloads the open page. Combine it with `--serve` to opt a
served report into periodic refresh. These checks can read or list storage
even when no new results arrive, so use on-demand refresh to avoid idle
storage traffic. For Python callers, `etude.page.render(page)` produces a
snapshot with no timer; `render(page, refresh=5)` opts into browser reloads.
Rendering alone never rebuilds the page's data.

**A folder** is every experiment file in it, at any depth outside hidden
folders: a `.py` file defining `eval(state)` or `report()` at its top level,
recognised without running it, so helper modules and job files are left
alone. It is drawn as a page per experiment and an index: written as
`reports/index.html` beside the pages, or served at `/` with each page at
`/STEM`. The index has a row per experiment showing its runs (running,
finished, failed, restarted), its latest checkpoint, each job's latest score
with a sparkline of its report's first curve, evaluation progress with
failed matches, time trained and latest activity. Experiments with failures
come first, then those with work running. A file that fails to run, or an
experiment with no runs yet, says so on its row. The served index also
refreshes on demand unless `--watch` is supplied. Once discovered, an experiment
keeps its row through syntax errors: the row shows the error and its link still
opens the last successful page, if one was built. Repair the file and refresh
to replace that page. Removing the file removes its row.

**Charts** are uPlot, inlined into the page. Drag across a chart to zoom in
and double-click to zoom out. The cursor is shared by every chart in a
section.

**Two parts.** Evals, then Perf and ML details, each with its own x axis (any
declared clock, `running_hours` or `hours_since_start`); Perf and ML details can be smoothed. Choosing
jobs and filtering are the only controls for the whole page. A section the
report gives no panel (`perf=()`, `ml=()`) is not drawn.

**Training jobs.** Training curves group a job's runs across seeds,
labelled by the job's launch name and seed count ("ppo · 3 seeds"), as its
seeds' mean banded by their range. A seed is a learner line: a run and the
runs continuing it count once. Seeds align on where each checkpoint stood
on the run's first declared clock. The `baseline` job is dotted everywhere; a
job's colour follows launch order. `Report(labels={"ppo": "PPO baseline"})`
replaces the displayed job name, keeping the seed count. Labels change
presentation only: `baseline`, `Planned.jobs` and a seat's `job`, as in
`Aligned(job=...)`, still refer to the launch names. Unlabelled jobs keep their launch names.

**Execution setup** is result provenance, like the seed: (device, code
revision, seed) records how a result was produced, and the page aggregates
over it rather than splitting by it. A job's training runs on different
devices or commits are pooled with its seeds: runs launched under one job
name and config are one job. A config change under the same name shows as two
jobs, each named with its config digest ("ppo 1a2b3c"). The launch name still
addresses both: `baseline="ppo"` dots both, `labels={"ppo": "PPO"}` shows
"PPO 1a2b3c · 3 seeds", `Planned(jobs={"ppo": ...})` draws the planned values for
each and `Aligned(job="ppo")` seats each. The full name, `"ppo 1a2b3c"`,
names one, and a full-name label or plan wins over the launch name's.
`TrainingRunSummary.job` in `eval(state)` is this same name. Games played on different
devices, compute setups or code revisions contribute to the same requested
matches; a replay counts once, from its latest delivered result. There is no Config
control. Ask for a setup under a config's `setup` key, which is part of no
identity; `play` returns the one it actually used as `Result.execution`, read
alongside `matches.seed` and `seats.commit` in `statsdb`
([evaluation](evaluation.md#eval-runs)).
Scientific differences, such as search budgets in `Inference.config`, remain
distinct players. A seating fixes the variant each seat plays, so a curve
draws the variants its selected seatings name; where one point would pool two
configs of one seat, it names the ambiguity instead. Select the intended
seating to resolve it.

**Evals** are the experiment's eval runs ([evaluation](evaluation.md#experiments)).
A checkpoint is drawn where its seat says it stood (`CheckpointRef.at`), else,
for a key a training run of the experiment saved (`RUN/checkpoints/NAME`),
where that run's clocks stood; where both give a clock and differ, the seat's
value is drawn and the page notes it. Two runs' checkpoints holding the same
bytes are two points. A checkpoint's line, the learner it belongs to, is
its seat's `line`, else its training run's line: the first run of the chain
of continuations that led to it. A checkpoint a training run saved
is drawn in that run's job; every other placed checkpoint is drawn in its
learner's curve, labelled by its name, or in one curve of "checkpoints
with no job". `Crosstable` keeps each learner's checkpoints separate and labels
its tabs by job and learner.
Every unplaced player, code or a checkpoint seated without an `at`, is an
anchor, and the page names the unplaced checkpoints.

**Evaluation only.** An experiment with eval runs and no training runs draws
too: the page says "Evaluation only", Perf and ML details say there are no
training runs, and the x axis offers the clocks the seats' `at`s use.
Matches of eval runs held for no tournament are offered as "matches outside a
tournament", each counting once per environment from its latest record,
whichever eval run made it.
Setup is not an environment: games played on CPU and on CUDA, under configs
that differ only in their `setup` key ([evaluation](evaluation.md#eval-runs)),
pool. A value played in two environments shows the one whose latest
match is newest, and the page notes the rest. `score` names an eval table's value, computed as
`etude.eval.query.compare` computes it, with its 95% interval over whole
matches; without one, every numeric column is offered. Each match counts once,
from the eval run that delivered it last (`etude.eval.tournament.played`), so at
the version of the code that played it last. A tournament `eval(state)` returns
is drawn as it wants it now; one it no longer returns, or now defines otherwise
than its eval runs were held with, as they held it.

| Panel | Shows |
|---|---|
| `Curve(split=None)` | the score at each checkpoint, against one anchor the Opponent control picks; `split` draws one chart per value |
| `Curve(matches=Matches(seating="half-training"))` | that seating over the checkpoints it was held for: a line per job, x where each held checkpoint stood |
| `Crosstable()` | one learner's checkpoints against each other, a tab per job and learner, both axes ordered by the clock; an unplayed pair shows `·` |
| `Matrix(rows, columns)` | one job's score per cell at one checkpoint: a finished job's final, else its latest evaluated |
| `Ranking(by)` | a bar per job for each value of `by`; clicking a row filters the page |
| `Anchors()` | the anchors against each other, and each one's strength over matches that seat no checkpoint |

Eval panels name columns of the score's table, and count the matches the
section's control selects (a tournament, or a tag within it) unless they name
their own: `matches=Matches(tournament=..., seating=... or tag=...)`, by the
names `eval(state)` gives them. A seating and a tag are alternatives; naming
both is refused. A curve against one anchor draws the others' scores against
it as reference lines.

Panels draw; seatings say who plays whom. Whom a curve scores against, which
jobs it draws and with which inference config are the seating's
([evaluation](evaluation.md#tournaments)), so a chart that needs another
opponent, job or inference needs another seating.

**Compare jobs over training.** With the `ladder` and `against small`
benchmarks of [Experiments](evaluation.md#experiments):

```python
# capacity.py
from etude.report import Curve, Matches, Report, Score


def report() -> Report:
    return Report(
        score=Score(table="plays", value="won", reference=0.5, unit="games"),
        labels={"small": "Small model", "large": "Large model"},
        evals=(
            Curve(title="Against the heuristic", matches=Matches(seating="vs heuristic")),
            Curve(title="Against half-training", matches=Matches(seating="half-training")),
            Curve(title="Against the previous checkpoint", matches=Matches(seating="vs previous")),
            Curve(title="Large against small", matches=Matches(seating="large vs small")),
            Curve(title="Against small's final", matches=Matches(seating="large vs final small")),
        ),
    )
```

Two of its charts, as values:

```text
Against half-training           Small model · 2 seeds, Large model · 2 seeds
  updates   2,000  4,000  6,000  8,000
  small      0.58   0.61   0.57      ·   not yet played
  large      0.62   0.66   0.64   0.63
Large against small             Large model · 2 seeds
  updates   2,000  4,000  6,000  8,000
  large      0.55   0.59   0.61   ┆      small has no checkpoint at updates 8,000 yet
```

Each point is the seating's value, its first seat's score, at the position
where the checkpoint it was held for stood, whether the other seat is a
fixed player or a relative seat. The relative seat's checkpoint is not drawn
as a point of its own. `large vs small` draws only `large`: its benchmark is
held only for large's checkpoints.

A held checkpoint whose relative seat has no checkpoint is a gap with its
reason. While the seat's run could still save one there, the gap says "not
yet" and fills in once a later pass plays it. Once it can't, because the run
ended or has passed that position without saving there, the gap says
"never", as it does for a `trained=True` seat with only initial weights to
meet. A `Final` seat is "not yet" until its run ends. Where
`Aligned` or `Final` seats several of a job's lines, a point is the mean over
them, as a point of a job is the mean over its seeds.

`Matches(seating="vs weak")` or `Matches(tag="anchor")` without a tournament
covers every tournament holding that seating or tag, each match counted once.
Give a tournament to select only its matches. A tuple of `Matches` unions the
selected matches for that panel. Overlapping selections count a match once,
and jobs remain separate lines. Tag names are literal, not patterns.

The Evals section shows its Matches picker and selection subtitle only when
at least one panel uses the section's selection. When every panel supplies
its own matches, as above, both are hidden. Other controls still apply to
the panels that use them.

**Score labels.** For a table with one score row per game for each subject,
`Score(..., unit="games")` labels the row count in games. For example, a
tooltip can read `0.63 · 95% interval 0.53–0.72 · 64 games`.
The unit names the counted rows; it does not multiply a row by the number of
games in its match. Leave the unit unspecified for the default `rows` when
the table has another granularity. `per` still controls the score's
denominator, not the row count's label. Display rounding changes neither
the stored values nor the calculation. The interval resamples whole
matches, even when the displayed count is games; with fewer than two
matches, no interval is shown.

Gaps stay visible. A checkpoint nothing played is marked not evaluated and is
no point of a line. One `eval(state)` wants that no eval run has delivered is
marked not yet played, and failed matches, including any stored with no
rows, are marked and listed with their errors; both break a line rather than
bridge it. A job's point needs every seed. A column, tournament,
seating or tag a panel names that nothing holds is said so.

**Perf** draws what Etude records for training runs: `Pace` (each step clock
per wall second between checkpoints), `Elapsed` (wall hours to each
checkpoint), `Attempts` (writer lifetimes), and `Flame`, an icicle of the seconds logged under
`time/` (`time/collection`, `time/collection/inference`: seconds since the
previous row). **ML details** default to reinforcement learning's `Counters`
and `Series` over `rl/` keys; pass `perf=` or `ml=` to name an experiment's
own. Keys a panel names that no run logged are listed.

A report reads and draws only the keys named by `Series` and `Counters` and
the prefixes named by `Flame`. It reads each immutable block once, whole,
instead of parsing the cumulative training index, retaining only the selected
columns of its header; charts then read the block's arrays from Etude's
storage cache. All clocks, checkpoints and continuation coordinates are
preserved. Memory still includes one full block while it is read and the
selected series; this does not bound the size of an individual block.

`Report(other_metrics=True)` also draws every metric no panel names, last
under ML details, by parsing each training run's cumulative index. An
experiment without `report()` gets this, since it declares nothing; for a large
metric vocabulary, define `report()` and name the metrics to draw.

**What a build reads.** Perf and ML details read the metric blocks of training
runs only, once per build; `eval(state)` is given the runs' checkpoints without
reading any metric data. An eval run contributes its `run.json`, `outcome.json`
(historically `state.json`), its tournament or environment record and each
block's index and tables; its blobs, such as replay evidence, are never read
to draw a page. Unless
`other_metrics=True`, a build's memory therefore follows the metrics and tables
the report draws, not how much data its runs hold. Bytes a build reads stay in
Etude's storage cache, so building the same page again over unchanged runs
downloads almost nothing: it lists the runs and reads only what was delivered
since. A training index is rewritten as its run grows, so with
`other_metrics=True` a build of a running experiment reads it again whenever it
has changed.

```python
from etude.report import ML, Planned, PlannedPoint, Report


def report() -> Report:
    return Report(
        ml=(
            *ML,
            Planned(
                title="Planned learning rate",
                clock="updates",
                unit="learning rate",
                jobs={
                    "large": (
                        PlannedPoint(at=10_000, value=0.001),
                        PlannedPoint(at=20_000, value=0.0001),
                    ),
                },
            ),
        ),
    )
```

**Configured curves** in ML details show planned values, such as a
learning-rate or damping schedule, separately labelled from logged
observations. Their current-position marker comes from the run's recorded
clock at the time the report was generated. A schedule against optimizer
updates uses absolute update coordinates across continuation; it does not
advance with time spent viewing the page. Refresh to move the marker when
new clock values arrive. A configured curve is not evidence that training
used those values; log a metric to compare the schedule with observations.
`Planned.jobs` uses original job names, full or launch. Points must have finite values and
strictly increasing clock coordinates. Values between samples are linearly
interpolated; no value is extrapolated outside the supplied range. Each seed
gets a filled current-position marker when its recorded clock lies inside that
range; configured sample points are hollow.
If no seed of a job has an `at` in range, the chart says so. A `Planned`
panel uses its own named clock, independently of the section's X axis control.

### Report helpers

Parts several experiments share are ordinary Python: keep them in a module of
the repository and call them from each experiment's `report()`.

```python
# manabot/reports.py
from etude.report import Curve, Matches, Score

GAMES = Score(table="games", value="score", reference=0.5, unit="games")


def label(job: str) -> str:
    """'w160l8' reads 'Width 160, 8 layers'; other names are kept."""
    width, _, layers = job.removeprefix("w").partition("l")
    if not width.isdigit():
        return job
    return f"Width {width}" + (f", {layers} layers" if layers else "")


def against(seating: str, title: str) -> Curve:
    return Curve(title=title, matches=Matches(tournament="frontier", seating=seating))


PANELS = (
    against("half-training", "Against itself at half its updates"),
    against("vs initialization", "Against the initial weights"),
    against("vs heuristic", "Against the heuristic"),
)
```

```python
# experiments/frontier.py
from etude.report import Report

from manabot.reports import GAMES, PANELS, label
from manabot.training.panels import ML, PERF

JOBS = ("w64", "w160l8")


def report() -> Report:
    return Report(
        score=GAMES,
        clock="updates",
        labels={job: label(job) for job in JOBS},
        evals=PANELS,
        perf=PERF,
        ml=ML,
    )
```

Helpers name jobs by launch name, so a job relaunched with a changed config is
still drawn and labelled, once per config ("Width 64 1a2b3c · 3 seeds"). Edit
`manabot/reports.py` and click **Refresh**: the page is rebuilt from the edited
module, with no restart. Don't define `report()` or `eval(state)` at the top
level of a helper module, or `etude report DIR` takes it for an experiment.
