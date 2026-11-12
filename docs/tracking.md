# Tracking

Declare a run, log from any process wherever it runs, read it back. It writes
files to your own directory or bucket and reads from them. There is no server
in this path, and nothing here starts compute or holds a provider credential.

```python
from etude.run.storage import storage_from_uri
from etude.train.records import Step, Time
from etude.train.run import track

run = track(
    experiment="capacity-pilot",
    run="capacity-64-seed-0",  # reusing a name resumes it
    job="capacity-64",  # what its siblings share
    seed=0,  # the only thing that differs
    clocks=[
        Step("updates", "optimizer updates, absolute across continuation"),
        Step("learner_transitions", "transitions the learner trained on"),
        Time("training_seconds", "active training time, pauses excluded"),
    ],
    config=open("config.json").read(),  # kept byte for byte
    storages=[storage_from_uri("s3://my-bucket/runs")],  # optional
)
for update in training():
    run.log({"rl/loss": loss}, updates=update, learner_transitions=n, training_seconds=t)
run.finish()
```

```sh
uv run etude report capacity-pilot.py --open   # the experiment's page (see Reports below)
uv run etude sync --directory DIR --to s3://my-bucket/runs   # deliver later, or again; idempotent
uv run etude checkpoints --source s3://my-bucket/runs
```

Runs are written to one place per computer, `~/.etude/track` (moved by
`ETUDE_HOME`, or named with `track(directory=...)`), and `report` reads that
place when given no source. So writing never chooses how it will be read.
`report` and `checkpoints` take `--source`, a directory or
`s3://bucket/prefix` or `r2://bucket/prefix`, and find runs in it at any depth. Attempts on rented
machines write to the same `PREFIX/RUN/` folder; pointing `--source` at the
storage's prefix reads the whole run.

One `etude` command provides compute, reports, sync and checkpoints. The
launcher (`etude.launch`) and tracking share one package and lock file; neither
imports the other. Tracking is `etude.train` for training runs, built on
`etude.run`: what every run shares (its declaration, files, storage and
delivery).
Install `etude-sdk[s3]` for tracking against a bucket; local tracking does not
need boto3. `s3://` uses boto3's own configuration. `r2://` reaches Cloudflare
R2 at `https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com` with `R2_ACCESS_KEY_ID`
and `R2_SECRET_ACCESS_KEY`, as DuckDB's `r2://` does. Tracking commands read the paths supplied on this computer, even
when a launcher controller is selected. Each command loads its own dependencies;
help and the worker do not load NumPy.

Under `etude launch` the launcher has already said which run this is, so the
same script needs only its experiment and clocks:

```python
run = track(experiment="capacity-pilot", clocks=[Step("updates", "optimizer updates, absolute")])
```

The run, job, seed and cohort come from the launcher (`ETUDE_RUN`,
`ETUDE_JOB`, `ETUDE_SEED`, `ETUDE_COHORT`); an argument passed to `track` wins.
Launched from an experiment file, the run is that experiment's
(`ETUDE_EXPERIMENT`): `etude launch ppo.py` starts runs of experiment `ppo`,
so `track(clocks=...)` needs no `experiment`, and one naming another
experiment is refused (`experiment_differs_from_launch`). Launched from a
plain job file, the script names its experiment.
The files are written to `ETUDE_TRACK_DIR`, one place on the machine for every
run and attempt, and the launcher carries them to the machine's bucket as each
file is finished, by the same rules as `sync` (`etude.carriage`): every
checkpoint is in the bucket while the job still runs. Tracking reports the
highest value logged on each clock to the launcher's `etude-result.json`, for
`etude status`; checkpoints are the run's alone. Attempts share the run's files,
including in the launcher's bucket. Tracking never imports the launcher and works without it.

Rules a caller can rely on:

- **Clocks are declared.** A run states its clocks once, each a step (a count)
  or a time (seconds) with one line of meaning. They are its only x-axis
  options besides wall time, which every row carries. Re-declaring a run with
  different clocks is refused.
- **Every row should give every clock.** A row missing one is charted against
  the clocks it did give, and wall time. The first such row in a run raises an
  `EtudeWarning` at the caller's line; `strict=True` makes it an error.
- **No ordering rule in storage.** A repeated or lower clock value is accepted
  and kept. A read returns the latest write per clock value. A clock that goes
  backwards within one process warns once, and raises under `strict=True`; a
  resume from an earlier checkpoint is a new process and does neither. A
  resumed process's rows replace earlier attempts' from the run's latest
  checkpoint on; one restored from elsewhere, such as its own snapshot, passes
  `track(..., resumed_at={"step": 13})`.
