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

Reviews one through six and compress are built. Their exact directions and
focused receipts are retained at `297abf7:scratch/exp-56.md`; full acceptance
remains gate's. Sixth-review changes give checkpoints a default source and
empty-selection remedy, qualify continue ids, explain missing launcher runs,
and name the current evaluation calls. No schema, query or provider-count
change. PR #103 stays draft.

Seventh-review corrections are built: evaluation(checkpoint=) accepts a
CheckpointRecord and delivers its members while preserving its key; released
single-file Input still works. Local run/new/resume and continue share JSON
change validation; omitted resume config still reads the stored model.
Finished-run refusals include actual clock positions. README and training,
launcher and evaluation docs match. No schema, SQL or new remote reads.

Eighth-review corrections are built (design, "Corrections from the eighth
concept review"): Job.runs owns seed expansion for file/Python clients;
singular clients refuse multiple seeds; cohort clients accept Jobs and mixed
requests. Docs teach cohort launches and one experiment; missing-run config
advice stays on continue. The file reader's seed loop is deleted. No schema,
SQL or provider-count change. Fetch's separate refusal is preserved; see
questions.md for the contrary evidence. Full acceptance remains with gate.
Compress: `_one` holds the singular refusal for queue/launch. Seeds given to
`Job.runs(1, 2)` keep admission-time ids (ids for them broke resubmission in
test_smoke/test_cli); only declared seeds are `NAME-seed-N`.

Steer built (Jack Heart: "i feel like it should be etude traine hanabi"):
`etude run FILE` is `etude train FILE` (`etude.local.train`), no alias; run
stays a noun. Unbuilt: experiment/job/seed addresses (design, "Run
addresses").
Check: `uv run pytest tests/test_jobs.py tests/test_smoke.py
tests/launch/test_queue.py tests/launch/test_cli.py tests/train/test_local.py
tests/train/test_track.py tests/launch/test_lium.py` — 158 passed; mypy,
ruff check and format pass.

Compress: `_resumed` computes the reached positions once; `_hold` and
`evaluation` share `_BENCHMARK_CHECKPOINT`; the test `play` reads weights
through one helper. Check: `uv run pytest tests/test_jobs.py
tests/train/test_local.py tests/test_continuation_out.py` — 61 passed; mypy
clean on jobs/local/call and tests/test_jobs.py; ruff check, format and
`git diff --check` pass. Gate owns full acceptance. PR #103 remains draft.

Hanabi command receipt and temporary report location are retained at
`06ff0f6:scratch/exp-56.md`; durable stop/resume evidence is in demo.md.

Compress pass and its 139-test receipt: `06ff0f6:scratch/exp-56.md`.

Writer/eval and typed-match cuts are built. questions.md holds decisions;
docs/hanabi.md prepares the optional OpenSpiel/PyTorch demo. No PufferLib
adoption or EXP-62 work is included.

Earlier typed-match, writer/allocation, stop/resume and CLI-file import checks
are retained at `e40e1a2:scratch/exp-56.md`; full acceptance stays with gate.

Indexed-query receipts: `ef6099a:scratch/exp-56.md`.

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
