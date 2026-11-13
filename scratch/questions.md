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
- Standalone local cleanup may lag remote retirement by another pass, so it
  keeps the names needed to retry deletion. Controller cleanup adds one
  conditional membership read per observed run; a changed state pointer costs
  one pointer read, one state listing and one deletion per older object.

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

`docs/training.md` (Carrying on; What is on disk) and `docs/launcher.md`
(The run promise) carry the built layout and retention table. Each save is
self-contained, only the pointer-selected save is delivered, and the pointer
is published last. Courier cleanup follows acknowledgement; the controller
owns remote deletion under upload-only grants. Skipping saves replaced before
a delivery pass is the implementation's reading of "extra uploads are not
[ok]"; identical bytes within successive delivered saves upload again.

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
   `etude.checkpoints`, with an encoded string key. `checkpoint_id` builds
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

Checkpoint implementation choices (2026-11-13, `etude/checkpoints.py`):
folder artifacts carry a `members` map of relative files to `cache.Content`;
the folder digest hashes that map's canonical JSON. Each member is an
ordinary verified `Input`; no folder archive or extra remote object. Exports
must be self-contained (no symlinks, dot paths or `.open`/`.partial` files).

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

Not documented: EXP-37's remaining points, a separate Task (`exp-56.md`,
Folded in).

## Merge of main at 66ffb28 (2026-11-13)

Merge choices and check receipts are at `9a762a1:scratch/questions.md`.
Invariants kept: first durable rows set attempt cutoffs; running hours resume
from saved state, falling back to a checkpoint; hours since start include
gaps; `RunData.reached` and `RunSummary.at` include Etude's clocks.

## `RunRequest` name taken on main (2026-11-13, realign)

Main's `afcda22` (#105) used `RunRequest` for the controller's message; the
design gives that name to launch `Run` (Jack Heart: "ok, RunRequest works").
Assumed, reversible, implemented: the controller message is `RunOperation`.

Local CLI choices (2026-11-13): an omitted `--seed` on resume uses the stored
seed; config defaults come from the current recipe, overlaid by `--config`,
then must match the stored config. An omitted `--until` keeps the previous
attempt's target. SIGINT/SIGTERM request a saved stop. Python callers can opt
in with `track(resume=True)`; launcher attempts opt in via their allocation.

Experiment-scoped reads (built): each folder's own declaration selects its
experiment. Reversible assumption: a folder with no declaration cannot claim
a declared run's experiment; raw single-run reads still support wholly
undeclared historical metric fixtures (`train/read.py::_located`).

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

Merging by allocation loses the writer boundaries row replacement uses;
counting writers as allocations changes retry limits and cost. Command jobs
may never open `track`; evaluation has its own writers. Do not refuse these
contexts to make one record fit.

**Open decision against Jack Heart's answer.** His "1a -- i think RunAttempt
is probably better than Attempt though" answered "two classes for an
attempt" by merging them under `RunAttempt`. The probe above contradicts that
merge, so the branch keeps two records: `RunWriter` (a tracking context, with
its first-row cutoff, linked to an optional allocation id) and `RunAttempt`
(the allocation). Built and reversible; nobody has accepted it. Choice left
open: (a) keep `RunWriter` beside `RunAttempt`; (b) one `RunAttempt` with
writers as a sub-part of it; (c) another name for the writer. This does not
touch the single stored `RunRecord`, which remains accepted and unbuilt.

The shared `RunRecord` must own the persisted job digest. The observed
`TrainingRun.job` is a report label derived from configs seen and can change;
keep it separate from identity. Manual resume preserves the original digest
while taking the current source, target and limits.

Launched resume choices (built): a file returning several jobs selects by run
id, or by the stored job name and seed for hand-built requests with no id.
Resume keeps its existing cohort's shared limits. A resume of a queued run
with no allocations replaces its pending specification.

Report labels use job; crosstable tabs name the job and seeded learner.
Evaluation's `Line` remains the seeded chain of continuations
(`ExperimentState.lines`); no stored lineage spelling or grouping changed.

## Package layout: run, training run, eval run (2026-11-13)

Jack Heart, in order: "ok so this is should be in run then, and run should be
top level not in training"; "and ten we can move tracking inside training";
"to be clear this is because we also have eval runs, right"; "These should be
training runs, which I think should be a subclass of `run`"; "We probably
also want to have an eval run"; then, quoting the layout below back,
"etude/eval/... EvalRun(Run): what evaluate() runs as. Adds matches, rollups,
skips". On folding the split into this Task: "do it in 56". On moving
errors, storage, records and checkpoints to the top level: "agreed".

    etude/run.py          Run: shared base. declare (run.json), delivery
                          (ship, destinations, RunFiles, StorageDestination),
                          outcome (RunState, RunCounters), listing
                          (run_folders, under, as_storage)
    etude/train/tracking  TrainingRun(Run): what track() returns; adds
                          log/log_table, steps, save/resume, checkpoint,
                          stopping(); `with ... as run` finishes it
    etude/eval/...        EvalRun(Run): what evaluate() runs as; adds
                          matches, rollups, skips. evaluate() keeps its
                          one-call shape.
    etude/errors.py, etude/storage.py, etude/records.py, etude/checkpoints.py

`etude.tracking` goes away. Layout rule: `etude.run` (and the four top-level
modules) import neither train, eval nor launch; train and eval do not import
each other. Names freed for the subclasses (proposed, not objected to): the
read-side `etude.experiment.TrainingRun` becomes `TrainingRunSummary` (or
folds into `train/report.RunSummary`); `etude.eval.match.EvalRun` (result of
`evaluate`) becomes `EvalResult`. Renamed public names keep a warning alias
until 0.5. EXP-64 is folded into EXP-56.
