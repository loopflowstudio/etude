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

`docs/training.md` Running it states the built local rules; three questions
it raised are in `questions.md` (Found reading the docs as a user).

The branch holds main at `286e0c5` (v0.3.2, merge `429db8d`; launcher
database version 18 on main; this Task now uses version 19 under the same
per-version naming); `questions.md` records how the `66ffb28` merge was reconciled.

## Delete — do not maintain

Replace these end to end:

- `etude.tracking` goes away (`questions.md`, Package layout). Done: `errors`,
  `storage`, `records`, `checkpoints` are top-level modules. Remaining:
  `declaration`, `delivery`, `listing` (with `LocatedRun`/`located_runs`) ->
  `etude/run.py` under a shared `Run` base; `train/run.py` ->
  `etude/train/tracking` with `TrainingRun(Run)`; `EvalRun(Run)` in eval.
  `block_file` has no stated home. `etude.experiment.TrainingRun` ->
  `TrainingRunSummary`, `eval.match.EvalRun` -> `EvalResult`, warning aliases
  until 0.5. Then drop `tracking` from `tests/run/test_layout.py` `SHARED`,
  `tests/launch/test_layout.py` and AGENTS.md's cache paragraph.
- Launch `Run` -> `RunRequest`; authors return Jobs with launch seeds instead.
- Separate tracking `RunDeclaration`/`RunFile` and observed `TrainingRun` ->
  the single stored `RunRecord`, shared with the launcher.
- Tracking `Attempt` -> `RunWriter` (built, decision open); `RunAttempt`
  stays the machine allocation.
- Experiment `Checkpoint` and eval query's `_StoredCheckpoint`/`_StoredArtifact`
  are removed. One `CheckpointRecord` and one `CheckpointRef` live in
  `etude.checkpoints`, which training and evaluation both import
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

Earlier implementation receipts and built behavior are recorded at
`f89ae54:scratch/exp-56.md`; the code and current docs remain the reference.

Remaining, before the accepted API can be treated as built:

0. The package layout in the Delete list (shared `Run` base, `etude/run.py`).
1. Unify records end to end. Launcher `RunRecord`, tracking
   `RunDeclaration`/`RunFile` and observed `TrainingRun` remain separate.
   Preserve historical file/field readers during the cut; do not extend the
   separate records as a final API. Built: `RunWriter` (tracking context,
   first-row cutoff) beside `RunAttempt` (allocation). This departs from
   Jack Heart's "1a"; the choice is open in `questions.md` (Record
   unification). The `RunRecord` cut keeps the job digest as identity and
   `TrainingRun.job` as a derived report label.
2. Experiment-scoped identity and `PREFIX/EXPERIMENT/RUN` carriage/grants in
   the launcher: names, storage and grants are still globally scoped there,
   and its status fallback, continuation and legacy conversion need
   experiment selection. Built: the local CLI nests by experiment; training
   reads, evaluation queries, listing and report refresh select by each
   folder's declaration, joining flat and machine-nested historical copies;
   launched explicit resume. Launcher schema is 19, edited in place for this
   unreleased Task.
3. Built: report labels use job; crosstables still separate each seeded learner
   and continuation chain. Internal `Line` and its stored spellings retain
   lineage semantics. Local CLI acceptance includes two-model save, stop,
   resume, checkpoint publication and evaluation seating in one test.
4. The brief's acceptance check (manabot checkout with `proposed_etude`
   imports swapped to `etude.train.run`) cannot pass as written: the design
   drops `local_launch`/`run.out`-owned `state.pt` for `etude run` and
   `run.resume`. Design Order of work moves that check to MTG-329 on an
   SDK override. Undecided: whether EXP-56 reports manabot's required
   changes against a probe of its recipe, or leaves it wholly to MTG-329.

## Verification

Compress, 2026-11-13, after moving the four shared modules and folding the
reader's folder selection into `train/read.py::_located`: `uv run pytest
tests/run tests/train tests/eval tests/launch/test_layout.py
tests/launch/test_measurement.py` plus the page, experiment, jobs, smoke and
continuation files: **340 passed**, 6 skipped; the 5 start-up import pins
that failed passed on a focused rerun after dropping by one each (the
`etude.tracking` package no longer loads for `EtudeError`). `uv run mypy`, `uv run ruff check .`, `uv run ruff format
--check .`: clean. `node --test tests/test_charts.cjs` passed 21 before this
step and was not rerun (no JS edit here). Full gate and remaining acceptance
checks are pending; the accepted API is not complete.

A bounded 4,096-row DuckDB fixture's `EXPLAIN ANALYZE` used index scans for
both the batched declaration lookup (two folders; 0.0015 s) and the keyed
run-kind lookup (one row; 0.0002 s): fixture costs, not a production latency
estimate. Reopening either same-named experiment reads zero source objects.

Previous checks and counting-pin changes remain at
`f89ae54:scratch/exp-56.md`. PR #103 remains draft.
