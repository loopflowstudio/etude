# EXP-56 choices

Earlier save/clock implementation assumptions and receipts are retained at
`224e45c:scratch/questions.md` (Implementation assumptions). The corresponding
behavior is built and described in the user docs.

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

## Train API implementation choices

Earlier doc-drafting choices and built implementation assumptions are retained
at `18537b0:scratch/questions.md` (Train API docs). The accepted design and
current user docs now describe those APIs. Still open: whether local resume
uses current or stored config defaults, upload-only cleanup without a controller,
and the second Ctrl-C behaviour (below). The default public example and save
cadence choices remain in the design's Open list.

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
3. **`--json` key.** Superseded by concept review: the new local command
   prints `outcome`; the stored field inside `outcome.json` stays `state`.
   A failed run prints no object.

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
writers as a sub-part of it; (c) another name for the writer. The single
stored `etude.records.RunRecord` is separate and built (`dbf799e`).

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

## Remaining implementation choices (2026-11-13)

Reversible choices in the current cut: launched references are qualified
`EXPERIMENT/RUN` strings; file-based resume supplies the experiment for a bare
run name, and status/continuation take `--experiment` for that selection.
Unlabelled hand-built command jobs retain flat opaque references. New local
run folders are experiment-scoped; an existing flat declaration retains its
physical folder only when its own experiment matches.

`RunRecord.submission` is absent for standalone runs. `launch` raises when a
caller asks a standalone record for a launcher specification. Config bytes
have one owner: the specification for launched runs, the run record otherwise.
`etude.job` owns the shared execution types rather than making shared records
import the launcher. The source folder a checkpoint reader resolved is a
read-side locator. Input source URIs are unsigned and excluded from scientific
job identity; continuation keeps logical run references separate from locations.

## Eval API made consistent with training (2026-11-13)

Jack Heart: "probably worth looking at whether there are any changes to the
eval API we want to make to make it match the changes to the training API. We
don't need to do a full redo here but we should try to make it consistent
where there's no reason not to"; "Can we get the best of both worlds where we
make it match training but we also make it easy to use our Matchup APIs?";
"i guess the other thing thats different hre is that EvalConfig would be OUR
type" / "training config is YOUR type"; on the shape below: "i thik thats
probably right?" (tentative; show it at demo).

Shape:

    class ArenaConfig(BaseModel): ...          # author's config, like ElementalConfig
    def arena(config: ArenaConfig) -> None:    # author's function, like train(config)
        progress = Benchmark(name=..., environment=world, seatings=[...], rollups=[...])
        with evaluating(progress) as run:      # EvalRun(Run); experiment, storage, checkpoint from the launch
            run.play(play)                     # play(match) -> Tables; checkpoint= only outside a launch
    job = evaluation(arena, config=ArenaConfig(), checkpoint=weights)   # same shape as training(train, config=)

- One config per job, as training; several benchmarks = several jobs.
- An eval run is one tournament (Benchmark or RoundRobin): one environment,
  many seatings. A plain match list is opened from its environment.
- `run.play` replaces `hold` and `evaluate`; the run reads the experiment's
  training lines itself (no `lines=`); `rollups` move onto the Benchmark /
  RoundRobin (query-time `statsdb(rollups=)` stays).
- `run.stopping()` checked between matches; stop raises `Stopped`; a retry or
  `--resume` plays only unstored matches; resume explicit, environment and
  rollups checked, as today.
- Unchanged: Benchmark, RoundRobin, Seating, Inference, Code, relative seats,
  Match, Rollup, play(match) -> Tables, scores/standings/statsdb.
- No public EvalConfig; `jobs.Evaluation` stays internal. Defaults chosen here,
  not by Jack: opener name `evaluating`; `evaluation(progress, play=play)` kept
  as a shortcut on the same path only if it costs little, else warning alias;
  `hold`/`evaluate` warning aliases until 0.5.

### Eval consistency: planning boundary found during implementation

The new shape cannot replace the current planner unchanged. In
`experiment.advance`, `evaluation_spec(job)` extracts a tournament and its
checkpoints **before** starting the job; `played(...)` uses that definition
to choose only unstored matches and supply only their checkpoint inputs.
`evaluation(arena, config=...)` instead hides the Benchmark inside arbitrary
user code. The planner cannot discover it without executing that code during
submission. Repeated jobs with identical author config can also want new
matches as implicit training lines grow. The worker's run grant is upload-only;
its existing Fetch/input path eagerly delivers the inputs selected beforehand,
so it cannot simply read arbitrary other training lines/checkpoints at runtime.

This is a counterexample to treating the new shape as a signature-only rename,
not a rejection of the requested shape. Stop dependent deletion of
hold/evaluate and the declarative planner pending design/demo. A concrete
choice still needed: preserve pre-launch match planning through an explicit
benchmark description, or make authored evaluation jobs opaque to the planner
and define when they run again and how their selected checkpoint inputs arrive.
The latter fits the requested author-owned function better, but changes the
league's pre-launch pending-match/gap behavior and needs a runtime context with
selective input delivery. No unused runtime/context implementation is added.
The existing live EvalRun/shared Run cut can be checked independently.

## Concept review (2026-11-13): one new open name

`etude.job` (new on this branch: `Job`, `RunRequest`, `Input`, `Until`,
`Runtime`) sits beside released `etude.jobs` (`training`, `evaluation`,
`continue_from`). A job file imports from both; the docs do so seven times.
They differ by one letter, against "every word in a path ... should be
different from its neighbours". Not settled here because it is a public
import path. Options: (a) keep both; (b) move the constructors' types and
the constructors into one module, `etude.jobs` re-exporting until 0.5;
(c) another name for the shared types' module. Built: (a). Does not hold
the implement direction in `design-train-api.md`.

Standing open choices, unchanged and for the demo: `RunWriter` beside
`RunAttempt` against the "1a" answer; the eval planning boundary; cleanup
under upload-only grants with no controller; config on a local `--resume`;
a second Ctrl-C.
