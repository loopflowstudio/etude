# EXP-56 choices

## Implementation assumptions (2026-11-13)

- `run.save()` also writes at the loop's final target, so a finished loop has
  restorable state even when its interval has not elapsed. Reaching the target
  wins over a simultaneous stop: code after the completed loop may run.
- Save manifests include `wall_ms` alongside their clocks. A fresh host has
  no earlier metric-attempt files; the timestamp preserves the origin of
  `hours_since_start` without a checkpoint.
- The optional PyTorch serializer is installed as `etude-sdk[train]`.
  Callback saves need no framework. Registered dictionaries load on CPU;
  the author's objects own their device transfer and format compatibility.
- Pointer acknowledgement is kept as its serialized body in existing sent
  markers, so cleanup on a later pass can retain newer in-flight saves.
  Standalone local cleanup may lag remote retirement by another pass so it
  keeps the names needed to retry deletion. Controller cleanup adds one
  conditional membership read per observed run; a changed state pointer costs
  one pointer read, one state listing and one deletion per older object.

The default recipe, record unification, folder checkpoints and explicit resume
commands are still unbuilt; the docs continue to describe the target experience.

The accepted rebuild is `design-train-api.md`; `docs/training.md` describes its
intended experience. Earlier local_launch/Run.until choices and QA history are
superseded as API direction, retained in git at `1666be1:scratch/questions.md`.
They remain evidence about the existing carriage and retry machinery.

## State publication (2026-11-13): layout settled, two points unanswered

**Why a pointer.** The existing carrier updates each mutable out file
separately, so an interrupted delivery leaves a mixed save
(`uv run python scratch/state-publication.py`: model=save-2,
optimizer=save-1, state.json=save-1, membership unchanged; recovery reads
current object bytes for membership keys, so hashes would detect and not
recover). Direction: "snapshot directory + pointer published last, recovery
follows the pointer". Do not weaken the Training page to per-file atomicity.

**Jack Heart, 2026-11-13**, on the four questions put to him (file sharing;
who deletes under upload-only grants; local retention; running hours):

- "1b is fine, my other feedback was that extra stuff locally we can cleanup
  automatically is ok, but extra uploads are not". 1b was: each save is
  self-contained and unchanged files upload again.
- "2a is the best of thse options". 2a was: the launcher deletes old remote
  saves when the worker cannot.
- On keeping the previous save locally: "i dont think it should need to save
  if its confirmed published online, but i also think we can have some grace
  in our cleanups".
- On what counts as confirmed and how long the grace is: "dunno, dunno".
- Running hours across a resume: not answered.

**As the docs now state it** (`docs/training.md` Carrying on and What is on
disk; `docs/launcher.md` The run promise, with a retention table):

- `out/state/current.json` names `out/state/save-N/`; each save holds every
  registered file and refers to nothing outside its folder. Named state is
  `state/NAME/`. Folders are numbered because `run.save(fn)` outside
  `run.steps` has no stepped clock.
- A delivery pass sends only the save the local pointer names, whole, then
  the pointer. Saves replaced before a pass reached them are never sent.
  This is my reading of "extra uploads are not [ok]" beside 1b: no upload
  beyond one per delivered save; identical bytes are sent again.
- Local: saves numbered below the one storage last acknowledged a pointer
  for are removed (no storage: below the local pointer).
- Remote: the courier deletes below the stored pointer after storing it;
  under an upload-only grant the controller does, with the launcher's keys,
  when its loop looks at the run.

**Unanswered, chosen here, reversible:**

1. *Confirmed* means storage acknowledged the `current.json` naming that
   save or a later one.
2. *Grace* is not a timer: removal happens on the delivery pass after
   confirmation and may lag.
3. `running_hours` continues from the save an attempt resumed, as
   `docs/tracking.md` says; the code continues it from the latest checkpoint
   (merge notes below).

**Awkward, found writing the docs.** With an upload-only grant and no
selected controller, the cleanup loop runs only while a CLI command does. A
run saving every five minutes leaves twelve replaced saves an hour in the
bucket until then, against "ideally avoid remote extra storage". The docs
say so. Question: is that acceptable for upload-only S3, or should such a
launch warn, or deliver saves less often?

**Probes, re-run 2026-11-13 after the docs change.** Both still describe the
existing carrier, not the layout to build: `state-publication.py` reproduces
the mixed save; `state-retention.py` (three saves, identical model bytes,
`delete` returning false) shows 3 model uploads, 3 remote copies, membership
holding the pointer and save 3. Under the settled layout the 3 uploads are
intended; the 2 lingering saves are the controller's to delete. Neither
probe exercises pointer ordering, skipping unsent saves or controller
cleanup; step 1's tests must.

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

5. **How a launch names its experiment.** The design says "Launch commands
   take the name as an argument" with no spelling. Docs show `--experiment
   NAME` on `etude launch` and `etude run`, and still say an
   `eval(state)`/`report()` file requires its own stem.
6. **A finished run taken further.** Design Open 2 records "Both":
   `--resume RUN --until ...` extends the same run, finished or not. Docs
   say a finished run becomes unfinished until the new target, and that
   `--resume` on a finished run with no later target is refused. Whether its
   record reads `finished` in between is not stated anywhere.

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

Not documented: EXP-37's remaining points, which are a separate Task
(`exp-56.md`, Folded in). The docs do state its "application
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
