# Training run lifecycle

## Rebuild on the train API (steered 2026-11-13)

`design-train-api.md` replaces the client-facing shape below; the launcher
machinery described below is kept. Everything under "Approach" about
`local_launch`, `Run.until`/`stopping` in recipes and `track(seed=)` is the
superseded surface.

User docs for the rebuild are written (2026-11-13) and describe intended
behaviour; none of it is implemented or run. Build to these files:

- `docs/training.md` (new): the Training page. `train(config)`, jobs/runs/
  seeds, `track(clocks=, config=)`, `run.steps`, `run.resume`/`run.save`
  (`extra=`, `name=`, `save(fn)`), rows, checkpoints (named objects, path,
  folder, several models, `run.checkpoints()`), `start=`/`run.input`, the
  on-disk contract, `etude run` / `--resume` / `continue`, the pieces
  underneath, a call table and an upgrade table.
- `README.md`: the new loop, `seeds=`, `etude run`, link to Training.
- `docs/tracking.md`: the "Out, a stop signal and running again" section is
  removed (moved to Training); taken names refused; `EXPERIMENT/RUN/` layout
  with `outcome.json`; `etude.tracking`; checkpoints section rewritten.
- `docs/launcher.md`: run = job + seed, `seeds=`, `runs()` returns jobs,
  `RunRequest` underneath, `launch --resume`, taken ids refused before
  renting, `run.input`, `start=`, `PREFIX/EXPERIMENT/RUN/` layout, `run` as
  a command kind.
- `docs/evaluation.md`: "player" wording, `.player(...)`, folder checkpoints
  handed to playing code.

