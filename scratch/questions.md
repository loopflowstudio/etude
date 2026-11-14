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

Typed match config and CLI-file imports are built. The detailed cut and
controlled receipts remain at `297abf7:scratch/questions.md`. Historical
conditions JSON and match keys/seeds survive; warning aliases end in 0.5.
statsdb remains schema 14 over released 13, with no new I/O or query shape.

Outside evidence: OpenSpiel 2.0.2's CPython 3.12 bindings installed and imported
on macOS arm64 and Linux amd64 (uv's Python 3.12 Debian container). PyPI wheel
index: https://pypi.org/project/open-spiel/2.0.2/ . Native Hanabi accepted small
colors/ranks/hands and two players. No fallback game or JAX was needed.

## Sixth concept review (2026-11-13)

Run at `67ebe7c`, ETUDE_HOME `/tmp/exp56 review6 8zKT` (removed after):
`etude checkpoints` -> `etude: Missing option '--source'.` (exit 1);
`etude continue --run coin-seed-0 --until updates=30` and the same with
`coin/coin-seed-0` -> `etude: no run '...'`; `etude runs` printed nothing.
`etude checkpoints --source "$ETUDE_HOME/track"` listed four checkpoints.

Assumption, reversible: `hold` the function is deprecated, but the
evaluation and report docs keep "hold" / "held checkpoint" as prose for the
checkpoint a benchmark's seatings are played for. Only sentences telling the
reader to call `hold` or `evaluate` are corrected. Open for demo: should the
prose verb follow the call (`evaluating`, `run.play`), and if so with which
word? `etude eval` help already says "pass" for one batch run.

## Seventh concept review (2026-11-13)

Run at `c8310bc`, ETUDE_HOME `/tmp/exp56 review7 51152` (removed after), coin
example with `uv run --extra train etude`:
`run examples/coin.py --config '{"lrr":1}' --name bad2 --until updates=2` ->
`bad2-seed-0`, exit 0; its `run.json` config is
`{"heads": 0.7, "lr": 0.1, "updates": 20}`.
`run ... --seconds 0.001 --name short --config '{"updates":200}' --json` ->
`"outcome": "stopped"`, exit 75; `--resume short-seed-0` finished; then
`--resume short-seed-0 --until updates=150` ->
`run_already_finished: give --until with a later target`.
`run ... --until update=5 --name typo` -> `target_clock_undeclared: update`,
and no `typo-seed-0` folder was left, so the name stayed free.
A `train` raising at update 4 recorded `failed`; `--resume` without the bug
finished at 6 with its saved count intact.

Checkpoint given to a job: `observe(default_root(), "coin").run(
"coin-seed-0").final` has members `checkpoint.json`, `policy.pt`;
`training(..., start=final)` produced inputs `start/checkpoint.json` and
`start/policy.pt`. `evaluation(checkpoint=)` is typed `Input | None`
(`jobs.py`), one key with one digest. The probe passing the record to it
stopped earlier, on the probe's own unimportable `play`, so the refusal
itself was not observed; the finding rests on the signature and on `_hold`
building the key from the input's file name.

Assumptions, reversible: a released `Input` stays accepted without a
warning; unknown-field refusal is top-level only, nested models keep
Pydantic's behaviour; a config model with `extra="allow"` keeps its extras.
