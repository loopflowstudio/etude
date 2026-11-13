# EXP-56 choices

The accepted rebuild is `design-train-api.md`; `docs/training.md` describes its
intended experience. Earlier local_launch/Run.until choices and QA history are
superseded as API direction, retained in git at `1666be1:scratch/questions.md`.
They remain evidence about the existing carriage and retry machinery.

## State publication (2026-11-13): blocked on persistence review

The design promises a whole-or-nothing save of model, optimizer and clocks,
including after a machine loss, over the existing out carriage. The carriage
cannot keep it. Dependent API implementation is stopped until the layout and
retention rule are specified. Do not weaken the Training page to per-file
atomicity or to current transport behaviour.

**Counterexample** (`uv run python scratch/state-publication.py`; file
fixtures through the real `StorageDestination`, no network or training):
save 1 delivered, save 2 interrupted after one file, leaving model=save-2,
optimizer=save-1, state.json=save-1, membership unchanged. Recovery accepts
newer mutable out bytes than membership on purpose: `Courier._restore` and
`BucketJournal.held` select membership keys, then read current object bytes.
Hashes in state.json or out.json would detect the mix without recovering the
overwritten weights; reordering mutable files fails the same way.

**Direction.** Each save is a new immutable snapshot, uploaded, then a small
pointer published last; recovery follows the pointer. Jack Heart on cost:
"local storage probably fine, but ideally avoid remote extra storage". Read
as: local snapshots may be kept; remote extra storage is at most transient,
not a growing history of saves. (Not taken: one atomic archive per save,
extracted for the author's files.)

**As the docs show it** (`docs/training.md` Carrying on and What is on disk,
`docs/launcher.md`, `docs/tracking.md`): `out/state/current.json` names the
save to load; each save is `out/state/save-N/`, written once; delivery sends
the save's files, then the pointer, then deletes the save before. Reversible
spellings: named state is `state/NAME/current.json`; folders are numbered
(`save-00000007`), not named for a clock, because `run.save(fn)` outside
`run.steps` has no stepped clock.

**Retention probe** (`uv run python scratch/state-retention.py`; three
complete saves with identical model bytes through `StorageDestination`, on a
directory fixture whose `delete` returns false as upload-only storage does):
3 model uploads and 3 remote model copies; membership holds only the pointer
and save 3 while saves 1 and 2 remain stored. It checks the existing carrier
against the proposed layout, not a save API or pointer ordering. Why:
`StorageDestination` fingerprints by destination key, so the same bytes
under a new save folder upload again; the storage protocol has no copy
operation; the S3 federation policy grants put/delete/abort without the read
a copy of a prior object needs.

Open, answered by no one. The first two block the build:

1. **How an unchanged file is not uploaded again.** Either each remote save
   is self-contained (needs a copy path and its permissions), or manifests
   share immutable files (`state.json`'s `files` names a file outside its
   save folder; needs file-level reachability and retirement instead of
   deleting the whole replaced save). The docs say only that `files` paths
   are relative to the save's folder.
2. **Who cleans up under upload-only grants**, and what retention holds
   while that owner is unavailable. The courier cannot delete there
   (`docs/launcher.md`: "absence does not depend on deleting bucket
   objects"); membership only controls restoration. The docs leave replaced
   saves in the bucket "until someone with delete rights removes them",
   which conflicts with the Training page's transient remote duplication.
3. **Local retention.** Keep the previous save on the machine as a second
   fallback, or one rule everywhere? The docs delete it in both places.
4. **Running hours across a resume.** `docs/tracking.md` says
   `running_hours` goes on from the save an attempt resumed; the code still
   continues it from the latest checkpoint (merge notes below).

Proposal for review, not recorded direction: immutable files owned by the
state root and shared by save manifests, with launcher-owned cleanup where a
worker cannot delete; specify reader and active-upload protection before
building cleanup. It changes the published storage contract.

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
points (`exp-56.md`, Folded in). The docs do state its "application
declares its restorable state and any discontinuity" point: only registered
objects are restored, and streams or games in flight start fresh.

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
