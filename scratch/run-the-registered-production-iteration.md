# INT-4: Run the registered production visit-teacher iteration

## Directive and current state

Directive v7 is incorporated. PR #133 is landed engineering-smoke evidence,
not the production result. This serial PR keeps INT-4 open and executes the
registered production slice. INT-3 is out of scope.

The smoke proved the end-to-end authority, replay, four-arm training, neural
PUCT, arena, and Study paths. It did not run the production controls,
competencies, isolated resource measurements, 256-game dataset, three training
seeds, or 48-game arena cells. The production work must preserve those smoke
artifacts rather than reinterpret them as admission evidence.

## User-visible outcome

The Intelligence owner can run one resumable command and receive a verified,
reviewable production result for the selected w2 matchup:

- Teacher-1 at 8, 32, and 128 traversals is compared with random, scripted
  competency villains, latency-matched flat Monte Carlo, and the exact frozen
  Teacher-0 policy-only and policy/value controls.
- A 256-game replayable visit/value dataset trains chosen-action versus visit
  supervision and policy-only versus policy/value students at seeds 197, 419,
  and 887.
- Teacher, policy-only student, visit/value student, and student-plus-search
  play the complete 48-game-per-cell arena for every training seed.
- The report exposes legality, replay, strength, competencies, calibration,
  p50/p95 latency, throughput, peak RSS, core/wall time, artifact size, and
  label cost, followed by one explicit `admit_student_search`, `admit_student`,
  or `prototype_failure` decision.
- One historical decision remains consumable through the viewer-safe Study
  contract with played Command, policy mass, visits, value, robustness,
  uncertainty, and genuinely unavailable fields kept distinct.

No result is called production evidence until the independent `--verify` pass
accepts every bound artifact and exact replay.

## Source of truth

The authoritative experimental inputs are:

1. `experiments/contracts/int-4-visit-teacher-iteration-v1.json`, specifically
   its frozen `iteration` profile: budgets 8/32/128, four worlds, 256 games,
   four training arms at three seeds, 48-game arena cells, sampled replay
   roots, and resource caps.
2. A new additive production contract,
   `experiments/contracts/int-4-visit-teacher-production-v1.json`, which binds
   the canonical SHA-256 and iteration-profile SHA-256 of the first contract.
   It adds only the previously omitted control, competency, calibration,
   resource-receipt, and admission rules. It must not change the registered
   budgets, dataset size, training seeds, arena blocks, or ablation blocks.
3. The authoritative `managym` state and structured legal offers. Learner
   shards, matchup summaries, calibration summaries, and Study evidence are
   derived views. Audit-only deal/search seeds and hidden authority state never
   enter learner shards or Study.
4. The exact Teacher-0 bytes named at runtime by
   `--policy-only-control` and `--policy-value-control`, accepted only when
   their SHA-256 values equal the hashes already frozen in the iteration
   contract. Paths are operational inputs; hashes and arm identities are the
   authority.

The existing smoke runner, contract, receipt, and report remain unchanged.
Production orchestration lives in
`experiments/runners/run_visit_teacher_production.py`, imports the already
tested iteration components, and pins its own complete source bundle. This
prevents production instrumentation from silently changing PR #133's smoke
identity.

## End-to-end proof

Concrete scenario: for the seed-197 selected matchup, the 128-traversal
Teacher-1 records a viewer-safe root visit distribution and root value, the
audit bundle reconstructs the authoritative root and exactly reproduces its
per-world/aggregate visits, Q values, selected Command, nodes, depth, and
outcome, the same replay-aligned shard trains all four seed-197 arms from the
same initialization, those checkpoints play their declared 48-game arena
cells and competencies, and the selected historical decision is serialized as
a valid Study artifact without private information.

The production run is:

```bash
uv run experiments/runners/run_visit_teacher_production.py \
  --contract experiments/contracts/int-4-visit-teacher-iteration-v1.json \
  --production-contract experiments/contracts/int-4-visit-teacher-production-v1.json \
  --policy-only-control /absolute/path/to/frozen-policy-only.pt \
  --policy-value-control /absolute/path/to/frozen-policy-value.pt \
  --out-dir .runs/int-4-visit-teacher-production-v1
```

The independent proof is:

```bash
uv run experiments/runners/run_visit_teacher_production.py \
  --contract experiments/contracts/int-4-visit-teacher-iteration-v1.json \
  --production-contract experiments/contracts/int-4-visit-teacher-production-v1.json \
  --policy-only-control /absolute/path/to/frozen-policy-only.pt \
  --policy-value-control /absolute/path/to/frozen-policy-value.pt \
  --out-dir .runs/int-4-visit-teacher-production-v1 \
  --verify
```

