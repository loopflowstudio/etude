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
  match validation, staging, rollups and delivery; `evaluate` keeps its
  one-call shape. Lightweight metric filenames live in `etude.block_file`.
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
- Launcher references are `EXPERIMENT/RUN`, yielding that same folder and
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

The first eight reviews, eval/writer and typed-match cuts are built. Their
exact directions and receipts remain at `312928d:scratch/exp-56.md`; those
receipts predate this pass and are not current acceptance evidence. The accepted
surface is in user docs. Historical outcomes/records, state publication,
planned stops, continuation, row replacement and cumulative budgets survive.

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

Focused checks (2026-11-13):
- `uv run pytest tests/train/test_addresses.py tests/train/test_local.py tests/test_page.py -q -k 'selection_preflight or local_declarations or checkpoint_listing or experiment_override or local_selection' --disable-warnings` — 6 passed before the package-source case was added.
- `uv run pytest tests/train/test_addresses.py tests/train/test_local.py -q -k 'selection or local_stop_resume' --disable-warnings` — 8 passed, including skipped JSON outcomes and untouched-run refusals.
- `uv run pytest tests/test_jobs.py tests/launch/test_resume.py tests/launch/test_scheduler.py -q -k 'job_file_names or evaluation_holds_what or typed_jobs_take or resume_retains_allocations or refused_resume_preserves or hours_and_dollars or run_longer_than_its_storage or a_job_is_what_decides or assigned_run_name' --disable-warnings` — 14 passed.
- `uv run pytest tests/train/test_addresses.py -q -k 'local_declarations or local_selection' --disable-warnings` — 3 passed, including package sources outside git and both config remedies.

Mypy on the 13 touched Python files, ruff check, ruff format --check and
`git diff --check` pass. Full suites and acceptance remain for gate.
