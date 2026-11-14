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

## Address and loop ergonomics pass (2026-11-13)

Built the recorded experiment/job@seed selection design and the later cadence,
automatic-clock and should_stop steer. `design-train-api.md` holds the settled
semantics and deletion cut. No schema or SQL-query change. Historical delivery
uses stored ids; compact training and nested eval discovery excludes out data.
Focused verification is recorded below. Gate owns the
full suite, acceptance and PR handoff; PR #103 remains draft.

Later steers built: string clock declarations optionally close the set;
first-use log keywords declare count clocks when open; every row requires all
used clocks; metric sets may differ and missing metrics remain null. Step is
only a released warning alias through 0.5; moved module paths gain none.
The Manabot malformed historical-record defect is fixed with focused coverage.

Checks: `uv run pytest tests/train/test_steps.py tests/train/test_addresses.py
 tests/train/test_local.py tests/train/test_identity.py -q --disable-warnings`
— 58 passed. The qualified-name/checkpoint-producer regression added afterward
passed separately (1 passed). Narrow launch/resume, worker/eval contexts,
continuation and smoke checks passed in this pass; full acceptance stays gate's.
Mypy on the 19 affected source/test files, ruff check, ruff format --check and
`git diff --check` pass. Measured discovery: 1 listing, 2 declaration reads for
a compact training root and its nested eval root. Context under both limits.

Compress after that pass: local training reads the stored record once and
picks its config through `changed_config` alone; `run_directory` names the
local run once and loops over the two historical folders; `run.run_name_taken`
owns the taken-run wording for `declare` and `etude train`. No behavior change.
`uv run pytest tests/train/test_addresses.py tests/train/test_local.py
tests/train/test_identity.py tests/test_jobs.py tests/launch/test_resume.py
tests/run/test_layout.py tests/test_continuation_out.py tests/eval/test_context.py
tests/train/test_track.py -q` — passed (106, then 82 on the overlapping rerun);
mypy and ruff clean on the touched files. Left alone: `load_job_function`'s
`current=` re-execution (only `etude train` uses it; whether a persistent
client needs it is a demo judgment) and the launcher's separate
"already admitted" wording.

Realign after that pass: the addressing, historical-read compatibility and
`--until` wording in `run.run_name_taken` were already built; nothing of that
direction remains. Quote labels for id-less requests now read `JOB@SEED`
(`Scheduler.quote`), and the launcher doc's quote example matches.
`tests/launch/test_scheduler.py` — 42 passed; mypy and ruff clean. Still
spelled `NAME-seed-N` by choice: explicit standalone `track(run=...)` names in
`docs/tracking.md` and historical-id fixtures.

Ninth concept review: next implement direction is design-train-api.md (Ninth
concept review), six items on several-run selections, refusal wording, local
requirements of a `runs()` file, printed addresses and `report --experiment`.
Unbuilt. Open 6 and 7 there are unanswered and do not hold the build.
