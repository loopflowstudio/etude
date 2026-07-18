# Compare Value Targets Inside the Visit-Trained Player

## Problem

INT-7 asks which value target produces the strongest complete visit-trained
player, not which target produces the prettiest calibration number. INT-8 has
removed the former capacity blocker by retaining the exact immutable INT-4
engineering-smoke bundle: four games, 507 visit-labeled rows, the two shard
files, the 5.8 MB trajectory/search audit, all four original checkpoints, and
the exact replay receipts. The retained payload identity is
`13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0`;
the trajectory audit is
`ae03c3bda06bdd65b090fefcaf1e23bb717c6f6566cc08731092c7911770f14f`.

The remaining question is deliberately narrow: with visit-distribution policy
supervision and every other training factor fixed, should the shared
policy/value manabot learn value from the eventual winner, the Teacher-1 root
estimate, or an equal blend of both? Each resulting checkpoint must be judged
as a complete PUCT player through the landed INT-6 arena authority. This is a
one-corpus engineering comparison, not an admission, promotion, method,
strength, or rating claim.

This advances the Intelligence measure that a practical student trains from
visit and value targets and then plays the actual matchup, and the measure that
teacher, student, policy-only, and student-plus-search are compared at declared
compute with legality, competencies, strength, calibration, latency,
throughput, cost, and uncertainty kept distinct.

## The demo

Run:

```bash
uv run python experiments/runners/run_int7_value_target_comparison.py \
  --input-manifest \
    experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/input-manifest.json \
  --out-dir .runs/int-7-value-target-comparison-v1/result
```

The command verifies the retained bundle without rewriting it, trains the four
matched arms, runs their complete 32-traversal PUCT players through the frozen
INT-6 smoke schedule, verifies every Command trace, and prints one bounded
decision naming the strongest point-estimate player plus whether the smoke
evidence separates it from the alternatives.

## Computable design contract

- **User-visible outcome:** the Intelligence researcher can run one command
  and observe which of policy-only, terminal-outcome, Teacher-1-root-value, or
  fixed-blend supervision produced the strongest complete visit-trained PUCT
  player at matched compute, together with an explicit separated-or-ambiguous
  smoke disposition. The result never presents itself as admission, promotion,
  method, rating, or general strength evidence.
- **End-to-end proof:** the documented run begins from the exact retained
  INT-8 input manifest, verifies and loads its 507 rows, trains twelve matched
  checkpoints, executes the complete 17-player/544-game INT-6 smoke matrix and
  128 matched roots, verifies every semantic Command trace, and writes one
  content-addressed result. The separate documented verify-only command must
  reproduce no bytes while validating the same complete evidence chain.
- **Source of truth:** the exact INT-8 retained payload is the only source of
  trajectory, visit, terminal, root-value, and inherited-cost facts. The
  frozen INT-6 contract is the only arena schedule, cohort, rating, competency,
  and profiling authority. The additive frozen INT-7 contract defines the
  experiment factors, caps, and decision rule; checkpoints, summaries, the
  report, and the decision are derived evidence.
- **Affected surfaces and consumers:** the implementation adds the INT-7
  contract, runner and verify-only CLI, value-target trainer support,
  neutral/learned checkpoint-leaf evaluation, additive arena registration,
  cumulative cap ledger, retained result, and experiment report. Existing
  INT-8 input verification, INT-6 rating/match/competency/profile/replay
  consumers remain compatible. Frozen INT-6 bytes and INT-9/managym authority
  consumers do not change.
- **Absent and error states:** a missing, added, changed, symlinked,
  regenerated, substituted, or rewritten retained input fails before training;
  an incomplete target row, mismatched training factor, missing checkpoint,
  illegal action, private exposure, root mutation, replay mismatch, malformed
  schedule, artifact tamper, or verification drift fails closed. A projected
  cap overrun emits typed `resource_cap_exceeded` and retains partial
  diagnostic receipts. Broad uncertainty with otherwise valid evidence is a
  successful `retain_point_winner_but_smoke_ambiguous` result, not a failure.
- **Operational boundary:** the complete registered schedule is fixed at
  twelve checkpoints, 136 matchup cells/544 games, thirty competency rows per
  target method, and 128 measured roots, under cumulative hard caps of six wall
  hours, twenty-four core hours, four workers, and 2,147,483,648 artifact
  bytes. The runner checks a persistent ledger before every stage or worker
  launch and never expands a cap or silently reduces the schedule.
