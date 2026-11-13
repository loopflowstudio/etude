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

Folder checkpoints, the local CLI recipe and explicit local resume are built.
Run/attempt record unification and launched explicit resume remain unbuilt.
`CheckpointRecord.player(inference)` takes anything with `player(ref)`, so the
shared record names no evaluation type.

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
   `docs/tracking.md` says (built: `Run.__init__` reads `saved_states`;
   `tests/train/test_state.py::test_running_hours_resume_from_saved_state_instead_of_a_checkpoint`).

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

1. **Class names for players.** Reversible implementation choice: the existing
   `Player` union already covers checkpoint and code seats, so keep it and
   `Code`; rename the checkpoint case `CheckpointPlayer`. `Inference.player`
   and `CheckpointRecord.player` are the taught constructors. Released
   `Model`, `Checkpoint` and `.model(...)` spellings warn, removed in 0.5.
2. **The one checkpoint reference type.** Chosen here: `CheckpointRef` in
   `etude.tracking.checkpoints`, with an encoded string key. `checkpoint_id` builds
   it from run/name or an outside address. Historical structured keys still
   decode; pinned match/seating hashes are unchanged. `CheckpointKey` is a
   deprecated constructor alias, not another record.
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

Checkpoint implementation choices (2026-11-13): folder artifacts carry a
`members` map of relative files to the existing `cache.Content` digest/length
record. The folder digest hashes canonical JSON of that map; length is the
sum of member lengths. Every member becomes an ordinary verified `Input`
under one directory name; no folder archive or extra remote object. Callback
exports list their paths in `checkpoint.json`; named objects reuse the state
serializer. Exports must be self-contained and use the carrier's published
names (no symlinks, dot paths or `.open`/`.partial` files).

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

## Merge of main at 66ffb28 (2026-11-13)

The field-by-field merge choices and check receipts (812 tests across two
partial runs, then 58 launcher checks; type/lint clean) are preserved at
`9a762a1:scratch/questions.md`. Keep the current invariants: first durable
rows set attempt cutoffs; running hours resume from saved state, falling back
to a checkpoint; hours since start include gaps. Launcher database version 18
includes both sides' schema changes. `RunData.reached` and `RunSummary.at`
include Etude's clocks.

## `RunRequest` name taken on main (2026-11-13, realign)

`afcda22` (#105) named the controller's record/status/cancel message
`etude.launch.controller.RunRequest`. The design renames launch `Run` to
`RunRequest` (Jack Heart: "ok, RunRequest works"). Assumed, reversible:
rename the internal controller message to `RunOperation` and keep the
design's public name (implemented).

Local CLI choices (2026-11-13): an omitted `--seed` on resume uses the stored
seed; config defaults come from the current recipe, overlaid by `--config`,
then must match the stored config. An omitted `--until` keeps the previous
attempt's target. SIGINT/SIGTERM request a saved stop. Python callers can opt
in with `track(resume=True)`; launcher attempts opt in via their allocation.

Experiment scoping must update readers in the same cut as launcher storage:
`run_folders` groups by run name, so two experiments with the same run id
currently share one folder group in `read_all`/evaluation queries. Local
`etude run` writes the designed nested layout; its direct resume reads the
experiment's own base. Cross-experiment report/query identity remains part of
the unfinished scoping work, not a supported final implementation.

## Found reading the docs as a user (2026-11-13, write-docs)

Nobody answered these; `docs/training.md` (Running it) states the built
behaviour for each.

1. **Config on a local `--resume`.** Built and now documented: the config is
   the file's current defaults overlaid by `--config`, and must equal the
   stored one, so a run started with `--config '{"lr": 0.001}'` must repeat
   it on every resume (the page's own example was refused before this edit).
   The seed and target are instead taken from the run when omitted. Question:
   should an omitted `--config` on `--resume` mean the run's stored config,
   like the seed, with the comparison kept only for a `--config` that is
   given? That would let a changed default in the file go unnoticed.
2. **Ctrl-C only requests a stop.** The handler touches the stop file, and
   a second Ctrl-C does the same, so a loop with a long pass, or one that
   never asks `run.steps`/`run.stopping()`, cannot be interrupted from the
   keyboard. Question: should a second Ctrl-C interrupt at once (the attempt
   then ends `failed`, without a save)?
3. **`--json` key.** The object's key is `state` (`finished`/`stopped`), the
   field name inside `outcome.json`; the page said "outcome" before. Kept
   `state`. A failed run prints no object.

## Record unification counterexample (2026-11-13, implement)

The proposed replacement of tracking `Attempt` by launcher `RunAttempt`
assumes they describe the same execution. They do not: `track` creates a
writer for each context, while the launcher allocates a whole command.

Controlled local probe at `9a762a1`, with temporary storage and
`ETUDE_ATTEMPT=allocation-1`, `ETUDE_RUN=allocated-run`: two sequential
`with track(clocks=[Step("updates", "updates")])` blocks each logged one
row. `read` returned two distinct writers, both referencing `allocation-1`,
and two rows. A third block with `run="another-run"` recorded that other
run under the same allocation. No worker, network or rental was involved.
Command/result: `uv run python` inline probe; 2 writers / 1 allocation / 2
rows, then another run / same allocation. All temporary files were removed.

This is a cardinality issue, not just missing fields: merging by allocation
id loses writer boundaries used for row replacement; counting writers as
allocations changes retry limits and costs. Command jobs may also never open
`track`, while evaluation uses its own writers. Do not refuse these contexts
just to make the proposed record fit.

Proposed correction for review: keep `RunAttempt` for machine allocations;
replace tracking `Attempt` with `RunWriter`, explicitly the lifetime of one
tracking context, linked to its optional allocation id. Preserve historical
writer files and their first-row cutoffs. This is a distinct real concept,
not a second representation of an allocation. The single stored `RunRecord`
still needs its ownership specified: launcher `job` is an identity digest,
but observed `TrainingRun.job` is a report label that can change when another
config is observed. Do not persist that derived label as the job's identity.

Dependent record/schema work stopped for design review, as implement requires
when a counterexample breaks the data model. Nothing here changes the accepted
names or the user docs. Launched resume and experiment scoping remain unbuilt.
