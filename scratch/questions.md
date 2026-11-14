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

## Writer/allocation evidence

At `9a762a1`, two sequential tracking contexts wrote two writers and two rows
under `allocation-1`; a third context wrote another run under that allocation.
No worker, network or rental was involved. The full probe and contrary early
"1a" answer are retained at `416847a:scratch/questions.md` (Record unification).
The later answer below chooses writers as sub-parts of the allocation.

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

## Eval consistency history

Jack Heart: "probably worth looking at whether there are any changes to the
eval API we want to make to make it match the changes to the training API. We
don't need to do a full redo here but we should try to make it consistent
where there's no reason not to"; "Can we get the best of both worlds where we
make it match training but we also make it easy to use our Matchup APIs?".

The earlier author-owned execution sketch and the pre-launch input-planning
counterexample are retained at `416847a:scratch/questions.md` (Eval API and
planning boundary). The later pure tournament reply below supersedes that
sketch: submission must see the tournament before planning inputs, since the
worker has upload-only grants and cannot fetch arbitrary checkpoints.

## Earlier review evidence

The local branching/listing, direct-call and id/name probes and prior module
naming choices are retained at `416847a:scratch/questions.md` (concept reviews
one through four). Those observations remain true; the decisions below choose
what to ship rather than adding more local-launcher integration in this cut.

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

## Typed match conditions (2026-11-13)

Jack Heart: "i do think conditions should be pydantic". Proposed shape (not
his words): `Seating.conditions` takes the author's Pydantic models (e.g.
`Deal(deal=7)`); stored and hashed as their JSON, so a match key and stored
data are unchanged for the same values and historical dict conditions stay
readable; `play` sees them validated back into its type, the way `etude.call`
validates a config into the function's parameter type (e.g. `play(match:
Match[Deal])`). `play(match) -> Tables` keeps `Match` as its one argument.
Then: "and be like MatchConfig or something". Read as: the field is the
match's config, the author's own Pydantic type, named like the training
config (`conditions` -> `config` on Seating and Match; the author's class is
e.g. `MatchConfig`; Etude defines no class of that name). Stored `conditions`
stays readable.
Then: "but only i guess similar to TrainConfig, doesnt need to be named by
us" and "just needs to be a pydantic model". Confirms: Etude names no match
config class; any Pydantic model of the author's.

## Implementation choices for demo (2026-11-13)

Built: `RunAttempt.writers` contains the tracking/eval contexts. Each keeps its
own id and first-row cutoff; `RunData.writers` reads the contexts of one run.
The observed old `RunData.attempts` accessor warns until 0.5. Reports label
these as writers rather than counting them as allocations. The allocation
still has one id, machine, number, rental share and attempt-limit charge.
Schema 19 is edited in place; no additional migration. No new database query
or remote read is added: metadata rides the existing progress/finished journal.

Simpler reversible choice: authored evaluation jobs use the planner's existing
selected matches, lines and input mapping. The low-level `evaluating` opener
retains explicit `lines=` for relative seats outside that path; it does not
import training or read arbitrary remote training files on a worker. Show this
at demo alongside the pure `arena(config) -> Benchmark | RoundRobin` shape.
Empty tournament rollups are omitted from serialization to preserve historical
hold names. Ready-made tournament jobs remain a shortcut on the same path.

Controlled checks: one real local allocation with four contexts (another run
and a silent context included) finished with one attempt and zero dollars;
its metric points were `[10, 200, 300]` at updates `[1, 2, 3]`. A pure eval
function's launched pass delivered only its selected checkpoint inputs and a
second pass launched nothing. Planned eval stop retained one match; explicit
resume played the other, and incompatible/taken-name refusals kept the finished
outcome bytes. Rollup resume retained one total rather than adding it twice.

## Decisions through demo (2026-11-13)

Jack Heart: "i tink iwant to see this go all the way to demo now, with decisions
made by you / the worker until we're ready to demo it using some pufferlib
thing". Then: "maybe we need something else for hte demo. i liked hanabi but
we dont do jax yet". The later steer replaces PufferLib with OpenSpiel Hanabi
and PyTorch, with cooperative group rows; EXP-62 remains separate.

Choices made here:
- Keep allocation-owned writers with individual cutoffs and a flat per-run
  reader view; the four-writer/one-allocation check proves no inflated limits.
- Move the unreleased `etude.job` types to `etude.spec`, beside constructors in
  `etude.jobs`. This matches the existing `RunSubmission.spec` fact and keeps
  low-dependency types out of the constructor module's train/eval imports.
  Delete `etude.job`; published launcher type imports still re-export one owner.
- Omitted local resume config uses the stored JSON, validated by the current
  recipe type. Explicit config uses current defaults and must still match.
  This avoids repeating overrides; source compatibility remains the author's.
- First Ctrl-C requests a saved stop; the second interrupts immediately and
  fails that attempt, leaving the last whole save available. Repeated SIGTERM
  remains a stop request. The focused signal check uses no artificial load.
- Upload-only remote cleanup waits for a controller or the next observation
  command. Document it; start no hidden daemon and hand out no delete grant.
- Keep direct calls explicit (`track(experiment=, run=)`); local runs remain
  files, launched continuation stays the branching path, ids stay qualified
  in run.json. These avoid implicit identity or a new persistence path.
- Keep opener `evaluating`, five-minute default saves (or every= steps), and
  coin as the smallest introductory example. Hanabi is the optional demo.

Match config is built end to end: Match[AuthorModel], generic Seating and
RoundRobin, config keyword and callback validation in each worker. Python
models survive author construction; records keep JSON under the released
`conditions` spelling and match keys/seeds remain equal. Old keyword/property
warn until 0.5. statsdb exposes matches.config; its unreleased schema cut stays
14 over released 13, with no new query shape or I/O. `typing-extensions` provides
Python 3.12's generic default support; it was already required by Pydantic.

A single-file example exposed an existing refusal: runpy-loaded top-level play
was treated as unimportable. The job builder now names that function from the
file's real path; nested functions remain unimportable. Its old exclusive
refusal test is replaced by the import-path result test.

Outside evidence: OpenSpiel 2.0.2's CPython 3.12 bindings installed and imported
on macOS arm64 and Linux amd64 (uv's Python 3.12 Debian container). PyPI wheel
index: https://pypi.org/project/open-spiel/2.0.2/ . Native Hanabi accepted small
colors/ranks/hands and two players. No fallback game or JAX was needed.
