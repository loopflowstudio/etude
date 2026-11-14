# EXP-56 choices

Earlier save/clock implementation assumptions and receipts are retained at
`224e45c:scratch/questions.md` (Implementation assumptions). The corresponding
behavior is built and described in the user docs.

The accepted rebuild is `design-train-api.md`; `docs/training.md` describes its
intended experience. Earlier local_launch/Run.until choices and QA history are
superseded as API direction, retained in git at `1666be1:scratch/questions.md`.
They remain evidence about the existing carriage and retry machinery.

## State publication (2026-11-13)

An interrupted mutable-file delivery reproduced a mixed save: model=save-2,
optimizer=save-1, state.json=save-1, membership unchanged. Command:
`uv run python scratch/state-publication.py`. Full evidence and exact answers
are retained at `6f70baa:scratch/questions.md` (State publication).

Jack Heart: "snapshot directory + pointer published last, recovery follows
the pointer"; "local storage probably fine, but ideally avoid remote extra
storage". On self-contained saves: "1b is fine, my other feedback was that
extra stuff locally we can cleanup automatically is ok, but extra uploads
are not". On controller deletion under upload-only grants: "2a is the best
of thse options". On local retention: "i dont think it should need to save
if its confirmed published online, but i also think we can have some grace
in our cleanups". Confirmation/grace: "dunno, dunno". Running hours unanswered.

Built assumptions, reversible: confirmed means storage acknowledged the
selected `current.json` or a later one; grace is the following delivery pass,
not a timer; running hours resume from the saved state. Self-contained saves
repeat unchanged bytes on delivery, but saves superseded before delivery are
skipped. Courier owns local acknowledgement/retention; controller removes old
remote saves under upload-only grants. The docs show this behavior.

Open: without a selected controller, upload-only remote cleanup runs only
while a CLI command does. At five-minute saves, twelve replaced saves/hour
remain until then. Accept, warn, or reduce delivery cadence?

## Train API implementation choices

Earlier doc-drafting choices and built implementation assumptions are retained
at `18537b0:scratch/questions.md` (Train API docs). The accepted design and
current user docs now describe those APIs. Still open: whether local resume
uses current or stored config defaults, upload-only cleanup without a controller,
and the second Ctrl-C behaviour (below). The default public example and save
cadence choices remain in the design's Open list.

Merge history and preserved clock/cutoff invariants: `394aa37:scratch/questions.md`.

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

Built layout and record ownership are in `exp-56.md` and the design.
The full built package mapping and implementation choices are retained at
`ef6099a:scratch/questions.md`. The enforced boundary remains: shared run,
records, errors, storage and checkpoints import neither train, eval nor
launch; train and eval do not import each other. Public warning aliases end
in 0.5. Unlabelled command jobs retain flat addresses; historical flat run
folders remain selected only by a matching declaration.

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

Remaining choices: upload-only cleanup without a controller, local resume
config and second Ctrl-C. Later steers below supersede the writer and eval
planning choices; their implementation remains to be built.

## Concept review, second and third passes (2026-11-13)

The second pass's local stop/resume, outcome and job-name corrections and
third pass's qualified resume and launched-only branch docs are built.
Exact CLI output at `df734c3` and the writer/attempt vocabulary evidence are
retained at `6f70baa:scratch/questions.md` (second and third passes).

**Open: branching a run started with `etude run`.** The Training page puts
`etude continue` in one table with the local commands, and it is the only
way to change a config on purpose, but it reads the launcher database and
queues a launched run. A run started here has no branch: a changed config is
a fresh run from update 1. Options: (a) say so, `etude continue` is for
launched runs (the docs correction now directed); (b) `etude continue` also
finds a run in the local runs directory and queues its child on this
computer; (c) `etude run train.py --from RUN --config ...` starts a child
here from a copy of the parent's state. Related: should `--until` stay
required on `etude continue` when only `--config` changes? Matters because
"try another learning rate from update 400" is the first thing someone does
after a local run, and the launcher path costs a job file. Does not hold the
implement direction.

Evidence for EXP-59, not a choice here: with `--json`, every refusal and a
failed `train` print no object and exit 1 (taken id, bad config, not found,
already finished, config changed, recipe exception); the code is in the
stderr line only. `outcome.json` does record `failed`.