Open choices and the assumptions those docs take are in `questions.md`
("Train API docs"). Docs were edited on this branch's pre-v0.3.1 text: main
(`77eb5f9`) has since changed `docs/evaluation.md` heavily (Seating,
relative seats) and `docs/tracking.md` (Etude's clocks, `Until`), so merging
main will conflict in the edited passages of those two files and in
`docs/launcher.md`'s capacity example; a dry-run merge also conflicts in 15
source and test files. Not touched: `AGENTS.md` (still says `etude.run`),
`.lf/skills/qa.md`, `RELEASE_NOTES.md`.

Jack Heart, 2026-11-12: "aproach should be to write a prototype of manabot using
the ideal etude api, then build etud API to match, then land the diff". EXP-56
is the Etude step. His target-only continuation decision and demo choices are
in `questions.md`.

## Approach

The recipe uses `local_launch(out=, experiment=)`, `Run.out`,
`Run.until(clock, default=)`, `Run.stopping()` and `Stopped` (exit 75).
Calling it again resumes saved state. `jobs.py:continue_from(parent, until,
source=, ordinal=None, config=None)` creates a child; the parent stays unchanged.

- `train/run.py`: first durable rows set new attempt cutoffs; historical
  checkpoint cutoffs survive. Launch targets override recipe defaults without
  changing config. Local records live in `DIR/etude/BASENAME(DIR)/`; launched
  out is `RUN/out/`.
- `launch/job_file.py:read_job_file`: every unnamed job gets the file stem as
  its experiment. Plain files preserve explicit `Job.experiment`; files with
  `eval`/`report` still reject conflicting identities. Recipes name none;
  direct Python submissions supply the experiment on their Job.
- `launch/worker/attempts.py`: only training's exit 75 is a planned stop.
  Cancellation and exhausted budgets end the run; other planned stops retry
  within `max_attempts` (default three, including planned stops).
- `out.py`, `carriage.py`, `run/delivery.py`, `launch/worker/courier.py`:
  fingerprinted membership follows file delivery, including empty membership.
  Recovery tolerates newer mutable saves but verifies checkpoint digests;
  continuation freezes published fingerprints. Complete relays alone reconcile
  stale out. Saves are atomic, per file; sync pins an inode with a temporary
  hard link. Retained checkpoint bytes never imply out membership.
- `jobs.py:continue_from`: populated out uses `RunData.reached` plus checkpoint
  clocks without rows; empty out falls back only to its latest copied checkpoint.
  With neither, refuse before queueing. Explicit ordinals read no out.
  `ParentRun.at` owns lineage independently of its optional checkpoint.
  Bucket delivery seeds the child before its first claim; unclaimed retries
  keep initial inputs, claimed retries use child state.

## Delete — do not maintain

No remaining Etude deletion target. The resume-point API, checkpoint-only
lineage, names-only membership, alternate working-file path and test-only
delivery methods are gone. Membership owns out-file deletion;
`carriage.retired` handles tracking only. Manabot's stub deletion is MTG-329.

At `c45858f`, `Scheduler._work` selects the child's inputs and
restoration once; `_relay` consumes that `Work` instead of reading the holder
again and repeating the selection. Both carriers use `carriage.listing`'s
existing key order for membership, removing redundant sorts. No API, schema,
dependency or product-choice change.

The near-limit retry follow-up is also complete at `c45858f`:
`docs/launcher.md` explains the remaining-allowance rental, and
`tests/launch/test_planned_stop.py:test_stopped_near_hours_limit_rents_again_for_the_remaining_time`
pins two attempts, two rentals, a 40-second retry allowance and final
`out_of_time`. The scheduler still permits an early planned stop to retry
while attempts and budgets remain; repeated stops can exhaust attempts
instead. Choice 8 remains a headless assumption for demo, not a settled
product decision. No further implementation change is identified here.

## Remaining work and limits

1. Full gate on `6fb164b`, including the same-machine restoration repair
   (`b738069`) and plain training-file experiment repair (`b6482b7`):
   outside-network-denied repository tests, full mypy, Ruff lint/format
   including Markdown. Historical readers, cross-host recovery, cancellation
   and budgets remain required. QA finding 2 is repaired and has bounded CLI
   evidence below; no further implementation gap is identified in this pass.
2. Demo layout, reference checkpoints, `Stopped`/75 and `local_launch` choices
   under "no opinion on the other stuff". Cancellation sends TERM beside the
   stop file; polling alone cannot guarantee a save. Near-hours-limit retries
   keep current behavior (choice 8), bounded by attempts and budgets.
3. Manabot run, immediate rerun, stop and resume passed on a copy during QA
   (evidence below and in `qa.html`); its original checkout remains untouched.
   Manabot under `etude launch` remains unverified; the plain-file fixture
   verifies Etude's launch path, not that integration. Cutover and the belief
   cache choice are MTG-329; see `questions.md`. That choice does not block
   Etude's gate. Pre-existing owned-host liveness remains outside this repair.

Known limits: populated out with metrics past its copied checkpoint validates
against the later position even when a recipe resumes from the checkpoint;
`--ordinal` chooses that earlier position. A local out checkpoint has an
absolute `file://` address and no portable key; renaming its directory starts
a new record. `ETUDE_OUT` (run) and `ETUDE_OUTPUT_DIR` (attempt) are easily
confused; renaming the older variable is outside this Task.

## Recorded evidence (2026-11-12)

Rechecked at `c45858f`: `git ls-remote origin refs/heads/main` returned
`e2f1dba9ab4f98fb432430e67b88120710b77964`, already merged here. Task status
had no newer direction than the target-only choice; `lf context` reported no
Wave. Manabot remained
clean at `f47e2621d48c2bc8b58324dbbddf53f12cfa3da4`: stub imports, independent
`Stopped`, config-bound loops and cached belief outputs (`questions.md`).
`lf task status MTG-329 --json` returned "this Task is not registered in
Loopflow"; delivery state is unknown. Source/status observations, not acceptance.

Bounded probes, all outside-network-denied unless noted:

- `fad2584`: write `out/final.pt`, carry, unlink, carry, then restore through
  `BucketJournal.held`/`put_back`: `local_exists=False, bucket_exists=True,
  restored_exists=True`. Empty relay into a reused directory left
  `stale_endpoint_exists=True`. Fixed at `bd4a687`; ordinary unlink had been
  missed by earlier lifecycle checks.
- At that repair, a bounded 1,000-run queue fixture returned 1,000 candidates
  in one query (0.0085 s observed). EXPLAIN used runs_pool, runs_rent and
  attempts_run, with a temporary B-tree for final ordering.
- Target-only fixture: 10 passed, 10 deselected in 6.65 s. Stopped once,
  retried to target 3 with config target 2, reached 4 via Python and 6 via
  CLI `--until` alone. Parent stayed at 3; child configs matched the parent.
  No training or paid service.
- `96d5fb9`: original worker continuation failed with "the target steps=2 is
  not beyond the parent's steps=2"; SSH continuation printed only
  `trained to 7\n` instead of `trained to 3\ntrained to 7\n`. Both used
  `latest_checkpoint()` with empty out. Fixed at `528e290`.
- `528e290`: copied checkpoint at 2 plus `out/samples.txt` yielded
  `at={'updates': 2.0} checkpoint=None input=None`, with only samples as
  input. Fixed at `5f76a7a`: carry the latest copied checkpoint alongside out.
- `5f76a7a`: out named, one row at updates=5, nothing saved; continuation to
  9 queued locally and after sync with `at={'updates': 5.0}, checkpoint=None,
  inputs=()`. Fixed at `05b480f`: refuse empty unseeded children, without
  implicitly selecting older checkpoints.
- `05b480f`: fake bench with 600 allowed seconds, planned stop at 560,
  ticks every 200: two attempts, first `stopped` with limit 600, second
  `out_of_time` with limit 40; final USD 0.0983... . The existing hours test
  consumed all 600 seconds and missed this. Repeated early stops can exhaust
  attempts instead. The launcher cannot distinguish a stop for the run's own
  limit from one for a machine window. Choice 8 keeps that behavior.

Focused verification before this compression:

| Change | Network-denied result | Other checks |
| --- | --- | --- |
| `528e290` checkpoint fallback | 41 passed in 51.22 s: continuation, original worker/SSH, out, lifecycle, carriage | Five-file mypy; repository Ruff; diff; wheel |
| `5f76a7a` checkpoint alongside out | 25 passed in 30.48 s: continuation, out, carriage | Five-file mypy; repository Ruff; diff; wheel |
| `05b480f` refuse empty children | 31 passed in 11.54 s: continuation, out, carriage | Three-file mypy; repository Ruff; diff; wheel |
| Near-limit retry | 1 passed, 8 deselected in 7.82 s | Focused mypy/Ruff; docs format; diff |

Continuation checks retained two listings, one membership read when carried,
and zero out-byte reads. Near-limit regression pins two attempts, two rentals,
a 40-second retry allowance and final `out_of_time` for that fixture only.
Earlier detailed checks and curation locations remain in the pre-edit plan,
moved before this curation to
`/var/folders/m6/r3tllnrs1yq7yfbwm680tss40000gn/T/etude-exp56-compress-b7vgzls0/exp-56.md`.

Compression verification:

`uv run python scripts/test_network.py uv run pytest tests/launch/test_planned_stop.py tests/launch/test_out.py tests/train/test_lifecycle.py tests/test_continuation_out.py tests/launch/test_scheduler.py::test_a_retry_on_another_owned_host_waits_for_and_continues_the_runs_folder -q`
— **54 passed in 63.85 s**. Four-file mypy, repository Ruff lint/format
(193 files), `git diff --check` and `uv build --wheel` passed. Maintainer
review found no further deletion target: first/unclaimed/claimed attempts
retain their input selection, and filtering the ordered listing preserves
membership order. Full gate remains open; subsequent local manabot acceptance
is recorded below.

Concept review at `c45858f` (headless, source and docs diff only, nothing run):
recipe surface (`track`, `local_launch`, `Run.out/until/stopping`, `Stopped`),
attempt outcomes and continuation match this design and `docs/tracking.md` /
`docs/launcher.md`. No next implement direction. Recorded in `questions.md`:
the `Stopped` import path. Choice 8 is still unanswered by a person; Jack's
"no opinion on the other stuff" predates it.

## QA through the real CLI (2026-11-12, report in `qa.html`)

- Planned-stop retry on the same owned machine after its disk was wiped
  (box replaced at the same address, pool off between attempts): attempt 2
  logged `start at 0` although the bucket held `out/state.json` at 12. Cause:
  `Work.resumes` was narrowed to other machines (`fad2584`). Fixed:
  `resumes` is any retry again, `Work.here` marks the same machine, and the
  courier skips restoring only when `here` and the run folder exists. Rerun:
  `start at 12`. A same-machine retry with its folder kept issued no
  run-folder reads from the worker.
- `test_a_pods_checkpoint_reaches_an_owned_host_without_its_storage_from_the_bucket`
  failed at `c45858f` too (expected restore set lacked the `.out` marker);
  test updated.
- `--config` non-object input now says `expected a JSON object, such as
  {"rate":0.001}` instead of a Pydantic dump.
- Manabot acceptance on a copy (`/tmp/qa56/manabot`, original untouched):
  belief run 85 s, immediate rerun 13 s with identical bytes, stop request
  at update 2 (exit 1, state `stopped`), resume from 3 to 4 with rows 1–4.
- Pre-existing, outside this Task: an owned host destroyed mid-attempt
  stays `running` (no deadline, no liveness timeout); cancel leaves `stopping`.
- Four import-count pins failed at `c45858f` (+`etude.out`, +`etude.stopping`
  versus main); re-pinned. Network-denied `tests/launch tests/run tests/train
  tests/test_continuation_out.py tests/test_jobs.py`: 653 passed, 4 failed
  (those pins, since passing); full mypy and Ruff pass. Other test folders not
  rerun in this pass.

## Plain training-file experiment repair (2026-11-12)

QA finding 2: `read_job_file` supplied no experiment for a file defining only
`runs()`, so a recipe naming none failed with `experiment_not_named` on the
worker. Default unnamed jobs to the file stem, using the existing naming
convention; retain explicit job identities and experiment-file conflicts.
No new API, persistence, query or deletion target. Recipe and worker paths
remain shared. README and tracking/launcher docs now show recipes naming none.

`uv run python scripts/test_network.py uv run pytest tests/test_jobs.py::test_a_plain_training_file_supplies_the_recipes_experiment tests/launch/test_cli.py::test_an_experiment_file_launches_runs_of_its_experiment tests/launch/test_cli.py::test_a_job_file_launches_a_cohort_and_each_verb_reads_it_back -q`
— **4 passed in 35.37 s**. The bounded typed CLI fixture records one row,
checkpoint and out file, with either the default or an explicit experiment.
Existing conflict rejection and cohort continuation pass. Focused five-file
mypy, Ruff lint/format including affected Markdown, `git diff --check`, and
`uv build --wheel` pass. No training load or paid service. Maintainer review:
one fallback at file submission, no recipe inference or report-discovery
change; the legacy cohort fixture now states its existing identity on its Job.
Full repository checks remain gate's work.

## Compression after the plain-file repair (2026-11-12)

Full branch review at `b6482b7` found no remaining obsolete path to delete.
`read_job_file` now uses one fallback, `Job.experiment or path.stem`: its
preceding conflict check already guarantees an experiment file's explicit
identity matches that stem. QA guidance now exercises the repaired plain-file
path instead of describing `experiment_not_named` as current behavior.
The restoration branches retain distinct required cases: empty membership,
newer local saves, explicit checkpoint inputs and unclaimed child retries.
No API choice or target-only continuation direction changed.

The network-denied pytest command immediately above: **4 passed in 35.95 s**.
`uv run mypy src/etude/launch/job_file.py`, focused Ruff lint/format,
`git diff --check` and `uv build --wheel` passed. `lf context` remained below
both scratch limits; no curation was required. Full gate remains pending.

Realignment at `6fb164b`: origin main is still `e2f1dba9`, already merged;
EXP-56 has no newer direction than the target-only choice and no Wave.
`read_job_file`, `track` and the default/explicit-identity fixture agree with
the repair and affected docs. Reused the four passing CLI checks above;
no code changed or tests reran. Manabot remains at `f47e2621d` with unrelated
working edits; the inspected recipes still import the stub and use
`config.updates`, and the loop still defines its own `Stopped`. Its cutover
and belief-cache decision remain separate; the original checkout was not edited.
`lf task status MTG-329 --json` again reports that the Task is not registered
in Loopflow, so its delivery state remains unknown.

Concept review at `6fb164b` (headless, source and docs diff since `c45858f`,
nothing run): `Work.here` beside `resumes` and the restored-when-gone rule
match `docs/launcher.md`; no next implement direction. New open choice 9 in
`questions.md`: the stem default refuses main's documented plain-file script
that names its own experiment. Built behaviour kept as the headless
assumption. Choices 8 and 9 are unanswered by a person.

## QA second pass at `6fb164b` (2026-11-12, `qa.html` section 7)

Real CLI, local pool, `/tmp/qa56/lab3`: a plain `job.py` stopped at 8 (75), resumed
to 20, recorded experiment `job`. Explicit `Job.experiment` kept `capacity-pilot`.
Main's README pattern (`track(experiment="capacity-pilot")` launched as `readme.py`)
failed in one attempt with `experiment_differs_from_launch` (smoke caught it first).
The same lab on main succeeded under `capacity-pilot`, so choice 9 is now confirmed
by a run. Message now names the remedy (`381b481`). Branch CLI on main's
`ETUDE_HOME` refuses launcher.db (schema 16); main-written run folders read the same.

## Publication gate (2026-11-13)

`lf sync` merged cleanly; origin/main remained `e2f1dba9` immediately before
publication. Reviewed the complete branch diff. Added upgrade guidance for
plain-file experiment identities, removal of `resumed_at`, and schema 16 at
`5c76712`. The controller CLI fixture also needed its existing `capacity`
experiment on the Job; fixed at `105abdd`.

Initial network-denied full run was interrupted after 192 passes, four failures
and three setup errors: controller 15-second startup/CLI deadlines, a 10-second
cohort wait, and a cleanup process not printing within its 0.2-second allowance.
Log: `/tmp/exp56-publish-pytest.log`. No allowance was changed or retry added.
After the fixture repair, controller and hooks: **19 passed in 67.22 s**.
Final network-denied full suite at `105abdd`: **793 passed, 6 skipped,
3 deselected, 1 warning in 996.09 s**. Skips are opt-in measurements; deselected
cases are scale workloads. The warning reports a dropped row in the existing
long-run storage fixture. Log: `/tmp/exp56-publish-final-pytest.log`.
Full mypy (162 source files), repository Ruff lint/format (193 files),
`git diff --check`, and wheel build passed. This closes the earlier full-gate
item. Manabot launch integration remains unverified and separate from cutover.

Published https://github.com/loopflowstudio/etude/pull/103 at `105abdd`.
The publish command returned open, but the subsequent GitHub read showed draft.
A second publish was started to mark it ready, then stopped (exit 130) on the
new direction before publication; its PR-copy child also exited.
Steer `1b36d08e-a6b4-49e1-b324-5986174db582` reports Jack Heart's words:
"yes, draft pr"; "#103 was converted to draft; keep it draft, do not mark it ready."
Keep the PR draft. No auto-merge was armed and the Task was not completed.
