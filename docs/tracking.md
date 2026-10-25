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
The experiment is always named in the script: the launcher does not know it.
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
- **Completion belongs to an attempt.** Resuming a run makes its previous
  completion inapplicable. Until the new writer finishes, the report says
  “running or unfinished.” Older completion records without a writer remain
  readable, but cannot establish completion after another attempt starts.

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
uv run etude report ppo.py --watch 5 --open   # rewrite it every 5 s; the open page reloads
uv run etude report ppo.py --serve            # serve it live, writing nothing
uv run etude report experiments/ --serve      # every experiment there, with an index at /
```

One HTML page per experiment, with its data inlined: no server and no network
to view it. The experiment is named for the file, and the page follows the
file's `report()`; a file without one gets `Report()`, and the page says so at
its top. The file is run on every build, so an edited `report()` shows on the
next. The page's top says where its runs were read from and when; it reads
files and never launches or rents anything. From a directory the page goes to
`reports/` beside its runs, from a bucket to `./reports`; `--out DIR` chooses.
`--serve` has no login and listens on this computer only; with `--watch` the
served page reloads itself.

Training runs must name the experiment for the file, `track(experiment="ppo")`,
until `etude launch` hands it to the runs it starts.

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
experiment with no runs yet, says so on its row. A served page reloads every
5 seconds, or every `--watch` seconds.

**Charts** are uPlot, inlined into the page. Drag across a chart to zoom in
and double-click to zoom out. The cursor is shared by every chart in a
section.

**Two parts.** Evals, then Perf and ML details, each with its own x axis (any
declared clock, or wall hours); Perf and ML details can be smoothed. Choosing
jobs and filtering are the only controls for the whole page.

**Jobs, not runs.** Every panel draws every job: its runs across seeds,
labelled by the job's launch name and seed count ("ppo · 3 seeds"), as its
seeds' mean banded by their range. Seeds align on where each checkpoint stood
on the run's first declared clock. The `baseline` job is dotted everywhere; a
job's colour follows launch order.

**Evals** are the experiment's eval runs ([evaluation](evaluation.md#experiments)).
A checkpoint agent is bound to the checkpoint its key names, `RUN/checkpoints/NAME`,
and drawn where that run's clocks stood; two runs' checkpoints holding the same
bytes are two points. Every other agent, code or a checkpoint from outside the
experiment, is an anchor. `score` names an eval table's value, computed as
`etude.eval.query.compare` computes it, with its 95% interval over whole
matches; without one, every numeric column is offered. Each match counts once,
from the eval run that delivered it last (`etude.eval.tournament.played`), so at
the version of the code that played it last. A tournament `eval(state)` returns
is drawn as it wants it now; one it no longer returns, as its eval runs held it.

| Panel | Shows |
|---|---|
| `Curve(split=None)` | the score at each checkpoint, against one anchor; `split` draws one chart per value |
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