- **Exclusions:** no new teacher labels, corpus regeneration, blend tuning,
  INT-6 production admission, promotion or general-strength claim, INT-9
  belief work, managym/Rules/Game/Study authority change, conditional search,
  new content, or new planner family belongs to INT-7.

## Approach

### 1. Freeze the experiment before training

Add `experiments/contracts/int-7-value-target-comparison-v1.json` before the
first training run. It binds:

- the exact retained input manifest, payload, shards, trajectory audit, and
  loader identities;
- the frozen INT-6 contract file byte identity
  `fc9cb76c0d80ad64951455ac6fede94b1355f383dde2f2964d0e578f62671a71`,
  arena key, five-anchor cohort, rating prior, smoke deal seeds, smoke
  competency seeds, and matched-root selection;
- the current runtime and a closed additive INT-7 source bundle, without
  modifying the INT-6 contract;
- the four arms, three training seeds, target formula, optimizer, architecture,
  split, batch order, PUCT compute class, comparison-seed aliases, decision
  rule, and exact cumulative hard caps: `wall_hours=6.0`,
  `core_hours=24.0`, `workers=4`, and
  `artifact_bytes=2147483648`;
- `engineering_smoke_only_no_admission_claim` as the only allowed evidence
  class.

The runner maintains a persistent cumulative hard-cap ledger and checks it
before every stage or worker launch. If the next action could exceed any cap,
the runner stops fail-closed with typed `resource_cap_exceeded` and retains all
partial diagnostic receipts. It never extends a cap and never silently shrinks
the registered training, 544-game, competency, or profiling schedule to fit.

The runner calls the existing INT-8 retained-input verifier before loading any
shard and again after finalization. It rejects any changed path, byte count,
digest, loader result, symlink, added file, missing file, conversion,
regeneration, substitution, or output written beneath the retained bundle.
New checkpoints and evidence live only under the requested output directory.

### 2. Train one matched four-arm block per seed

Use training seeds `197`, `198`, and `199`. These are independent model
initializations and batch orders over one retained teacher/data seed; they do
not turn the corpus into multi-data-seed evidence.

All arms use:

- all retained rows selected by the same fixed game split;
- split seed `197`, so data volume and the held-out game never vary with model
  initialization;
- visit counts normalized over the legal mask as the policy target;
- the default 102,722-parameter `Agent` architecture and observation space;
- identical initial model bytes within each seed block;
- Adam with learning rate `1e-3`, default betas/epsilon, batch size `256`, ten
  fixed epochs, CPU execution, one Torch thread, and no early stopping;
- identical per-seed row order and batch boundaries across arms;
- policy loss weight `1.0`; joint value loss weight `1.0`.

The arms are:

| Arm | Value supervision | Serving leaf value |
| --- | --- | --- |
| `visit_policy_only` | none (`value_weight=0`) | neutral `0.5` |
| `visit_terminal` | deciding player's terminal win indicator | checkpoint value |
| `visit_teacher_root` | retained Teacher-1 `root_value` | checkpoint value |
| `visit_blend_50_50` | `0.5 * terminal + 0.5 * teacher_root` | checkpoint value |

The blend is fixed in probability space and is not tuned after results. Every
row has one terminal target and one finite root target. The trainer records
target-array SHA-256, train/validation index SHA-256, batch-order SHA-256,
initial-state SHA-256, optimizer identity, final model-state SHA-256, checkpoint
SHA-256, elapsed seconds, and examples per second. A matched-factor receipt
must show that only value-target identity/value weight differ within a seed
block.

### 3. Make the PUCT comparison genuinely compute-matched

Add an additive checkpoint evaluator with two declared modes:

- `neutral`: execute the same checkpoint forward pass for legal-action priors
  but replace the unsupervised value with `0.5`;
- `learned`: use the checkpoint priors and sigmoid value, converting from the
  node player's perspective to the root player's perspective exactly as the
  existing `AgentLeafEvaluator` does.

Both modes therefore perform one model forward per expanded nonterminal node.
Terminal nodes still use the exact winner. This avoids two invalid controls:
using an arbitrary untrained value head for policy-only, or giving policy-only
expensive random terminal playouts while joint arms use cheap network leaves.

