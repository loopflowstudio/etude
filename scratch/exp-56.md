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

Checked at `7a26dd0` (realign): none of 1-5 has started.

1. Unify records. Launch `Run` (`etude.launch.records`) is still the launched
   record; tracking still has `RunDeclaration`, `RunFile`, `Attempt`
   (`etude.tracking.records`) and `TrainingRun` (`etude.experiment`). Name
   clash: main's #105 (`afcda22`) added `RunRequest` in
   `etude.launch.controller` as the controller's record/status/cancel message,
   so the design's `Run` -> `RunRequest` rename needs that name freed. Assumed
   (`questions.md`): rename the internal controller message, keep the design's
   name. Then wire historical readers and `outcome.json` (no writer yet).
2. Default recipe acceptance through the CLI; the registration API has only
   focused Python tests.
3. Record `start=` lineage in the unified run record; the launcher Job
   already persists it.
4. Local `etude run` and explicit local/launched `--resume RUN [--until]`
   (neither exists; `etude.cli` has launch, continue, eval, report, ...),
   create-only names, config/seed refusals naming fields, per-attempt commit.
   Fix the folded bug here: `Run.__exit__` still calls `finish("failed")` for
   any non-`Stopped` error and `Attempt(first_row=True)` is written in
   `__init__`. `etude launch` and `etude run` take `--experiment`; the stem
   is the default.
5. `Job` launch `seeds` (today only `Job.runs(*seeds)`), the tiny non-Magic
   example (none exists), one-release warnings for `local_launch` and
   `track(seed=)` (only `latest_checkpoint()` warns now), then the
   `design-train-api.md` Acceptance list as focused tests. No publication,
   landing or Graph changes in implement.

## Verification

2026-11-13 compress pass. At `d3ef6bb` three things failed outside the 28
focused checkpoint tests: the layout rule (train imported eval, eval imported
train), `tests/test_page_evals.py` (fixture imported `ArtifactRef` from
`etude.eval.records`) and so its six dependants. Fixed by the move above.
Also cut: `carriage.named_file`, `RunFiles.deliverable`, and the out-recovery
selection duplicated in `BucketJournal.held` and `Courier._restore`, now
`etude.out.recovery`. `uv run --extra train pytest tests/run tests/eval
tests/train tests/test_experiment.py tests/test_page_evals.py tests/test_jobs.py
tests/test_continuation_out.py tests/test_page.py tests/test_smoke.py` plus the
launch state, out, worker, transfer, layout, planned-stop, queue and
measurement files: 451 passed, and the 10 that failed or errored (import
pins, page evals) passed once fixed. mypy, ruff check and format clean. Not run: the rest of `tests/launch`. PR #103 stays draft.

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
