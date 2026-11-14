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

## Built layout and writer decisions

Exact package-layout, allocation/writer, pure-eval and typed-match replies are
retained at `312928d:scratch/questions.md` (Writer/allocation evidence through
Implementation choices for demo). Keep shared run/records/storage independent
of train, eval and launch; train/eval independent of each other. Allocation
writers keep independent cutoffs without extra budget charges. Authored evals
use planned lines and inputs; the low-level opener keeps explicit lines=.
The writer shape remains a demo choice, not an attributed acceptance verdict.

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


## Headless choices for addresses and loop ergonomics (2026-11-13)

Use the demo's recorded `@` address choice. Omission uses the full declaration
before --job/--seed selection. A train-only file has seed 0; declare runs() to
select other seeds. Explicit RunRequests retain their supplied ids; local
execution requires an id. Local selections run in order and stop at the first
saved stop; launch --resume selects one run. Existing cohort requirements stand.
Both save cadence options mean either threshold; specifying only save_every
replaces the default 300-second cadence. These are reversible implementation
choices, not additional replies from Jack Heart. Continue's existing child
suffix remains; status/logs and continuation consume the same full address.
Report-file naming remains file-based; named variants can be read with the
Python readers. A broader report override is outside this pass.

The later clock steer supersedes the wrapper choice above. Jack Heart, quoting
"any keyword on run.log is a clock, so the first row that uses it declares it /
it's a count unless you declare it Time / its name is its label / a later row
missing it is refused": "these all sound fine to me". Keep Step only as its
released warning alias until 0.5. Table row keywords follow the same rule;
missing used clocks follow the existing strict/warn policy. Dynamic declarations
are serialized across contexts; queued rows seal with their original columns.

Later clock strictness: Jack Heart, "i do think we should have some amount of
config between: - no non-init-declared clocks vs not - all clocks set every
update vs not"; then "Right be we have a separate eval paradigm". Given
`track(clocks=["updates", ...])`, close the set; omitted, declare by use.
Every row requires all used clocks, with strict raise versus warn-once;
no sparse-clock switch. Jack Heart: "i think we should allow sparsity in
WHAT gets logged, but i see little reason to be loose with clocks". Metrics
may differ per row; missing reads null, never zero or carried forward.

Manabot check steer `9f5a3e52-f52e-458a-9c62-51efc6b0cb10`, source branch
382c653ec8 checked against ddff7d4: 33/35 loop/recipe tests passed after module
moves. Jack Heart: "i think we dont need warnings, were teh only users".
No aliases for moved paths. Malformed nested declarations now raise a Pydantic
validation error rather than KeyError. Minimal id-only records remain valid.

## Ninth concept review (2026-11-13)

Probed at `ca88f10` with an empty `ETUDE_HOME`; direction is in
design-train-api.md (Ninth concept review), open choices in its Open 6 and 7.

`examples/coin.py`, after `etude train examples/coin.py --until updates=10`:
- `--config '{"updates":40}'` -> `run_identity_conflict: updates was 20, is 40;
  choose --experiment NAME`
- no flags -> `run_name_taken: coin; use --resume; to train further give
  --resume --until CLOCK=TARGET`
- `--resume` -> `run_already_finished: finished at updates=10; give --until
  with a later target`; `--resume --until updates=20` trained on, exit 0
- `--resume --config '{"lr":0.2}'` -> `run_identity_conflict: lr was 0.1, is 0.2`
- `etude checkpoints` lists the run as `coin/coin`
- `etude report examples/coin.py --experiment wide` -> `No such option`

A file with `runs()`: two jobs, `narrow` seeds 0-2 and `wide` seeds 0-1:
- without `max_hours`: `a job needs max_hours`; outside git: `is in no git
  checkout; name the job's source with repo=`
- with `narrow@0` finished, `narrow@1` ended at 3, `narrow@2` never started:
  `etude train F` -> `run_name_taken: sweep/narrow@0`; `--resume` and `--job
  narrow --resume` -> `run_already_finished: finished at updates=6`;
  `--job narrow --resume --until updates=8` trained `@0` and `@1`, then
  `run_not_found: sweep/narrow@2`, exit 1
- `--job wide --json` printed two objects, one per run

Assumption, reversible: never-started runs under `--resume` stay refused
(Open 6); a changed config still points to `--experiment` (Open 7).
