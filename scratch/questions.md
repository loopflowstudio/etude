# EXP-56 choices

The accepted rebuild is `design-train-api.md`; `docs/training.md` describes its
intended experience. Earlier local_launch/Run.until choices and QA history are
superseded as API direction, retained in git at `1666be1:scratch/questions.md`.
They remain evidence about the existing carriage and retry machinery.

## Implementation counterexample: state publication (2026-11-13)

The design promises a whole-or-nothing save of model, optimizer and clocks,
including after a machine loss. It also keeps the existing out carriage.
A bounded file fixture through the actual `StorageDestination` delivered save 1,
then interrupted save 2 after one file. It retained model=save-2,
optimizer=save-1, metadata=save-1. No training or network was involved.

Recovery intentionally accepts newer mutable out bytes than membership: both
`Courier._restore` and `BucketJournal.held` select membership keys, then read
current object bytes. Merely adding hashes to state.json or checking out.json
would detect a mismatch without recovering the previous weights: they were
already overwritten. Reordering mutable files has the same failure.

This changes the persistence design, not a reversible spelling. Dependent
implementation is stopped for design review, as implement requires. The
proposed correction is immutable save generations and a commit manifest
published last, with reader pinning and explicit generation retention/retirement.
An alternative is one atomic archive per save, with extraction for the author's
files. The layout and retention rule must be specified before building either.
Do not quietly weaken the documented recovery promise to per-file atomicity.

## Train API docs (2026-11-13): open choices and assumptions

Written while drafting `docs/training.md` from `design-train-api.md`. Nothing
here was decided by a person; each is the spelling the docs currently show.
The design's own Open list (name `track`, `continue` as child run, games in
flight, the public example, save cadence) is unchanged and not repeated.

Open product choices the design does not spell:

1. **Class names for players.** The design renames the noun (`Model` →
   player; Jack Heart: "works for me") but gives no class name, and
   `etude.eval.records.Player` is already the `Model | Code` union. Docs avoid
   naming the class and write `Inference(...).player(ref)` and
   `checkpoint.player(inference)`. Question: what is today's `Model` class
   called (`CheckpointPlayer`? `Player` with the union renamed?), and is
   `Code` kept?
2. **The one checkpoint reference type.** Design: "`CheckpointRecord`, plus
   one reference type" replacing `Checkpoint`, `CheckpointKey`,
   `CheckpointRef`. No name given. Docs keep `CheckpointRef`/`CheckpointKey`
   spellings in `docs/evaluation.md` untouched.
3. **How playing code receives a folder checkpoint.** Docs take the smallest
   extension: `player.checkpoint.artifact.uri` names the folder, which holds
   `checkpoint.json`. Alternative: a `path` accessor on the reference.
4. **`etude launch job.py --resume RUN` limits.** Docs say the job's limits as
   the file now states them bound the whole run, earlier attempts included, so
   raising them in the file is how a run that ran out gets more. Alternative:
   limits fixed at first launch, with flags to add.

5. **How a launch names its experiment.** The design now says "Launch
   commands take the name as an argument" (the launcher is the authority; the
   stem is the default). No spelling is given and the docs do not show one
   yet; they still say an `eval(state)`/`report()` file requires its own stem.
   Simplest reading: `--experiment NAME` on `etude launch` and `etude run`.

Assumptions taken (reversible, simpler option):

