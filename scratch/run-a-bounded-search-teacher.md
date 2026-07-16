# W2-234: Teacher-1 admission evidence

## Problem

W2-234 asks whether search on the current w2 observation/legal-action ABI can
produce useful policy and value supervision. PR #105 landed Teacher-0 flat
determinized Monte Carlo, PR #106 landed a deterministic multi-world PUCT
Teacher-1 substrate, and PR #108 landed incremental crash-safe distillation
datagen at commit `df01e1749f7095380bf0a37858be196605081ca5`.

Those merged substrates are not the missing evidence. The live Teacher-0 run
result is still pending, no immutable control lock exists, and Teacher-1 has no
bounded three-budget quality result. This PR therefore owns only the Teacher-1
admission instrument and its pre-registration. It must neither restart nor
mutate the live recovery run, and it must not train a student.

This advances the Intelligence measure requiring a current-ABI, attributable
search teacher while preserving the wave's evidence discipline: competency,
legality, information safety, cost, and uncertainty are gates before
distillation or strength promotion.

## The demo

Once the live recovery has a terminal manifest and a separately checked-in
control lock, a developer runs:

```bash
uv run experiments/runners/run_teacher1_pilot.py \
  --contract experiments/contracts/w2-234-teacher1-pilot-v1.json \
  --control-lock experiments/contracts/w2-234-teacher1-control-lock-v1.json \
  --stage teacher-gate \
  --out-dir .runs/w2-234-teacher1-pilot-v1
```

The runner resumes completed cells, reports three independent 16-game seed
blocks and their fixed 48-game aggregate for every matchup, exactly replays
eight predeclared MCTS roots, and ends in `completed_pass` or `completed_kill`.
Only `completed_pass` authorizes a later three-training-seed student experiment.

## Approach

### Frozen identities and control lock

The machine-readable contract pins world, engine source and binary, content,
observation/action ABI, experience protocol, matchup, host/runtime, Teacher-1
algorithm, budgets, seeds, caps, predictions, gates, and branch table. The
runner hashes its own evaluation source and refuses any mismatch.

The contract intentionally cannot name a current control artifact. A separate
control lock must bind the canonical contract hash to:

- the terminal live-recovery manifest and exact SHA-256;
- the recovery's preselected `policy_value` checkpoint and exact SHA-256;
- a same-host latency calibration artifact;
- one Teacher-0 playouts-per-action value for each Teacher-1 budget, with
  measured p50 decision latency within 10%.

The lock is impossible while the live recovery is nonterminal. Missing or
mismatched lock data fails before the output directory or search work exists.

### Viewer-safe audit and exact replay

The authoritative environment remains the only state transition owner. Each
Teacher-1 audit decision records:

- acting-viewer protocol frame and legal `InteractionOffer` set, with opponent
  private hand redacted;
- revision-bound chosen `Command`, actor/seat, and opponent class;
- total root visits, Q values, root value, selected action, tree nodes/depth,
  cap hits, exact player/call/search seed, and encoded legal cardinality;
- contract, control-lock, runtime, teacher, deal, and terminal provenance in
  the linked audit bundle.

Replay reconstructs each game from the fixed deal seed, regenerates every
viewer-safe frame and command surface, reapplies the command sequence, and
checks the terminal outcome. It additionally reruns these eight fixed roots:

| Audit game | Decision indices |
|---:|---:|
| 0 | 0, 8 |
| 1 | 0, 8 |
| 2 | 0, 8 |
| 3 | 0, 8 |

Each rerun uses the recorded call seed whose derivation is pinned by the hashed
Teacher-1 source. Selected action, integer visit vector, float32 Q vector,
root value, nodes, depth, and cap hits must match exactly. A missing root or any
mismatch is a hard failure. State digests before and after every search must
also match, proving the search never mutated the authoritative root.

### Three-budget quality gate

Teacher-1 is fixed as uniform-prior, random-leaf, separately determinized PUCT:

| ID | Total traversals | Worlds | Traversals/world |
|---|---:|---:|---:|
| `t1-8-w4` | 8 | 4 | 2 |
| `t1-32-w4` | 32 | 4 | 8 |
| `t1-128-w4` | 128 | 4 | 32 |

Teacher-0 and Teacher-1 `sims` do not share units. Teacher-0 counts playouts
per legal action; Teacher-1 counts total adaptive traversals across worlds.
Controls are therefore matched by realized p50 latency, with raw budgets and
realized gaps still reported.

Every budget/control matchup cell remains exactly 48 seat-balanced games. It is
split into the same three common-random-number blocks for paired inspection:

| Block | Exact seed | Games |
|---|---:|---:|
| `matchup-197` | 1197 | 16 |
| `matchup-419` | 1419 | 16 |
| `matchup-887` | 1887 | 16 |

Each block gets its own strength interval, latency/search accounting, and wall
time; the top-level cell aggregates the fixed 48 games. These are independent
evaluation/search seeds, not independently trained methods. The split adds no
games.

Root-label stability uses 192 roots driven by stream seed 2197. Every root and
budget is searched at the three exact common-random-number seeds 2197001,
2197002, and 2197003. This replaces an opaque derived seed formula without
changing the 192 × 3 × 3 search workload.

The remaining battery is 100 runs per S1-S5 competency cell, four audit games,
random and frozen-checkpoint controls, matched-wall Teacher-0, and a direct
`t1-128-w4` versus `t1-8-w4` matchup.

### Admission and continuation

Teacher quality passes only when all boundary and target invariants pass plus:

