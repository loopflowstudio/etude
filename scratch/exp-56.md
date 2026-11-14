# Training API rebuild (EXP-56)

Build `design-train-api.md` and the corrected user docs. PR #103 stays draft.
Jack Heart: "#103 was converted to draft; keep it draft, do not mark it ready."
Manabot adoption is MTG-329; do not edit its checkout.

The accepted surface is in `docs/training.md`, `docs/launcher.md`,
`docs/tracking.md` and `docs/evaluation.md`. Keep their corrections. The
state-publication layout is self-contained save folders, pointer published
last, only the selected save delivered. The courier owns acknowledgement and
local retention; the controller removes old remote saves under upload-only
grants. Preserve planned-stop retries, row replacement, continuation,
cancellation and cumulative hours/dollars/allocation limits.

Earlier design, probes, exact quotes and check receipts are in git:
`224e45c:scratch/exp-56.md`, `224e45c:scratch/questions.md`,
`1666be1:scratch/positioning.md`, `1666be1:scratch/ontology.md` and
`1666be1:scratch/etude-readme-draft.md`. Checks recorded there predate the accepted
API and prove nothing about the current tree.

## Delete — do not maintain

Built in this cut (committed at `dbf799e`; grep finds no `etude.tracking`,
`RunDeclaration` or `RunFile` left in src, tests or docs):

- `etude.tracking` is deleted. `etude.run` owns shared declaration, local
  files, delivery, listing and live `Run`. `TrainingRun(Run)` lives in
  `etude.train.tracking`; `EvalRun(Run)` in `etude.eval.run`. The latter owns
  match validation, staging, rollups and delivery; `evaluating` opens it and
  `run.play` executes matches. Released `evaluate` warns until 0.5 and delegates
  to it. Lightweight metric filenames live in `etude.block_file`.
- `RunDeclaration`/`RunFile` and launcher `RunRecord` become the single
  `etude.records.RunRecord`. Historical nested declarations decode there.
  The record's launcher specification is absent for standalone writers;
  `RunRecord.launch` requires one. Each config is stored once (within the
  specification when launched). The job digest remains identity;
  `TrainingRunSummary.job` remains a derived report label.
- Shared job specification types move to `etude.spec`, so neither shared run
  records nor training/evaluation import the launcher. Released
  `etude.launch.records` imports re-export the same types.
- The observed `TrainingRun` becomes `TrainingRunSummary`; the result of
  `evaluate` becomes `EvalResult`. Their old names warn until 0.5.
  `etude.train.run` is a warning-only import surface until 0.5, with no
  implementation of its own.
- Launcher references are `EXPERIMENT[/JOB][@SEED]`, yielding that same folder and
  grant prefix. Worker environment gives training its local run name and
  experiment. Local writers also use experiment folders; existing flat
  declarations still select the old folder when their experiment matches.
- Continuation selects by declaration and experiment, across historical
  physical copies. Checkpoint inputs retain the resolved source folder;
  input locators are excluded from scientific job identity.
  Legacy eval conversion and deletion refuse ambiguous experiment names.

Already built before this cut: launch `Run` -> `RunRequest`; checkpoint
record/reference unification and folder checkpoints; checkpoint `Model` ->
`CheckpointPlayer` and `.player`; `outcome.json` with old outcome readers;
create-only run identity and explicit resume; state registration and saves;
local and launched `--resume RUN --until`; job report labels preserving
seeded continuation lineage. Deprecated taught spellings warn until 0.5.
Do not restore deleted implementations or their exclusive tests.

`RunAttempt.writers` now holds its tracking/eval contexts. Each `RunWriter`
keeps its own first-row cutoff; contexts never inflate allocation or rental
accounting. The tentative shape stays for demo (`questions.md`).

Launcher schema remains 19, the single unreleased schema change over main's
18 (`286e0c5`). New keyed/scoped lookups use indexed SQLite queries. Do not
stack another migration; the launcher refuses older database files.

## Remaining work and verification

