# Training API rebuild (EXP-56)

## Outcome and authority

Build `design-train-api.md` and the edited user docs, preserving existing
output carriage, planned-stop retries, row replacement and continuation.
`docs/training.md` is the intended experience, not a description of current code.
PR #103 stays draft. Jack Heart's recorded steer: "#103 was converted to
draft; keep it draft, do not mark it ready."

The complete earlier design, decisions, probes and QA history are in git:
`1666be1:scratch/exp-56.md` and its siblings `positioning.md` (every dated
remark), `ontology.md` (the naming exploration) and `etude-readme-draft.md`
(league-led README; its local_launch/until recipe surface is superseded). The
earlier 793-pass gate covers `105abdd`, not the unbuilt API.

Later work: EXP-57 live view; EXP-58 machines/metrics; EXP-59 broader JSON
commands; EXP-60 evaluation expansion; EXP-61 joinable shared budgets.

## State publication

Unblocked 2026-11-13. Each save is a self-contained folder with the pointer
published last; delivery sends only the save the pointer names; Etude removes
replaced saves, the controller doing it under upload-only grants.
`questions.md` (State publication) has Jack Heart's words, the three choices
he left unanswered and one awkward case. Publication and registration are now
implemented; the full train API is still incomplete (remaining work below).

## Docs for the build

The intended experience, to build against and keep true:

- `docs/training.md`: the whole train API; Carrying on and What is on disk
  carry the save layout; Games in flight; `--resume RUN --until`;
  `--experiment`.
- `docs/launcher.md`: The run promise carries delivery order of a save and
  its pointer and the retention table (who removes what, what lingers);
  also restoration, the storage layout, `--resume --until`, `--experiment`.
- `docs/tracking.md`: Etude's clocks across a resume, the run folder.
- `docs/evaluation.md`: players, folder checkpoints, on main's seatings and
  relative seats.

`docs/training.md` Running it now states the built local rules: Ctrl-C and
SIGTERM as saved stops, the `--json` object, the config a `--resume` must
repeat, removal in 0.5. Three questions it raised are in `questions.md`
(Found reading the docs as a user).

The branch holds main at `286e0c5` (v0.3.2, merge `429db8d`; launcher
database version 18, so its file is `launcher-18.db` under main's per-version
naming); `questions.md` records how the `66ffb28` merge was reconciled.

## Delete — do not maintain

Replace these end to end:

- `etude.run` internals package -> `etude.tracking`: removed; real consumers moved.
- Launch `Run` -> `RunRequest`; authors return Jobs with launch seeds instead.
- Separate tracking `RunDeclaration`/`RunFile` and observed `TrainingRun` ->
  the single stored `RunRecord`, shared with the launcher.
- Tracking `Attempt` -> the single `RunAttempt`, shared with the launcher.
- Experiment `Checkpoint` and eval query's `_StoredCheckpoint`/`_StoredArtifact`
  are removed. One `CheckpointRecord` and one `CheckpointRef` live in
  `etude.tracking.checkpoints`, which training and evaluation both import
  (`tests/run/test_layout.py` forbids either importing the other); keys are
  strings (old structured keys still load).
- Eval `Model` -> `CheckpointPlayer`, keeping the `Player` union and stored
  kind; `.player(...)` replaces `.model(...)`. Report line -> job is still open.
- Root `state.json` writer -> `outcome.json`; retain old outcome readers.
- Implicit resume on taken names -> create-only identity and explicit resume.
- `local_launch`, `track(seed=)` and `latest_checkpoint()` leave the taught
  API; the design requires one-release warnings for published spellings.

Preserve stored runs/evaluations, old file/field readers, progress, stop exit
75, cancellation, cross-machine restoration and hours/dollars/attempt limits.
Do not repair exclusive tests of deleted APIs; move behavior coverage to the
surviving API. Manabot cutover is MTG-329; do not edit its checkout here.

## Folded in, 2026-11-13 (Task comment, Jack Heart)

**Finished run marked failed.** "This should be part of 56". Manabot QA
against `105abdd`: a second `track` block on a finished run that raises
before any row (refused resume, KeyboardInterrupt) turns the run `failed`.
Still present at `f410793`: `Run.__exit__` (`src/etude/train/run.py`) calls
`finish("failed")` for any non-`Stopped` error, and the attempt is recorded
in `__init__` with `first_row=True`, which is no evidence of a logged row.
The fix must keep a genuine failure before the first row reportable, so
skipping every rowless exception is not enough. Planned with explicit resume.

**EXP-37** "Fold this one into 56"; EXP-37 is closed as folded. What this
plan answers of its brief, and what it does not:

- Application declares restorable state and discontinuity: answered by the
  design (only registered objects restore; streams/games in flight restart).
- Durable checkpoint publication: the save pointer published last (step 1)
  gives whole-save recovery; *acknowledged* durability is not specified.
- One cumulative budget: hours, dollars and attempts span attempts, but
  hours "count code execution" (`docs/launcher.md`); transfer, idle and
  teardown time count only through dollars. Not planned.