- `t1-128-w4` wins at least 55% against `t1-8-w4`, at least 55% against the
  frozen checkpoint, and at least 80% against random;
- repeated-search top-action agreement is at least 70% and median JSD at most
  0.10;
- mean nodes and depth grow from 8 to 128 traversals;
- at least one of S1/S2/S5 reaches 20%, with no S1-S5 result more than ten
  points below random;
- p95 is at most 500 ms, throughput at least two labels/s/worker, cap hits below
  0.1%, and every realized matched-control p50 gap at most 10%.

Root-value Brier score, ECE, and reliability bins against terminal outcomes are
diagnostics, not hard gates: the root value evaluates random-leaf continuation
while the realized game follows Teacher-1.

On pass, the next serial PR may freeze `t1-128-w4` and compare normalized visits
with one-hot chosen actions at training seeds 197, 419, and 887. On any failure,
the contract's named branch applies and no student training starts.

## Evidence-backed decisions and assumptions

| Classification | Statement | Design consequence |
|---|---|---|
| Evidence | PRs #105/#106/#108 are merged; current main contains Teacher-0, deterministic PUCT, and resumable datagen. | The PR does not duplicate those substrates. |
| Evidence | Teacher-1 code emits seeded real visits/Q/value and focused tests prove deterministic root isolation. | Exact replay can rerun the existing implementation; no new search algorithm is needed. |
| Evidence | Teacher-0 and Teacher-1 budget units differ. | Matching is by pinned realized latency, never equal integers. |
| Evidence | No control lock or bounded Teacher-1 result is checked in. | This PR makes no teacher-quality or distillation claim. |
| Assumption | The live run will eventually produce a terminal manifest and the preselected `policy_value` checkpoint. | If false, the control lock cannot be created and the gate stays blocked. |
| Assumption | The declared Apple M4 Max remains available and uncontended for calibration/evaluation. | Runtime identity and realized matching fail closed if it changes. |
| Assumption | A 3 × 16 game split is useful admission evidence. | Report blocks and aggregate honestly; do not treat them as method-level training seeds. |

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Can frame replay establish MCTS provenance? | No. Frame/command/outcome replay says nothing about search outputs. | Exact sampled MCTS reruns are a separate hard receipt. |
| Can equal `sims` values match compute? | No; Teacher-0 is per action and Teacher-1 is total across worlds. | Same-host p50 calibration and realized 10% gate are mandatory. |
| Does determinized PUCT become information-set safe? | No. It builds separate trees per determinization and retains strategy-fusion risk. | Name it precisely; require competency and viewer-safety gates; make no exploitability claim. |
| Do three 16-game blocks equal three trained seeds? | No. They measure evaluation/search stochasticity for one fixed method. | Report each block and aggregate; reserve method uncertainty for later training seeds. |
| Does exact replay expand the experiment materially? | Eight 128-traversal reruns add exactly 1,024 traversals and no games. | Keep the existing 8 wall-hour / 32 core-hour / 2 GiB caps. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Count frame/command replay as full provenance | Cheap, but leaves MCTS outputs unaudited. | Rejected by the exact search provenance requirement. |
| Increase to 3 × 48 games | Cleaner per-seed intervals but triples matchup cost. | The review explicitly requires three blocks without increasing 48 total. |
| Derive a unique repeat seed from root ordinal | Compact contract but opaque and easy to change accidentally. | Three exact shared repeat seeds are auditable and preserve common random numbers. |
| Distill before the teacher gate | Produces checkpoints sooner but confounds weak labels with optimization/capacity. | Distillation remains forbidden until all admission gates pass. |

## Key decisions

- PR #108 is merged infrastructure; only its live result/control lock is
  pending.
- Matchup evidence is 3 × 16 games at exact seeds, not 48 games under one base
  seed and not 144 games.
- Stability searches use three listed seeds at every root and budget.
- Eight audit roots rerun MCTS exactly; command replay alone cannot pass.
- The control checkpoint arm is preselected as `policy_value` before reading
  its result.
- No student data generation, training, student-plus-search, or semantic input
  work belongs in this PR.

## Scope

- In scope: immutable contract; control-lock validation; three-budget matchup,
  stability, competency, latency, calibration, and replay evaluator; focused
  tests; pre-registration report.
- Out of scope: touching the live recovery; creating its control lock before it
  is terminal; running the bounded gate; training students; semantic programs;
  BranchDriver scaling; neural PUCT; public-belief/CFR treatment.

## Done when

- `uv run pytest tests/sim/test_teacher1_evidence.py tests/sim/test_teacher1_pilot.py tests/sim/test_mcts.py -q` passes.
- A missing/nonterminal/mismatched control lock fails before search or output.
- Tampering any sampled root's Q vector causes exact replay failure.
- Contract inspection shows three 16-game blocks, eight replay roots, three
  exact stability seeds, and future training seeds 197/419/887.
- PR #110 contains only this instrument/design revision and W2-234 remains open
  for the `teacher1-evidence` serial continuation.

## Measure

Before: two-game Teacher-1 plumbing smoke; no three-budget strength curve, no
sampled search replay, one opaque matchup seed, derived repeat-search seeds.

After the future run: 8/32/128 strength/cost curve with three reported seed
blocks per cell, exact replay receipts for eight roots, five competency scores,
label stability, value calibration diagnostics, realized matched-compute gaps,
and an explicit pass/kill decision. This PR builds and freezes that instrument;
it does not supply the result.