`--verify` performs no training or evaluation. It rehashes contracts,
controls, runtime/source/extension identities, shards, checkpoints, receipts,
and Study evidence; reruns the exact trajectory and sampled-search replay;
loads every checkpoint; recomputes summaries and gates from immutable raw
records; and validates Study in Python and Rust. It writes only
`verification.json` after all checks pass.

## Production contract and controls

The additive contract freezes these omitted requirements before any production
result is inspected:

- Competencies: S1-S5, 100 runs per agent/scenario, seed 4197, at most four
  spawned workers. Agents are random, all three Teacher-1 budgets, both frozen
  Teacher-0 controls, and each seed's primary student and student-plus-search.
  Scripted villains remain those defined by `manabot.verify.competency`.
- Teacher matchup cells: every Teacher-1 budget versus random, both frozen
  controls, and its latency-matched flat-MC control; plus 128 versus 8. Every
  cell reports the existing three 16-game blocks and their fixed 48-game
  aggregate.
- Arena cells: the existing four-agent round robin and registered ablations
  run independently for seeds 197, 419, and 887. The student-plus-search arm
  retains the registered 128 total PUCT traversals; the report calls it
  traversal-matched and separately reports its realized latency ratio.
- Calibration: on one host and one runtime, reconstruct 16 preregistered
  viewer-safe roots, warm each method before measurement, measure Teacher-1
  and flat-MC on the identical roots, evaluate the fixed integer flat-MC grid
  1 through 128 simulations per legal root action, and select minimum absolute
  p50 gap with lower simulations as the tiebreak. The later 48-game control
  cell must realize a p50 gap no larger than 10%; otherwise matched-wall claims
  fail. The calibration receipt is immutable across resumes.
- Resources: each measured search/matchup/competency cell runs in an isolated
  spawned child. The child reports high-water RSS, user and system CPU time,
  wall time, decisions, labels, traversals/playouts, cap hits, p50/p95 latency,
  and artifact bytes. Aggregation is deterministic and independent of child
  completion order.
- Calibration diagnostics: root-value Brier score, ten-bin ECE/reliability,
  and per-seed outcomes are reported without promoting game blocks to
  independent training seeds.

The exact Teacher-0 checkpoint files are not present in this worktree. The
production implementation may be built and tested with fixtures, but the real
run must fail closed until the artifact archive supplies bytes matching:

- policy-only: `3bfedccf5aa6ed7621d99284ea8cea3975d8b195cecf6426d37dd7abc812c978`
- policy/value: `92ced7abb31bc68298b48cc08ed7eb57f3dde50295a22d50ea2fe32f7e359176`

Do not retrain, port, reconstruct, or substitute either control.

## Gates and admission decision

Integrity is non-negotiable: any illegal Command, target-integrity failure,
viewer-boundary leak, authoritative-root mutation, non-finite target, missing
sampled root, replay mismatch, or identity mismatch invalidates the run and
prevents admission.

Teacher quality retains the already preregistered Teacher-1 gates:

- 128 traversals wins at least 55% versus 8 traversals, at least 55% versus
  the frozen policy/value checkpoint, and at least 80% versus random;
- repeated-search top-action agreement is at least 70% and median
  Jensen-Shannon divergence is at most 0.10;
- nodes and maximum depth increase from 8 to 128;
- at least one of S1, S2, or S5 reaches 20% correct and no S1-S5 result is more
  than 10 percentage points below random;
- p95 is at most 500 ms, throughput is at least two labels/second/worker,
  playout-cap rate is below 0.1%, and every matched flat-MC cell is within the
  10% realized-p50 tolerance.

Teacher quality failure is reported honestly but, if integrity and resource
caps still hold, does not erase the registered four-arm student experiment.
The complete iteration determines admission in this order:

1. `admit_student_search` when its paired win rate versus the seed-matched
   visit/value student has median at least 55%, is above 50% for at least two
   of three training seeds, and no competency is more than 10 percentage
   points below that paired student.
2. Otherwise `admit_student` when the visit/value student clears the same
   median/two-of-three rule against both exact Teacher-0 controls and no
   competency is more than 10 percentage points below the policy/value
   incumbent.
3. Otherwise `prototype_failure`, with the failed teacher, supervision,
   value, search, strength, competency, or economics gates named as the next
   build decision. No checkpoint is admitted by choosing a favorable single
   game block or training seed.

The report also preserves the Project language `continue`, `revise`, or
`kill`; this research disposition is derived from the complete gate vector and
is distinct from checkpoint admission.

## Stage and recovery model

The production manifest is append-only by stage:

1. `preflight` — validate both contracts, current runtime, host, exact control
   hashes/arm identities, and caps before creating evidence artifacts.
