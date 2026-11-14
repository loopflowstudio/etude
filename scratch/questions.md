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

Built package-layout, allocation/writer and typed-match decisions remain at
`312928d:scratch/questions.md`; the writer shape remains a demo choice.

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

The eighth review's probes and contrary fetch wording remain at
`0dcf4d7:scratch/questions.md` (Eighth concept review); its corrections are built.

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
Report now follows --experiment for a single file (ninth review correction).

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

Manabot's module-move probe and malformed-record fix are retained at
`0dcf4d7:scratch/questions.md`. Jack Heart: "i think we dont need warnings, were
teh only users". No aliases for moved paths.

## Ninth concept review (2026-11-13)

Probed at `ca88f10` with an empty `ETUDE_HOME`; direction is in
design-train-api.md (Ninth concept review), open choices in its Open 6 and 7.

Exact coin and multi-job command output is retained at
`0dcf4d7:scratch/questions.md` (Ninth concept review). The probe found partial
selection execution before a later refusal, missing local declaration support,
misprinted compact addresses and no report override; the design lists the fixes.

Assumption, reversible: never-started runs under `--resume` stay refused
(Open 6); a changed config still points to `--experiment` (Open 7).

## Selection recovery probe (2026-11-13)

Concept review on `822eb553`: `uv run python` called `etude.cli.main` against
a temporary recipe, isolated `ETUDE_HOME`, three seeds and three steps.
Only a Pydantic counter was saved; no model training, rental, network or
artificial load. Seed 1 raised `Stopped` once after saving step 1.

Observed command receipt:

```text
etude train sweep.py                         exit 75; sweep@0 finished, sweep@1 stopped
etude train sweep.py --resume                exit 1
etude: selection_refused: sweep@2: run_not_found: sweep@2
refusal_preserved_all_files=True
etude train sweep.py --resume --seed 1        exit 0; coordinates and counts [1, 2, 3]
etude train sweep.py --seed 2                 exit 0
etude train sweep.py --resume --json          exit 0; three finished objects, selection order
etude train sweep.py --seed 0 --config '{"rate":0.2}'
etude: run_identity_conflict: rate was 0.1, is 0.2; use --resume --until CLOCK=TARGET to train this run further; choose --experiment NAME for a changed config
etude train sweep.py --seed 0 --config '{"rate":0.2}' --experiment wide
exit 0; experiment labels ["sweep", "wide"]
```

The mixed selection was recovered with explicit seed commands, not by
changing the assumption. Open 6 and 7 remain unanswered product choices;
the review adds no reply or authorization from Jack Heart. The temporary
fixture was removed and no processes were left running.