Every arena candidate uses the same declared compute class:

- determinized PUCT;
- 32 total traversals across four worlds;
- `c_puct=1.5`;
- no root noise;
- maximum 2,000 steps;
- CPU, batch size one;
- `full_clone/current_game_v1` branch authority;
- acting-viewer observation/history only;
- the same per-training-seed comparison alias, so target arms receive paired
  search randomness.

The policy-only arm uses neutral mode; joint arms use learned mode. A separate
matched-root diagnostic also runs each joint checkpoint in neutral mode on the
same 128 INT-6 roots. This decomposes shared-encoder policy spillover from the
online contribution of the learned value without doubling the gameplay
cohort.

### 4. Evaluate the complete players through INT-6

Use the INT-6 smoke profile byte-for-byte: two deal blocks, both seat legs,
two competency seeds, the five frozen code-only anchors, the frozen rating
model/prior, and the 128-root isolated profiler. Do not edit the INT-6 contract
or its ownership rules.

The cohort contains the five anchors plus twelve seed-specific candidates.
Run the complete connected matrix, including every anchor-anchor,
candidate-anchor, and candidate-candidate cell. At two deals and two seat legs,
that is 136 cells and 544 games. Retain:

- every per-game row and the full payoff matrix;
- Gaussian-MAP diagnostic ratings, global deal-block bootstrap ranges,
  connectivity, residuals, and per-seat results;
- S1-S5 competency runs and correct-line aggregates;
- illegal action, root mutation, truncation, private exposure, offer binding,
  Command fabrication, and exact replay counters;
- per-player p50/p95 isolated latency, decisions/s, nodes/s, simulations/s,
  peak RSS delta, and contended gameplay throughput;
- per-root priors, visits, root value, action, nodes, latency, and neutral versus
  learned action/value deltas.

`search value per node` and `search value per wall-clock second` are reported as
the same-seed rating and paired-score uplift over `visit_policy_only`, divided
by isolated mean nodes per decision and isolated CPU seconds per decision.
These are diagnostic efficiency ratios on this matrix, not transferable
strength units; the unnormalized rating, payoff cells, and cost remain primary.

### 5. Keep calibration subordinate and correctly labeled

For every checkpoint, score predictions against all three target sources on
the fixed held-out game:

- terminal outcome;
- Teacher-1 root value;
- the fixed 50/50 blend.

Report count, mean prediction, mean target, Brier score, binary cross-entropy,
ten-bin reliability rows, and expected calibration error overall and for each
encoded phase: beginning, precombat main, combat, postcombat main, and ending.
Sparse or empty phase bins retain their count and become `insufficient_n`; they
are never silently pooled. Terminal-outcome reliability is calibration.
Root/blend results are explicitly target-source agreement because those soft
teacher targets are not ground truth.

The policy-only checkpoint is scored post hoc against the same target sources
even though its value head is not used in play. Calibration or Brier never
selects the winner.

### 6. Decide from complete-player strength

Rank the four target methods by the mean of their three seed-specific
diagnostic ratings in the complete matrix. Break exact ties, in order, by:

1. mean paired score against the common five anchors;
2. within-seed head-to-head score;
3. total S1-S5 correct count;
4. lower isolated p95 latency;
5. `visit_policy_only`, `visit_terminal`, `visit_blend_50_50`, then
   `visit_teacher_root` as a final deterministic order.

Always name that point-estimate winner. Separately issue a scale decision:

- `continue_<arm>` only if integrity and replay are perfect, the arm is
  competency-noninferior to policy-only, and its mean paired-score advantage
  over every other arm is at least `0.05`;
- otherwise `retain_point_winner_but_smoke_ambiguous`;
- any integrity failure yields `kill_invalid_evidence` regardless of strength.

Competency noninferiority is one preregistered aggregate gate. For each
value-target method, sum correct-line outcomes across its three model
checkpoints, five scenarios, and two frozen competency seeds: exactly 30 binary
rows. Compare that sum with the corresponding 30-row aggregate for
`visit_policy_only`; the method passes when the difference is nonnegative, so
ties pass. Retain and report deltas by model seed and by scenario as diagnostics
only; neither becomes an additional or post hoc gate.

The decision payload states that no arm is promotion/admission eligible and
that one 507-row teacher/data seed cannot support a method-level claim.

### 7. Preserve complete cost provenance