## Concept review, fourth pass (2026-11-13): one open choice widened, one new name

Three corrections the design implies are in `design-train-api.md` (fourth
concept review). What the runs showed at `cbdefd7`, temporary `ETUDE_HOME`,
no network, `examples/coin.py` unless said:

    run --until updates=10                              coin-seed-0            exit 0
    run --name slow --seconds 0.001 --json              outcome "stopped"      exit 75
    run --resume slow-seed-0 --json                     outcome "finished"     exit 0
    run --resume coin-seed-0                            etude: run_already_finished: give --until with a later target   exit 1
    run --resume coin-seed-0 --until updates=20         coin-seed-0            exit 0
    recipe raising at update 3 (saves every step)       traceback; outcome.json failed; current.json save-00000003
    run --resume of it, after the fix                   finished; writers resumed_at updates 1 and 4; one run, reached 6
    runs                                                (no output)            exit 0
    status                                              last loop: never       exit 0
    checkpoints --source HOME/track                     eight lines, both runs
    report examples/coin.py                             HOME/track/reports/coin.html
    python -c "import coin; coin.train(coin.Config())"  etude.errors.EtudeError: experiment_not_named
    run.json                                            "id": "coin/slow-seed-0", "job_name": "slow", "seed": 0

**Open, widening the third pass's branching question: what is a run started
with `etude run` to the rest of Etude?** It is files only. The launcher
database never hears of it, so `etude runs`, `status`, `cancel` and
`continue` do not see it, and nothing lists local runs but `ls`, a report or
`checkpoints --source`. Options: (a) files only, said plainly (the docs
correction now directed); (b) `etude run` records the run in the launcher
database as a run on this computer, so the same reads and `continue` work
for it; (c) the reads take the runs directory (`etude runs --source DIR`)
and branching stays as the third pass's (b)/(c). Matters because the page
teaches one word, run, for both, and the first run anyone makes is a local
one. Does not hold the implement direction.

**Open: what a direct call of `train(config)` means.** Today it is refused
(`experiment_not_named`); the directed correction only makes the refusal
say what to do. Options: (a) keep refusing; (b) a direct call is a new run
as `etude run FILE` would make it (experiment from the module's file, seed
0, a taken id refused), so a test or debugger needs nothing else; (c) a
Python entry taking the function and a config object, since
`etude.local.run` takes a file path and a JSON string. Evidence: Jack Heart's
"You can just call our code" (principle 1) against "Nothing is inferred"
(principle 8). Does not hold the implement direction.

**Open name, small.** The docs' "id" is `train-seed-0`; the code's
`RunRecord.id` and the stored `run.json` field are `train/train-seed-0`, and
the code's word for the short form is `RunRecord.name`, while `--name` names
the job. Options: (a) keep, with the docs saying what the file holds (now
directed); (b) store the short id beside `experiment`; (c) teach "name" for
the short form and rename `--name`. Does not hold the implement direction.

## RunAttempt and writers (2026-11-13)

Options put to Jack Heart: (a) keep `RunWriter` beside `RunAttempt`; (b) one
`RunAttempt` with writers as a part of it (Slurm job/steps shape); (c) another
name for the writer. He answered: "- b. one RunAttempt with writers as a part
of it i think  probably?" (tentative; show at demo). Keep the many-writers-
per-allocation behaviour: each writer keeps its own first-row cutoff, and
writers do not count against attempt limits or rental accounting.

## Eval planning boundary: the author function returns the tournament (2026-11-13)

Proposed: `def arena(config: ArenaConfig) -> Benchmark` is pure (no I/O);
`evaluation(arena, config=ArenaConfig(), play=play)` makes the job; the planner
calls `arena(config)` at submission and plans pending matches and checkpoint
inputs as today; on the machine Etude runs `with evaluating(benchmark) as run:
run.play(play)`. Prior art: Inspect AI `@task -> Task`, HELM
`run_spec_function -> RunSpec`. Jack Heart: "I guess thats fine." This
replaces the earlier sketch where the author function ran the eval itself.