- Run id is `NAME-seed-N` (`baseline-seed-7`, as the design writes it; the
  launcher's `RunRecord.name` writes `seed7` today). An unnamed job is named
  for its file stem: `etude run train.py` starts `train-seed-0`. `--name`
  names the job part; the id still carries the seed.
- `etude run FILE` calls the function named `train` in FILE, with its config
  type's defaults; `--config` is a JSON object of changed fields. `--seconds N`
  is a deadline: the run stops saved and exits 75.
- Storage layout is `PREFIX/EXPERIMENT/RUN/` (design: `<experiment>/<run>/`,
  ids unique within an experiment). Launcher docs changed to match; older flat
  `PREFIX/RUN/` folders stay readable. This moves launcher keys and grants.
- Checkpoints from named objects live in `out/checkpoints/CLOCK-VALUE/`
  (eight-digit zero padding, as the design shows). A single file from outside
  `out` is still copied to `RUN/checkpoints/`. A second different checkpoint
  at one position is `CLOCK-VALUE.ordinal-N/`.
- `run.save()` is due five minutes after the last save (design: wall time, no
  number); `run.steps(..., every=25)` switches to a count.
- A checkpoint outside or after `run.steps` is placed at the stepped clock's
  current value and the latest value given for every other clock.
- `run.resume(extra=model)` returns that same object, loaded; Etude saves it
  as it stands, so authors mutate it in place.
- `run.checkpoints()` items expose `at` and `path` (folder or file here).
- `training(..., start=CHECKPOINT)` takes a checkpoint from
  `observe(source, experiment).run(RUN).final`; the loop reads
  `run.input("start")`. `seeds` is a launch option on `Job`, outside job
  identity; `runs()` returns `Job` or `list[Job]`; `job.runs(...)` and
  `RunRequest` remain for hand-built launches.
- Deprecated spellings (`track(seed=)`, `local_launch`,
  `run.latest_checkpoint()`) warn for one release. State files an older loop
  kept in `run.out` are not read by `run.resume`.

Not documented, because nothing user-facing is settled: EXP-37's remaining
points (acquisition mode and loss causes in records, acknowledged checkpoint
durability, fault-injection fixtures). The docs do state its "application
declares its restorable state and any discontinuity" point: only registered
objects are restored, and streams or games in flight start fresh.

## State publication direction (2026-11-13)

Proposed: write each save as a new immutable snapshot, upload it, publish a
small pointer last; recovery follows the pointer. Jack Heart: "local storage
probably fine, but ideally avoid remote extra storage". So local snapshots may
be kept freely; remotely, extra storage should be at most transient (e.g.
content-addressed files so unchanged files are not re-uploaded, and the
superseded snapshot's objects deleted once the new pointer is published), not
a growing history of saves.

### What the docs now show, and what the direction leaves open

`docs/training.md` (Carrying on, What is on disk), `docs/launcher.md` and
`docs/tracking.md` describe it as: `out/state/current.json` names the save to
load; each save is `out/state/save-N/`, written once; delivery sends the
save's files, then the pointer, then deletes the save before.

Spellings taken, reversible: the pointer is `current.json` inside `state/`
(named state: `state/NAME/current.json`); save folders are numbered
(`save-00000007`), not named for a clock, because `run.save(fn)` in a loop
without `run.steps` has no stepped clock.

Open, not answered by anyone:

1. **Local retention.** "local storage probably fine" permits keeping older
   saves on the machine; the docs delete the replaced save in both places,
   one rule. Question: keep the previous save locally as a second fallback,
   or one rule everywhere?
2. **How an unchanged file is not uploaded again.** Either storage copies it
   into the new save folder (needs a copy the worker's grant allows), or
   `state.json`'s `files` may name a file in an earlier save folder, which
   then outlives its save. The docs say only that `files` paths are relative
   to the save's folder, which holds for both. Question: which?
3. **Upload-only grants.** The courier cannot delete there
   (`docs/launcher.md`: "absence does not depend on deleting bucket
   objects"). The docs say replaced saves stay in the bucket, outside
   membership, "until someone with delete rights removes them". Question:
   does the launcher, which holds keys, delete them, or is this left to the
   owner?
4. **Running hours across a resume.** `docs/tracking.md` now says
   `running_hours` goes on from the save an attempt resumed. The merged code
   still continues it from the latest checkpoint (below).

## Merge of main at 66ffb28 into this branch (2026-11-13)

Main's #99 and #105 and this branch changed the same tracking and carriage
code. Resolved as: main's names and types (`RecordMismatch`,
`StoredContentMismatch`, `checkpoint_key`, `RecordedFile.record`, `Until`,
`Fetch`, `(run, key)` sent markers, `RunSummary.at`) around this branch's
behaviour (out carriage, `ReadLink` lengths, `CheckpointRecord.file`,
`ParentRun`, planned stops). Launcher database version is 18: both sides
changed the schema from 15.

Behaviour choices made in the merge:

- `track(resumed_at=)` stays removed, as this branch had it (gap 6: "the
  first `run.log(..., updates=n)` of an attempt sets that point, so a recipe
  author passes nothing"). Main still had the argument and tested it; those
  two tests in `tests/train/test_store.py` stay deleted.
- An attempt's first row now also sets its cut on Etude's two hour clocks.
  `running_hours` restarts from the latest checkpoint's value, so on that
  axis rows an earlier attempt logged after its last checkpoint are replaced
  even when the loop resumed from later state. `hours_since_start` keeps
  them, since wall time moved on (`tests/train/test_hours.py` asserts this).
  Continuing `running_hours` from the save is part of the rebuild.
- `RunData.reached` and `RunSummary.at` include Etude's clocks; tests compare
  the declared ones through `tests.experiments.authored`.

Checks after the merge: `uv run mypy` clean, `uv run ruff check .` and
`uv run ruff format --check .` clean; 812 tests passed across two partial
runs (186 tracking-side, 626 other), then the four launcher files changed
afterwards passed again (58).
