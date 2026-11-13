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

The branch holds main at `66ffb28` (merge `1fae5d9`); `questions.md` records
how the two sides were reconciled.

## Delete — do not maintain

Replace these end to end:

- `etude.run` internals package -> `etude.tracking`: removed; real consumers moved.
- Launch `Run` -> `RunRequest`; authors return Jobs with launch seeds instead.
- Separate tracking `RunDeclaration`/`RunFile` and observed `TrainingRun` ->
  the single stored `RunRecord`, shared with the launcher.
- Tracking `Attempt` -> the single `RunAttempt`, shared with the launcher.
- Experiment `Checkpoint`, eval `CheckpointKey`/`CheckpointRef` and tracking
  `CheckpointRecord` -> one record and one reference; preserve identity,
  lineage, ordinals, tags, clocks, digests and relative-seat resolution.
- Eval model noun -> player; report line noun -> job.
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
Still present at `429db8d`: `Run.__exit__` (`src/etude/train/run.py`) calls
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

1. Unify/rename records; wire historical readers and outcome files. The
   tracking package move is done; RunRequest and the record unification are not.
2. Finish default recipe acceptance on the surviving records and commands.
   The registration API currently has focused Python tests, not CLI acceptance.
3. Carry folder checkpoints through tracking, launch inputs and evaluation;
   add start= and shared checkpoint enumeration.
4. Add local `etude run`, explicit local/launched --resume (with `--until`
   extending the same run, finished or not), create-only names,
   config/seed refusals and per-attempt source recording; a refused or
   interrupted attempt never marks a finished run failed (Folded in).
   `etude launch` and `etude run` accept the experiment name; the file's stem
   is only the default (design, Set up for Etude Cloud; `positioning.md`).
5. Move job seeds and examples; focused acceptance tests, then the caller's
   gate/demo steps. No publication, landing or Graph changes in implement.

## Verification

2026-11-13, after compress: `uv run --extra train pytest tests/train
tests/launch/test_state_publication.py tests/launch/test_out.py tests/run
tests/test_continuation_out.py tests/launch/test_worker_attempts.py
tests/launch/test_transfers.py tests/launch/test_measurement.py -q`: 248
passed, 6 skipped. `uv run mypy`, `uv run ruff check .`, `uv run ruff format
--check .`: clean. No full gate, publication, landing or Graph changes. The
two historical probes still describe raw mutable out carriage, not registered
state.

Counting pins moved by the state build, each with its reason beside it:

- Every run now has an `out`, so its first delivery uploads an empty
  `out.json` (`tests/train/test_store.py` 5 -> 6, `test_tables.py` 8 -> 9).
- `BucketJournal._clear_state` asks for a run's membership, conditionally, on
  every observation, whether or not the grant is upload-only:
  `tests/launch/test_transfers.py` control requests 42 -> 50. Unanswered:
  should that read happen only where the courier cannot delete?
- `etude sync --seal` loads `etude.train.state` (`tests/train/test_cli.py`
  25 -> 26).