Full acceptance stays with gate: fresh/stop/resume; two models in one save and
checkpoint seated in evaluation; name/config/seed refusals; changed-source
provenance; start= links; stop between phases; finished outcome unchanged by a
refusal. The hands-on Hanabi evidence is in demo-hanabi.md. Manabot adoption
and smoke remain MTG-329; do not edit its checkout. PR #103 stays draft.

## Folded and later work

Jack Heart: finished-run corruption "This should be part of 56"; EXP-37
"Fold this one into 56". The durable save layout and restorable-state boundary
are covered. On EXP-37's other points: "followup task is fine". Remaining
fault-injection matrix, whole-budget semantics beyond execution hours and
detailed acquisition/loss causes do not hold this release. Exact scope and
quotes are retained at `224e45c:scratch/exp-56.md`.

Later Tasks: EXP-57 live view; EXP-58 machines/metrics; EXP-59 broader JSON;
EXP-60 population/evaluation expansion; EXP-61 joinable budgets. EXP-64's
package split is folded into EXP-56.

## Address, loop and selection corrections (2026-11-13)

Address and loop ergonomics are built; their earlier receipts and compression
notes remain at `0dcf4d7:scratch/exp-56.md`. Current contracts are in user docs.

Ninth review corrections are built: whole-selection preflight; finished members
skip during multi-run resume (including ordered JSON outcomes); shared config
refusal remedies; local declarations without hours/git; stored checkpoint
addresses; report experiment overrides, including watch/serve. Unstarted resume
members remain refused before any work (Open 6); variants remain new experiments
(Open 7). PR #103 remains draft. Gate still owns full acceptance.

Delete — do not maintain: the per-run validation/execution path and training
constructor's rental-only requirement are removed. One preflight/execution path
serves both file forms; launch/queue/smoke still require machine hours and git.
No SQL or remote-read path changed; no schema increment.

Earlier selection check commands and compression receipts remain at
`9799f9d:scratch/exp-56.md`; full suites and acceptance remain for gate.

## Report QA corrections (2026-11-13)

Delete — do not maintain: restart-to-resumed-coordinate pace estimates,
unconditional absent default RL panels, and empty evaluation-caption parts.
Pace is null at a writer boundary and recovers between later checkpoints.
Default RL panels retain logged keys; explicit panels still diagnose absent
keys. Evaluation captions join their present parts. No schema, SQL or remote
read path changed. PR #103 stays draft; full acceptance remains for gate.

## Compression (2026-11-13)

Delete — do not maintain: duplicate train/eval stop detectors and parallel
checkpoint name/record maps plus their ordinal counter. `Run.should_stop` owns
the shared signal; one `_Kept` collection owns checkpoint records and names.
Evaluation captions share their present-part join. Behavior and formats stay.

Delete obsolete scratch publication/retention probes that import the removed
`etude.tracking` package. Their code and original evidence remain at
`6f70baa:scratch/state-publication.py`, `6f70baa:scratch/state-retention.py`
and `6f70baa:scratch/questions.md`; current state-publication tests own coverage.

## Current alignment (2026-11-13)

At `b21bc8d`, folder-checkpoint evaluation and `start=` have a two-model
integration fixture (`tests/train/test_checkpoints.py`); the design's
investigation step was stale. Report and compression receipts above remain
focused evidence; full acceptance stays with gate.

Fetched `origin/main` remains `286e0c5`; no upstream commits await reconciliation.
PR #103 is still draft with no submitted reviews. No Wave is attached.
Launcher docs' obsolete schema numbers are corrected to the database's 19.
Open 6 (resume unstarted selection members) and Open 7 (config variants as jobs)
remain unanswered; preserve their current reversible choices for demo.

The shared-report refresh test's counted wrapper now forwards build's
`experiment` keyword; its unchanged-generation and edited-helper assertions stay.
`uv run pytest tests/test_page.py tests/train/test_checkpoints.py tests/train/test_store.py tests/train/test_lifecycle.py tests/train/test_steps.py tests/eval/test_context.py -q --disable-warnings` — 115 passed. All 24 chart tests, wheel build, test_page mypy, touched-file ruff check/format and diff whitespace checks pass.
