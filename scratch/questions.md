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

Earlier CLI choices and unanswered questions (superseded by Decisions through
demo below) remain at `e40e1a2:scratch/questions.md`. Experiment-scoped reads
select folders by their own declarations; wholly undeclared historical metric
fixtures remain readable.

## Writer/allocation evidence

Full writer probes and resume/report-label choices remain at
`acf036f:scratch/questions.md`. The persisted job digest is identity;
report labels are derived. Resume keeps cohort limits; queued runs without
allocations replace their pending specification.

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

## Earlier review history

Eval consistency requests, the superseded author-owned execution sketch and
review-one-through-four probes remain at `297abf7:scratch/questions.md`.
The pure tournament boundary below supersedes that sketch; local branching,
listing and direct-call choices remain as recorded under Decisions through demo.

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

Built. Jack Heart: "i do think conditions should be pydantic"; "and be like
MatchConfig or something"; "but only i guess similar to TrainConfig, doesnt
need to be named by us"; "just needs to be a pydantic model". The proposed
shape and readings are at `acf036f:scratch/questions.md`.

## Implementation choices for demo (2026-11-13)

Built: `RunAttempt.writers` holds the tracking/eval contexts, each with its
own id and first-row cutoff; the allocation keeps one id, machine, rental
share and attempt-limit charge. Schema 19 edited in place; no new query or
remote read.

Reversible choice to show at demo: authored evaluation jobs use the planner's
selected matches, lines and inputs; the low-level `evaluating` opener keeps
explicit `lines=`. Detail and controlled-check receipts:
`acf036f:scratch/questions.md` (Implementation choices for demo).

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

Typed match config and CLI-file imports are built. The detailed cut and
controlled receipts remain at `297abf7:scratch/questions.md`. Historical
conditions JSON and match keys/seeds survive; warning aliases end in 0.5.
statsdb remains schema 14 over released 13, with no new I/O or query shape.

Outside evidence: OpenSpiel 2.0.2's CPython 3.12 bindings installed and imported
on macOS arm64 and Linux amd64 (uv's Python 3.12 Debian container). PyPI wheel
index: https://pypi.org/project/open-spiel/2.0.2/ . Native Hanabi accepted small
colors/ranks/hands and two players. No fallback game or JAX was needed.

## Earlier concept-review evidence

Sixth and seventh review probes and reversible choices remain at
`acf036f:scratch/questions.md`. Their corrections are built (exp-56.md).
The sixth review's open prose choice remains: should "hold" / "held checkpoint"
follow the current calls `evaluating` / `run.play`, and with which word?

## Eighth concept review (2026-11-13)

Built (exp-56.md). What the probe showed at `acf036f`: with `seeds=(0, 1, 2)` in `job.py`, `etude queue job.py` -> `several
runs or cohort limits need --cohort NAME`; `_smoked([job], False)` (what
`launch(job)` sent) gave one request with seed and id `None`;
`queue_cohort("capacity", job)` raised a pydantic `ValidationError`. `status
coin-seed-0` appended `a changed config needs a new --name` to the missing-run
refusal.

Assumption, reversible: several runs still need `--cohort`, and the docs say
so. The run-address steer (design, "Run addresses") may replace this.

Contrary evidence: `fetch coin-seed-0 weights.pt DIR` reports `run coin-seed-0
has not reached a session`, not the shared refusal. Left alone: changing it
would add a database read.