- Fault fixtures: step 1 covers interrupted saves; loss before/after commit,
  partial upload, duplicate completion and controller restart are not planned.
- Recording acquisition mode and telling preemption, host loss, lease expiry
  and learner failure apart: not planned.
- Manabot adoption: MTG-329.

Jack Heart on the unplanned EXP-37 points: "followup task is fine". They do
not hold this release and are a separate Task.

## Remaining implementation

Built: pointer-last publication of the selected save, both carriers, recovery
independent of stale membership, delayed local/remote cleanup and controller
cleanup under upload-only grants. Registration, PyTorch serialization, extra
counts, callback saves, named state, step cadence, stop propagation, seed/input
access and stepped row clocks are wired into Run. Both hour clocks survive a
relay without checkpoints. `etude.run` is removed in favor of `etude.tracking`.
Folder checkpoints now publish named objects or callback exports, share records
with evaluation/query, list through `run.checkpoints()`, and travel as verified
member inputs through `start=`, evaluation and explicit checkpoint continuation.
The query cache rebuilds at schema 14 so folder keys join their seated players.

Built in this continuation: local `etude run` and explicit local resume with
saved SIGINT/SIGTERM stops, target extension/retention, config and seed checks
before the recipe, create-only local names and per-attempt commits. Refused
local attempts preserve a finished outcome; genuine rowless failures remain
reportable. First-row cutoffs are now marked only after a durable row.
`outcome.json` writers retain historical `state.json` readers. Jobs accept
launch `seeds`, job files expand jobs to named `RunRequest`s, and launch/queue
accept `--experiment`. `training(start=)` passes the checkpoint key into the
tracked declaration. Published spellings warn for removal in 0.5.
`examples/coin.py` is the tiny non-Magic recipe.

Remaining, before the accepted API can be treated as built:

1. Unify records end to end. Launcher `RunRecord`, tracking
   `RunDeclaration`/`RunFile` and observed `TrainingRun` remain separate;
   launcher `RunAttempt` and tracking `Attempt` remain separate. The
   controller message is now `RunOperation`, freeing public `RunRequest`.
   Preserve historical file/field readers during the cut. Do not extend the
   separate records as a final API.
   **Design review needed before this cut:** the local probe in
   `questions.md` (Record unification counterexample) found two tracking
   writers and multiple tracked run names sharing one launcher allocation.
   Tracking `Attempt` is a writer lifetime, not a machine allocation.
   Review the proposed `RunWriter` distinction and job identity versus
   derived report label before choosing the shared schema. No production
   record changes were made in this pass.
2. Launched `etude launch job.py --resume RUN [--until]`, keeping cumulative
   limits and attempts, current source and the same out. No such CLI or
   scheduler operation exists yet. Names/storage remain globally scoped in
   the launcher; implement experiment-scoped identity and `PREFIX/EXPERIMENT/RUN`
   carriage/grants (local CLI already nests by experiment). Ensure refused
   launched attempts preserve a finished run too.
3. Report line -> job. Local CLI acceptance now includes two-model save,
   stop, resume, checkpoint publication and evaluation seating in one test.
   No publication, landing or Graph changes here.

## Verification

2026-11-13 implement continuation: `uv run --extra train pytest tests/train/test_local.py -q`:
8 passed. Focused consumers: 121 passed; launcher/experiment files: 141 passed
and their three failures passed after explicit-resume fixes; page/hour/lineage
rerun: 22 passed; tables, read batches, measurement, page, continuation and
tournament: 82 passed, 6 skipped. Mypy and Ruff passed. Full gate remains later.
Compress pass after it (resume checks in `etude.local._resumed`, one outcome-name lookup
in `read`, plain job expansion; `ETUDE_SEED` unset for a seedless historical run):
`uv run --extra train pytest tests/train/test_local.py tests/test_jobs.py
tests/launch/test_cli.py tests/train/test_lifecycle.py tests/train/test_track.py -q`:
73 passed; mypy and Ruff clean. Second compress pass (`track` docstring names
`etude run` and `Run.checkpoints`; one `ExperimentOption` in `etude.cli`):
`uv run --extra train pytest tests/train/test_local.py tests/launch/test_cli.py
tests/train/test_cli.py -q`: 30 passed. Earlier checkpoint/carriage checks and the import-boundary
repair are recorded at `dda6b17:scratch/exp-56.md` (451 passed, type/lint clean).

Counting pins moved by the state build, each with its reason beside it:

- Every run now has an `out`, so its first delivery uploads an empty
  `out.json` (`tests/train/test_store.py` 5 -> 6, `test_tables.py` 8 -> 9).
- `BucketJournal._clear_state` asks for a run's membership, conditionally, on
  every observation, whether or not the grant is upload-only:
  `tests/launch/test_transfers.py` control requests 42 -> 50. Unanswered:
  should that read happen only where the courier cannot delete?
- `etude sync --seal` loads `etude.train.state` and
  `etude.tracking.checkpoints` (`tests/train/test_cli.py` 25 -> 27); a report
  loads the latter too (30 -> 31).