Propagate the original retained teacher/search label cost and 507-label count
from the INT-4 manifest instead of treating reused labels as free. Add marginal
INT-7 target construction, training seconds, examples/s, arena wall/core time,
artifact bytes, and verification time. Report inherited label cost, marginal
training cost, and evaluation cost separately and in total.

Retain the verified result under
`experiments/data/int-7-value-target-comparison-v1/sha256/<manifest-sha256>/`
and write `experiments/int-7-value-target-comparison.md` with the bounded
result, exact command, identities, full matrix links, cost, and decision.

## De-risking

| Question | Finding | Impact on design |
| --- | --- | --- |
| Is the old unavailable-input blocker actually removed? | The INT-8 manifest closes the exact 13-file payload and binds both shard digests, the 5.8 MB audit digest, four checkpoint digests, 507 rows across four games, and the 175-decision exact replay receipt. | The runner accepts only that manifest and verifies it before and after all derived work. |
| Are terminal and root targets meaningfully different? | The 507 rows contain nine root-value levels from `0.0` to `1.0`; only 101 rows equal terminal outcome. Root versus terminal Brier is about `0.215`, correlation about `0.420`, and mean absolute difference about `0.372`. | The three targets are non-degenerate. The fixed 50/50 blend is a real third treatment, not an alias. |
| Can phase-stratified evaluation be recovered without changing the corpus? | The canonical observation already contains exact phase one-hots. The retained rows cover beginning 35, precombat main 283, combat 123, postcombat main 63, and ending 3. | Decode the frozen observation feature; do not add or regenerate metadata. Mark sparse phase results explicitly. |
| Can a learned value run inside current PUCT authority? | Existing `AgentLeafEvaluator` already consumes checkpoint priors/value, preserves the root, executes through `full_clone/current_game_v1`, and emits visits, values, nodes, worlds, and branch receipts. | Add only the neutral-value mode and arena registration seam; do not create a second search implementation. |
| Is equal traversal count also equal work? | Random terminal leaves and network leaves are not comparable per traversal. A checkpoint forward with neutral `0.5` and the same forward with learned value are comparable. | Policy-only and joint arms use identical forward-count semantics at 32 traversals; random terminal leaves are excluded from the primary comparison. |
| Does joint value training leave policy fixed? | The policy targets are fixed, but the shared encoder means value gradients can change policy logits. | Measure policy KL/action agreement and run each joint checkpoint in neutral and learned modes on matched roots. Judge the complete player while exposing the mechanism. |
| Does standard AlphaZero settle the target choice? | AlphaZero trains value from eventual game outcome, while later planning systems also use bootstrapped value targets; neither determines which noisy target is best in this retained imperfect-information smoke corpus. | Treat terminal, root, and blend as empirical alternatives. Do not import an external method claim. |
| Can the frozen INT-6 contract describe new checkpoint players? | Its bytes and authority must remain unchanged; INT-8 already established the additive-contract pattern for new diagnostic candidates. | Bind INT-6 byte-for-byte and put new implementation identities in the INT-7 contract and result manifest. |

## Alternatives considered

| Approach | Tradeoff | Why not |
| --- | --- | --- |
| Reuse the retained seed-197 root-value checkpoint and train only missing targets | Less marginal training. | It would leave only one model seed and would not prove that all arms share current initialization, split, batch order, optimizer, and epochs. |
| Compare validation calibration only | Cheapest and statistically tidy. | It answers the wrong product question; calibration does not establish that the value helps the complete search player. |
| Use random terminal leaves for policy-only and checkpoint values for joint arms | Gives policy-only a familiar complete evaluator. | Traversals have radically different cost and semantics, so matched simulations would not be matched compute. |
| Use the policy-only checkpoint's untrained value head | Preserves one checkpoint forward per node. | Arbitrary initialization would be an unstable control. Neutral `0.5` preserves cost without injecting random value preferences. |
| Run the INT-6 production profile | More games and competency seeds. | One retained teacher/data seed cannot justify admission-scale spend or claims. The frozen smoke profile is the honest first comparison. |
| Tune the blend coefficient on validation Brier | Could improve calibration. | It would make the third arm post hoc and optimize the subordinate metric. The coefficient stays preregistered at 0.5. |

## Key decisions

- Use three model seeds but call out the single teacher/data seed everywhere.
- Train all arms anew as derived artifacts; never overwrite or substitute any
  retained INT-8 byte.
