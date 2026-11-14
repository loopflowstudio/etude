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

The eval consistency steer is built: pure author functions return the tournament
at submission, before match/input planning. `evaluating` opens the live run;
`run.play` executes it. `hold`/`evaluate` warn and delegate until 0.5. Relative
seats in authored jobs use the existing planner's lines and selected inputs;
the low-level opener retains explicit `lines=` (demo detail in questions.md).

Initial run/record/scope acceptance and the first three review corrections
are built. Exact focused check receipts and earlier API corrections remain at
`6f70baa:scratch/exp-56.md`; they do not replace gate's full acceptance.

Fourth-review corrections (refusal remedies, local-versus-launched docs, stored
layout) are built; receipt at `416847a:scratch/exp-56.md`. Local
listing/branching, direct calls and id/name choices remain in `questions.md`.

Fifth-review corrections are built: CheckpointRef owns local path decoding;
CheckpointRecord delegates to it and playing consumers use checkpoint.path.
Seating/RoundRobin expose configs, serialize conditions, and retain only the
released conditions warning alias. Hanabi uses Deal.deal and default_root().
Delete — do not maintain: hand URL parses in playing fixtures/docs/example,
the record's duplicate decoder and the unreleased collection config spelling.
No schema or query changes. Gate still owns full acceptance; PR #103 stays draft.

Check: ruff, format, `uv run mypy` (175 files) pass; `uv run pytest` over
tests/eval/{test_match,test_tournament,test_records,test_context}.py,
tests/test_{continuation_out,experiment,jobs,page_evals,smoke}.py and
tests/launch/{test_worker_attempts,test_ssh,test_runpod_machines}.py — 200
passed. Covers pinned historical keys, stored conditions JSON/warnings, paths
with spaces and remote-path refusals. Test readers of a checkpoint record use
`.path` too; one stored-attempt expectation gained `writers: []` (stale since
`620bfd6`).

Hanabi command receipt: isolated ETUDE_HOME with spaces; run updates=2,
resume --until updates=3, eval (8 new/8 played), eval (0 new), report, and
the docs' statsdb(default_root()) query (16 team rows, mean score 0.9375).
Report: `/var/folders/m6/r3tllnrs1yq7yfbwm680tss40000gn/T/exp56 Hanabi mstut3r3/track/reports/hanabi.html`.
No process remains. Earlier stop/resume evidence stays in demo.md.

Compress pass: one writer merge in the launcher database, `RunProgress` under
one name, resume identity resolved once in `etude.local`; no behavior change.
Check: ruff, format, `uv run mypy` (175 files) pass; `uv run pytest
tests/test_jobs.py tests/eval/test_context.py tests/eval/test_match.py
tests/train/test_local.py tests/launch/test_worker_attempts.py
tests/launch/test_scheduler.py tests/train/test_tables.py tests/test_page.py`
— 139 passed.

Writer/eval cutovers and the later typed-match-config steer are built. Remaining
choices have been made in questions.md; the unreleased type module is now
etude.spec, local resume defaults to stored config, and a second Ctrl-C exits.
Optional Hanabi (OpenSpiel/PyTorch) is ready for a person's demo; no PufferLib
adoption or EXP-62 work is included. docs/hanabi.md gives the commands.

Earlier typed-match, writer/allocation, stop/resume and CLI-file import checks
are retained at `e40e1a2:scratch/exp-56.md`; full acceptance stays with gate.

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
