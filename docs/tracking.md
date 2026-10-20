# Tracking

Declare a run, log from any process wherever it runs, read it back. It writes
files to your own directory or bucket and reads from them. There is no server
in this path, and nothing here starts compute or holds a provider credential.

```python
from etude.runs.storage import storage_from_uri
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
uv run etude reports write           # write a page per experiment and print where
uv run etude reports write --watch 5  # keep regenerating; the open page reloads itself
uv run etude reports serve           # or browse every experiment at http://127.0.0.1:8765/
uv run etude sync --directory DIR --to s3://my-bucket/runs   # deliver later, or again; idempotent
uv run etude checkpoints --source s3://my-bucket/runs
```

Runs are written to one place per computer, `~/.etude/track` (moved by
`ETUDE_HOME`, or named with `track(directory=...)`), and `reports write` and
`reports serve` read that place when given no source. So writing never chooses how it will
be read: generate a page, or run the server, over the same files.
`reports write`, `reports serve` and `checkpoints` take `--source`, a directory or
`s3://bucket/prefix`, and find runs in it at any depth. Attempts on rented
machines write to the same `PREFIX/RUN/` folder; pointing `--source` at the
storage's prefix reads the whole run. The server
has no login and listens on this computer only.

One `etude` command provides compute, reports, sync and checkpoints. The
launcher (`etude.launch`) and tracking share one package and lock file; neither
imports the other. Tracking is `etude.train` for training runs, built on
`etude.runs`: what every run shares (its declaration, files, storage and
delivery).
Install `etude-sdk[s3]` for tracking against a bucket; local tracking does not
need boto3. Tracking commands read the paths supplied on this computer, even
when a launcher controller is selected. Each command loads its own dependencies;
help and the worker do not load NumPy.

Under `etude launch` the launcher has already said which run this is, so the
same script needs only its experiment and clocks:

```python
run = track(experiment="capacity-pilot", clocks=[Step("updates", "optimizer updates, absolute")])
```

The run, job, seed and cohort come from the launcher (`ETUDE_RUN`,
`ETUDE_JOB`, `ETUDE_SEED`, `ETUDE_COHORT`); an argument passed to `track` wins.
The experiment is always named in the script: the launcher does not know it. `run.checkpoint(path, ...)` also writes the launcher's
`etude-result.json`, so the launcher can resume the run from that checkpoint
without the script reporting it twice; `run.log` does not report progress to
the launcher. The files are written to `ETUDE_TRACK_DIR`, one place on the
machine for every run and attempt, and uploaded to the machine's bucket as
each file is finished when it has one. A local checkpoint is copied into the
run's directory before the call returns; the launcher receives a link to that
snapshot (a copy across filesystems). Overwriting the trainer's original file
cannot change the recorded checkpoint. Attempts share the run's files, including
in the launcher's bucket. Tracking never imports the launcher and works without it.

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
  resume from an earlier checkpoint is a new process and does neither.
- **Continuation is lineage.** A run may name a parent run, a clock and the
  value it continues from. The parent is unchanged. The report does not join
  the two yet.
- **Reads are bucketed and keep extremes**: per bucket first, last, min, max,
  mean, count and a count of NaN or infinite values. A range with fewer points
  than buckets returns the points themselves.
- **A job's runs across seeds are its seed sample.** A job is the launcher's
  Job: a run without its seed. The report draws the
  mean and a 95% interval (Student's t over the seeds present in each bucket)
  and shows how many seeds each point has.
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
  in the bucket also holds `attempts/ATTEMPT.log` and `attempts/ATTEMPT.json`.
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
  declared clock stood, numbered within the run from one. `checkpoint` returns
  its reference; pass that to whatever evaluates the model.
- **Etude stores the file, in your storage**, at
  `PREFIX/RUN/checkpoints/CLOCK-VALUE.EXT`, uploaded in the background from the
  process that recorded it. The copy is taken before `checkpoint` returns, so saving over the same
  path afterwards is safe, and the call takes about as long as writing the
  file did. A JSON model uses `.json.data` to keep it distinct from the JSON
  checkpoint record. Under the launcher, `.snapshot` is appended if the
  generated name would replace the trainer's original output path.
  Saving at the same coordinate replaces that checkpoint;
  a later sync delivers the replacement even when its size is unchanged.
  Removing stored files by rule is not built.