- Fix the split independently from model seed so every arm sees identical row
  volume.
- Use ten fixed epochs to make value supervision visible without early-stopping
  selection; the training cost is still negligible relative to gameplay.
- Use neutral-value checkpoint PUCT as the policy-only control so node and
  inference work match the learned-value arms.
- Evaluate the complete 17-player smoke matrix rather than cherry-picking
  opponents.
- Name a point-estimate winner even when uncertainty is broad, but separate it
  from the stricter continue/ambiguous decision.
- Treat terminal Brier as calibration and root/blend Brier as imitation
  agreement.
- Preserve INT-6 and INT-9 authority: no changes under `managym/`, no changes to
  the INT-6 contract, and no edits to belief, possible-world, materialization,
  or semantic-Command kernels.

## Success and failure modes

Wild success is a target that wins across model seeds, lifts competencies or
rating at the same 32-traversal compute, remains fast, and whose neutral/learned
root ablation shows that value—not accidental policy drift—caused the gain.
That produces a concrete value recipe for the next retained multi-seed corpus.

Wild failure is a visually impressive Brier improvement with no arena lift, or
a target ranking driven by one deal, one initialization, sparse ending-phase
rows, shared-encoder policy drift, or the quantized eight-traversal teacher
estimate. The full matrix, per-seed outputs, root ablation, target-source
labels, and ambiguous-smoke disposition make those failures explicit rather
than promotable.

## Scope

- In scope: exact retained-input verification; three value targets plus a
  policy-only control; matched visit-policy training; neutral/learned
  checkpoint PUCT; INT-6 smoke matrix, competencies, rating, replay, profiling,
  calibration, cost, decision, report, and retained artifact.
- Out of scope: new teacher games or labels; corpus regeneration; target tuning;
  INT-6 production admission; promotion or strength claims; INT-9 beliefs;
  Rules/Game authority changes; conditional strategy, Study evidence, new
  cards, new worlds, or a new planner family.

## Done when

The documented command completes from the exact retained manifest and its
self-verification reports:

- unchanged INT-8 input identities before and after the run;
- twelve loadable checkpoints with matched-factor receipts;
- 544 seat-paired arena games and a complete 17-player payoff matrix;
- perfect legality, viewer safety, semantic Command binding, root preservation,
  and exact replay;
- S1-S5, diagnostic ratings, uncertainty, residuals, p50/p95 latency,
  throughput, RSS, search-efficiency, phase/target-source Brier, and complete
  cost fields;
- a point-estimate winner plus `continue_<arm>`,
  `retain_point_winner_but_smoke_ambiguous`, or `kill_invalid_evidence`;
- `promotion_eligible=false`, `admission_eligible=false`, and
  `method_level_claim=false`.

Exact run command:

```bash
uv run python experiments/runners/run_int7_value_target_comparison.py \
  --input-manifest \
    experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/input-manifest.json \
  --out-dir .runs/int-7-value-target-comparison-v1/result
```

Exact verify-only command over the completed output directory:

```bash
uv run python experiments/runners/run_int7_value_target_comparison.py \
  --out-dir .runs/int-7-value-target-comparison-v1/result \
  --verify-only
```

Verify-only regenerates nothing. It validates the before/after INT-8 payload
identities, frozen INT-6 contract SHA-256
`fc9cb76c0d80ad64951455ac6fede94b1355f383dde2f2964d0e578f62671a71`,
matched-factor receipts, persistent cumulative cap ledger, every evidence
artifact, the exact 544-game schedule, and exact Command replay.

Verification before review:

```bash
uv run pytest \
  tests/sim/test_search_supervised.py \
  tests/arena/test_guidance.py \
  tests/arena/test_int7_value_targets.py \
  tests/sim/test_int7_value_target_runner.py
cargo test
```

## Measure

The primary measure is complete-player diagnostic rating at identical
32-traversal/four-world checkpoint-PUCT compute, supported by the full payoff
matrix and paired score. Competencies and integrity are gates. Secondary
measures are search uplift per node/second, p50/p95 latency, nodes/s,
decisions/s, gameplay throughput, peak RSS, policy drift, terminal calibration,
root/blend target agreement, and inherited plus marginal cost. No metric from
this one-corpus smoke run is an admission or method claim.
