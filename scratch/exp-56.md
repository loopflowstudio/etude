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
- Shared job specification types move to `etude.job`, so neither shared run
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

`RunWriter` remains a tracking context, distinct from the `RunAttempt`
machine allocation. See the reproduced many-writers-per-allocation case and
Jack Heart's contrary "1a" answer in `questions.md`. This implementation
correction remains open; do not merge the records and lose writer cutoffs or
inflate rental/attempt accounting.

Launcher schema remains 19, the single unreleased schema change over main's
18 (`286e0c5`). New keyed/scoped lookups use indexed SQLite queries. Do not
stack another migration; the launcher refuses older database files.

## Remaining work and verification

The new eval consistency steer is tentative for demo and is not built.
`questions.md` records the concrete planning/input-delivery counterexample:
a Benchmark constructed inside arbitrary author code is unavailable to the
current pre-launch match planner. Dependent eval API deletion stops until
that boundary is designed; the current run/record/scope cut remains checkable.

Initial run/record/scope acceptance checks pass.

Record/API corrections are built: optional launcher submission metadata,
one run name, `RunOutcome`, and `Stopped` from `etude.stopping`.

Second-review corrections built: `RunData.outcome` with a warning `state`
property until 0.5, all internal consumers moved, local planned-stop docs
corrected, `--name` described as a job name, eval outcome filename corrected.
Check: focused pytest filter (outcome alias/current and historical files,
stop/resume/refusal/failure, reports) **13 passed, 126 deselected**;
`uv run mypy` (174 files), ruff check/format and diff check pass.
Full acceptance stays with gate. Open writer/allocation and eval planning
choices remain in `questions.md`; PR #103 stays draft.

Third-review corrections built: local `--resume EXPERIMENT/RUN` uses
`run_address`; missing-run refusals name the searched experiment and taken-name
hints give a qualified reference. Training and launcher docs show `--run` and
required `--until` for launched continuation, plus the actual config refusal.
Local branching, writer/allocation and eval planning stay open in `questions.md`.
Check: `uv run pytest tests/train/test_local.py` — **10 passed**; mypy on
`local.py`, ruff check/format and diff check pass.
Full acceptance stays with gate; PR #103 remains draft.

Earlier checks and indexed-query receipts: `ef6099a:scratch/exp-56.md`.

Acceptance remains the design's fresh/stop/resume example; two models in one
save and checkpoint seated in evaluation; taken-name/config/seed refusals;
changed-source provenance; `start=` links; stop between phases; a finished
run not overwritten by a refused attempt. Existing local CLI tests cover
these, including a genuine learner failure before any row.

Manabot adoption and its smoke remain MTG-329; see its constraints in the design.

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