- **Continuation is lineage.** A run may name a parent run, a clock and the
  value it continues from. The parent is unchanged. The report does not join
  the two yet. A launched continuation (`etude continue`) declares this
  itself, from the parent's checkpoint it is handed in `ETUDE_CONTINUES`.
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
  for the run, `RUN/`, in the runs directory and in each storage it delivers
  to: `run.json` (what it is, its experiment included), `state.json`,
  `metrics/` (row blocks and their indexes, from every attempt) and
  `checkpoints/` (each named for where its first clock stood, such as
  `steps-1000.pt` beside `steps-1000.json`). Under the launcher the same folder
  in the bucket also holds `attempts/ATTEMPT.json` and the pieces of the job's log
  it names, under `attempts/ATTEMPT.log/`.
  A run's name is unique in its runs directory whatever its experiment. Config,
  state and checkpoint records are JSON. Rows are columnar block files (the
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

```python
# A local file is stored: copied into the run's directory, digested, and
# delivered to your bucket with the run's other files. An address such as
# "s3://..." records a reference only.
model = run.checkpoint(
    "out/latest.pt", updates=63_000, learner_transitions=n, training_seconds=t, tags=["periodic"]
)
run.tag(model, "best")  # tags can change later
```

- **A checkpoint is a recorded model**: the saved artifact and where every
  declared clock stood, numbered within the run from one, across every attempt
  on every machine. `checkpoint` returns its reference; pass that to whatever
  evaluates the model.
- **Etude stores the file, in your storage**, at
  `PREFIX/RUN/checkpoints/CLOCK-VALUE.EXT`, uploaded in the background before
  its record. The copy is taken before `checkpoint` returns, so saving over the
  same path afterwards is safe, and the call takes about as long as writing the
  file did. A JSON model uses `.json.data` to keep it distinct from the JSON
  checkpoint record. Removing stored files by rule is not built.
- **Nothing recorded is replaced.** Recording the same model at the same
  coordinate again returns the existing reference and sends nothing. A
  different model at a recorded coordinate is a new checkpoint,
  `CLOCK-VALUE.ordinal-N.EXT` beside the first. A name some file already
  holds is never reused, even one an interrupted attempt kept without its
  record. Only tags change later.
- **Resume from the latest.** A later attempt of a run sees every checkpoint
  recorded so far; on another machine the launcher restores the records and
  the latest file before the job starts, and ordinals continue. A continuation gets its parent's
  checkpoint until it records its own. Its file is on this machine:
  `latest = run.latest_checkpoint()`, then load
  `urllib.parse.urlparse(latest.artifact.uri).path`; it is `None` on a first start.
- **Readers give storage addresses.** `etude checkpoints --source` and
  `etude.train.read` report each file where it is in the source read, not
  where the machine that wrote it kept it; a record whose file has not arrived
  is listed as recorded.

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
from etude.report import Anchors, Curve, HeadToHead, Matches, Matrix, Ranking, Report, Score

from arena import LADDER  # the Benchmark eval(state) holds


def report() -> Report:
    return Report(
        score=Score(table="plays", value="won", reference=0.5),
        evals=(
            Curve(title="Win rate against the anchors", matches=Matches(tag="anchor")),
            Curve(title="Against the checkpoint before", matches=Matches(tag="consecutive")),
            Matrix(rows="deal", columns="on_play", matches=Matches(tournament=LADDER.name)),
            Ranking(by="on_play"),
            HeadToHead(),
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
file's `report()`; a file without one gets `Report()`, and the page says so at
its top. Reports read files and never launch or rent anything.
From a directory the page goes to
`reports/` beside its runs, from a bucket to `./reports`; `--out DIR` chooses.
`--serve` has no login and defaults to this computer only
(`--host 127.0.0.1 --port 8765`). Keep the command running to use Refresh;
Ctrl-C stops it.

**Refresh** checks the experiment file, the modules it imports from its
folder, and its source, then replaces the served page when a generation
succeeds; an edited helper module is read afresh without a restart. Requests for the same report while
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
declared clock, or wall hours); Perf and ML details can be smoothed. Choosing
jobs and filtering are the only controls for the whole page.

**Training jobs.** Training curves group a job's runs across seeds,
labelled by the job's launch name and seed count ("ppo · 3 seeds"), as its
seeds' mean banded by their range. Seeds align on where each checkpoint stood
on the run's first declared clock. The `baseline` job is dotted everywhere; a
job's colour follows launch order. `Report(labels={"ppo": "PPO baseline"})`
replaces the displayed job name, keeping the seed count. Labels change
presentation only: `baseline` and panel subjects still refer
to the original job names. Unlabelled jobs keep their launch names.

**Execution setup** is result provenance. Games played on different devices,
compute setups or code revisions contribute to the same requested matches;
a replay counts once, from its latest delivered result. There is no Config
control. Record the actual setup with `Result.execution`, inspect it alongside
`matches.seed` and `seats.commit` in `statsdb` ([evaluation](evaluation.md)).
Scientific differences, such as search budgets in `Inference.config`, remain
distinct players. A curve uses the variant played in its selected matches;
when several remain, it names the ambiguity. Select the intended matchup or
set `Curve(inference={"search": 64})` to choose that exact scientific config.
It selects no execution setup.
When a curve names an opponent, that opponent's scientific config must also
agree across seeds; a shared player name alone does not make variants comparable.

`HeadToHead(matches=Matches(matchup="search vs direct"))` uses the scientific
variants in that matchup on both sides. It averages scores against the other
job's checkpoint positions, then across subject seeds, only when each side's
scientific config agrees. Mixed variants show an ambiguity even when no valid
points remain; select a matchup to resolve it.

**Evals** are the experiment's eval runs ([evaluation](evaluation.md#experiments)).
A checkpoint is drawn where its seat says it stood (`CheckpointRef.at`), else,
for a key a training run of the experiment saved (`RUN/checkpoints/NAME`),
where that run's clocks stood; where both give a clock and differ, the seat's
value is drawn and the page notes it. Two runs' checkpoints holding the same
bytes are two points. A checkpoint's line, the learner it belongs to, is
its seat's `line`, else its training run. A checkpoint a training run saved
is drawn in that run's job; every other placed checkpoint is drawn in its
line's curve, labelled by the line's name, or in one curve of "checkpoints
with no line". `Earlier`, `Fraction`, `At` and `Crosstable` look only within a line.
Every unplaced player, code or a checkpoint seated without a position, is an
anchor, and the page names the unplaced checkpoints.

**Evaluation only.** An experiment with eval runs and no training runs draws
too: the page says "Evaluation only", Perf and ML details say there are no
training runs, and the x axis offers the clocks the seats' positions use.
Matches of eval runs held for no tournament are offered as "matches outside a
tournament", each counting once per environment from its latest record. One
comparison played in several environments shows the environment whose latest
match is newest, and the page notes the rest. `score` names an eval table's value, computed as
`etude.eval.query.compare` computes it, with its 95% interval over whole
matches; without one, every numeric column is offered. Each match counts once,
from the eval run that delivered it last (`etude.eval.tournament.played`), so at
the version of the code that played it last. A tournament `eval(state)` returns
is drawn as it wants it now; one it no longer returns, as its eval runs held it.

| Panel | Shows |
|---|---|
| `Curve(split=None, opponent=None)` | the score at each checkpoint, against one anchor; `split` draws one chart per value |
| `Curve(inference={"search": 64})` | one scientific variant when several play the same checkpoint; `None` resolves from the selected matches |
| `Curve(opponent="heuristic")` | against the player of that name, whatever the Opponent control says |
| `Curve(opponent=Earlier(clock="updates", by=400))` | each checkpoint against the one of its line exactly 400 updates earlier; none there is a gap that says so |
| `Curve(opponent=Fraction(clock="updates", value=0.5))` | each checkpoint against the one of its line at exactly half its updates (6,000 v 3,000); none there is a gap |
| `Curve(opponent=At(clock="updates", value=200))` | each checkpoint against the one of its line at update 200 |
| `Crosstable()` | one line's checkpoints against each other, a tab per line, both axes ordered by the clock; an unplayed pair shows `·` |
| `Matrix(rows, columns)` | one job's score per cell at one checkpoint: a finished job's final, else its latest evaluated |
| `Ranking(by)` | a bar per job for each value of `by`; clicking a row filters the page |
| `HeadToHead()` | a job's score in games against another job's checkpoints |
| `Anchors()` | the anchors against each other, and each one's strength over matches that seat no checkpoint |

Eval panels name columns of the score's table, and count the matches the
section's control selects (a tournament, or a tag within it) unless they name
their own: `matches=Matches(tournament=..., matchup=... or tag=...)`, by the
names `eval(state)` gives them. A matchup and a tag are alternatives; naming
both is refused. A curve against one anchor draws the others' scores against it as
reference lines.

For a matchup seating search against no search at the same checkpoint, two
curves can deliberately show the two scientific variants:

```python
Curve(
    title="Search against direct",
    matches=Matches(matchup="search vs direct"),
    inference={"search": 64},
)
Curve(
    title="Direct against search",
    matches=Matches(matchup="search vs direct"),
    inference={"search": 0},
)
```

**Compare jobs across selections.**

```python
# capacity.py: jobs "small" and "large" evaluated in benchmark "paired",
# checkpoints every 200 updates.
from etude.report import Curve, Earlier, Fraction, Matches, Report, Score


def report() -> Report:
    return Report(
        score=Score(table="plays", value="won", reference=0.5, unit="games"),
        labels={"small": "Small model", "large": "Large model"},
        other_metrics=False,
        evals=(
            Curve(
                title="Against half-training checkpoints",
                opponent=Fraction(clock="updates", value=0.5),
                matches=(
                    Matches(tournament="paired", tag="half-training-small"),
                    Matches(tournament="paired", tag="half-training-large"),
                ),
            ),
            Curve(
                title="Against the previous checkpoint",
                opponent=Earlier(clock="updates", by=200),
                matches=(
                    Matches(tournament="paired", tag="previous-small"),
                    Matches(tournament="paired", tag="previous-large"),
                ),
            ),
            Curve(
                title="Large against small at equal training steps",
                matches=Matches(tournament="paired", tag="head-to-head"),
                subject="large",
            ),
            Curve(
                title="Large against small at equal training time",
                matches=Matches(tournament="paired", tag="equal-hours"),
                subject="large",
            ),
        ),
    )
```

The first two charts each draw a line per job from its own tagged matches.
Both players of such a matchup are checkpoints, so `opponent` keeps each
point the newer checkpoint's score; without it, the older checkpoint's
complementary score is drawn at its own position on the same line.
The last two draw only the large model's score against the small model;
the small model's complementary score is omitted. The tags must be present
on the benchmark's matchups, and the matching results must have been
delivered to the report's source. This spec selects results; it schedules
no evaluations.

A tuple of `Matches` unions the selected matches for that panel. Overlapping
selections count a match once, and jobs remain separate lines. Give an
explicit tournament to select it exactly. With only `Matches(tag="anchor")`,
the report prefers a matching tournament with results over an empty one;
it does not combine every tournament carrying that tag. Use an explicit
tuple to combine selections. Tag names are literal, not patterns.

`Curve(subject="large")` selects the job whose score to show; `opponent`
still selects whom it is scored against. Omitting `subject` keeps all
eligible subjects. A missing subject is reported as missing rather than
substituted with another job.

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
marked not yet played, and failed matches are marked and listed; both break a
line rather than bridge it. A job's point needs every seed. A column, tournament,
matchup or tag a panel names that nothing holds is said so.

**Perf** draws what Etude records for training runs: `Pace` (each step clock
per wall second between checkpoints), `Elapsed` (wall hours to each
checkpoint), `Attempts`, and `Flame`, an icicle of the seconds logged under
`time/` (`time/collection`, `time/collection/inference`: seconds since the
previous row). **ML details** default to reinforcement learning's `Counters`
and `Series` over `rl/` keys; pass `perf=` or `ml=` to name an experiment's
own. Keys a panel names that no run logged are listed, and metrics no panel
names are drawn last.

For a large metric vocabulary, use `Report(other_metrics=False)`. Only keys
named by `Series` and `Counters`, and prefixes named by `Flame`, are read and
drawn. This reads one immutable block header at a time instead of parsing the
cumulative training index, retaining only the selected columns. All clocks,
checkpoints and continuation coordinates are preserved. Memory still includes
one full header and the selected series; this does not bound the size of an
individual block header. The default, `other_metrics=True`, includes all logged
metrics.

```python
from etude.report import ML, Report, Schedule, SchedulePoint


def report() -> Report:
    return Report(
        ml=(
            *ML,
            Schedule(
                title="Planned learning rate",
                clock="updates",
                unit="learning rate",
                jobs={
                    "large": (
                        SchedulePoint(at=10_000, value=0.001),
                        SchedulePoint(at=20_000, value=0.0001),
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
`Schedule.jobs` uses original job names. Points must have finite values and
strictly increasing clock coordinates. Values between samples are linearly
interpolated; no value is extrapolated outside the supplied range. Each seed
gets a filled current-position marker when its recorded clock lies inside that
range; configured sample points are hollow.
If no seed of a job has a position in range, the chart says so. The schedule
uses its own named clock, independently of the section's X axis control.