2. `calibration` — freeze same-root timing and matched flat-MC mappings.
3. `teacher` — controls, stability, competencies, integrity, economics, and
   teacher gate.
4. `dataset` — 256 games in 8-game shards plus four-game/eight-root audit.
5. `training` — four arms by three seeds from one recorded initialization per
   seed and identical train/validation rows.
6. `arena` — all registered 48-game cells, controls, ablations, and student
   competencies.
7. `study` — one seed-197 historical Study position.
8. `report` — immutable machine receipt plus checked-in compact data/report.

Each completed stage records the hashes of its contracts, runtime, controls,
and upstream manifests. A resume skips only an exact matching completed stage.
Partial child output is written to a temporary path and atomically promoted;
crashes retain completed work without treating partial data as evidence.

## Affected surfaces and consumers

- `experiments/runners/run_visit_teacher_production.py`: production-only CLI,
  stage orchestration, cap enforcement, verification, and report generation.
- `experiments/contracts/int-4-visit-teacher-production-v1.json`: additive
  measurement and decision contract bound to the frozen iteration profile.
- `manabot.verify.competency`: reused scenario authority; only a small adapter
  may be added for production agent specs and in-memory/atomic receipts.
- `manabot.sim.flat_mc`, `manabot.sim.mcts`, visit distillation, Study evidence,
  and existing iteration runner: consumers/providers that remain compatible;
  algorithm or authority changes are not intended.
- `tests/sim/test_visit_teacher_production.py`: contract rejection, control
  hash/arm rejection, stage resume, cap handling, gate evaluation, receipt
  recomputation, and verify-no-generation coverage.
- `.runs/int-4-visit-teacher-production-v1/`: raw resumable artifacts, not
  committed.
- `experiments/data/int-4-visit-teacher-production-v1.json` and
  `experiments/int-4-visit-teacher-production.md`: compact checked-in receipt
  and honest report consumed by reviewers and Project/Wave judgment.

Existing smoke commands and receipts must keep validating unchanged.

## Absent and error states

- Missing, unreadable, wrong-arm, wrong-world, or wrong-hash Teacher-0 bytes:
  fail before calibration or run-directory evidence; never silently omit the
  control.
- Runtime, source, extension, content, ABI, protocol, Study schema, Python,
  Torch, host, or MPS mismatch: fail preflight and print the actual versus
  expected identity. Refresh a contract only before a production run, never to
  make an existing result fit.
- Missing/partial stage artifact or changed upstream hash: rerun that stage;
  never aggregate it as zero or skip the cell.
- Child failure, non-finite metric, empty latency sample, or competency error:
  preserve the diagnostic and mark the gate failed; no admission.
- Wall/core/artifact/RSS cap reached: stop launching new children, preserve
  completed artifacts, set `inconclusive_resource_cap`, and make no admission
  claim.
- Search cap hit: count it. A cap rate at or above 0.1% fails economics; it is
  not dropped from denominators.
- Missing Study field: encode it as unavailable under the schema. Never infer
  private information or invent client-side meaning.

## Operational boundary

- Hard cumulative caps remain 16 wall hours, 64 core hours, 4 GiB artifacts,
  and four workers for the entire production run, including resumes.
- All Python entry points run through `uv`; spawned Python children use the
  current uv-managed interpreter.
- Training uses the registered MPS device. Evaluation/search is CPU-only where
  required by the existing PUCT implementation.
- Rust is unchanged by design. If implementation changes `managym/src`, rebuild
  only with the repository's documented uv-managed maturin command, replace the
  cp312 extension, refresh production fingerprints before evidence, and rerun
  smoke plus production verification.
- Debug Rust tests are required before publication because CI exercises debug
  assertions.

## Verification target for pursue

Before starting the expensive run:

```bash
uv run pytest tests/sim/test_visit_iteration_runner.py \
  tests/sim/test_visit_teacher_production.py
cargo test --manifest-path managym/Cargo.toml
```

Then run the production and `--verify` commands above, update only the compact
receipt/report from verified raw artifacts, publish this serial PR, keep
auto-merge enabled, and use `lf pr land -c` only after production evidence is
complete, CI is green, required reviews are satisfied, and the Task's Done
When—not PR #133's smoke result—actually holds.

## Exclusions

- INT-3 or recovery of any INT-3 work.
- Retraining or reconstructing Teacher-0 controls.
- New ISMCTS, public-belief search, semantic encoder, Rules driver, card/rules,
  or Study UI behavior.
- Post-result tuning of budgets, seeds, temperature, optimizer, capacity,
  thresholds, competency definitions, or opponent selection.
- Cross-world ratings, a superhuman claim, or treating 16-game blocks as
  independent method seeds.
