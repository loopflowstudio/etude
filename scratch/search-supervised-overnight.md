# Search-supervised overnight experiment

## Question

Does joint policy/value supervision extract more useful gameplay signal from
the existing search teacher than policy-only distillation, without waiting for
semantic-program inputs or claiming that flat determinized Monte Carlo is
MCTS?

This is the first experiment in the Intelligence Project **Search Teacher and
Distillation Loop**. The separate **Semantic Representation Katas** Project
must prove program order, hierarchy, argument binding, and recombination in
small controlled exercises before semantic features enter this gameplay run.

## Why this experiment

The repository already records, at every teacher decision:

- the exact viewer observation consumed by the policy;
- the legal action mask and selected teacher action;
- one search score per legal action;
- the deciding seat and terminal winner;
- teacher, seed, round, checkpoint, and source-commit provenance.

However, `train_bc` trains only the policy head. The Agent's value head is
trained later in a separate pass, so the shared encoder never receives the
KataGo-shaped joint policy/value objective. Also, the current teacher evaluates
every root action with an equal number of independent rollouts. Its score
softmax is a useful policy distribution, but it is not an MCTS visit
distribution and must not be named one.

## Minimal ablation

Use one deal-diverse search dataset and identical seeds/hyperparameters for two
arms:

1. `policy_only`: score-distribution cross-entropy, value loss weight 0;
2. `policy_value`: the same policy loss plus terminal-outcome BCE on the value
   head and shared encoder.

This isolates the auxiliary value objective. It does not mix in semantic
features, a structured decoder, a different teacher, or a different data
distribution.

## Pre-registered diagnostics

- Teacher signal: current-world search teacher must beat random in a small
  seat-balanced probe before a long dataset is trusted.
- Policy learning: held-out policy cross-entropy must improve over its initial
  epoch and nontrivial top-1 accuracy must exceed uniform-over-legal.
- Value learning: `policy_value` must beat a constant 0.5 predictor on held-out
  Brier score (`< 0.25`). `policy_only` is expected to stay near chance.
- Gameplay: both students are judged against random from identical evaluation
  seeds. The joint arm must not lose more than 10 percentage points to the
  policy-only arm; improvement is evidence, not a precondition.
- Teacher/student gap: report student versus teacher on a smaller
  seat-balanced cell. Do not infer teacher recovery from validation accuracy.

If teacher strength fails, improve search before training. If policy fails but
teacher succeeds, inspect target entropy/representation/optimization. If only
value fails, change value targets or loss weighting. If validation improves but
gameplay does not, treat distribution shift as the leading diagnosis.

## Implementation

- Add a joint trainer that consumes the existing score and winner columns.
- Split by complete games once and share that split across both objectives.
- Train policy cross-entropy against a masked score-softmax target.
- Train value BCE from the decision maker's perspective; winner-less rows do
  not contribute value loss.
- Log policy CE/top-1/nontrivial accuracy, value BCE/Brier/accuracy, target
  entropy, exact hyperparameters, dataset hashes/provenance, wall time, and
  evaluation cells.
- Provide one `uv run` entry point that can generate data, train both arms,
  checkpoint them, evaluate them, and atomically write a resumable manifest.

## Follow-on branch, not silently included

A real KataGo-like search target requires adaptive tree search (PUCT or an
explicit alternative), per-edge visit counts, a policy/value-guided leaf
evaluation, and hidden-information semantics. That is the next diagnostic
experiment after the joint learner is sound. The current run is the honest
policy/value data-and-training substrate it will plug into.

## Bounded pilot result (2026-07-15)

The end-to-end preflight used 100 self-play games, search-16, 10 epochs per
arm, and 100-game policy evaluations. It completed in 174 seconds and passed
every pre-registered gate:

- teacher versus random: 95% wins (19/20; Wilson 95% CI 76.4–99.1%);
- dataset: 11,886 decisions, 2.93 mean legal actions, zero winner-less rows;
- policy-only: held-out policy CE 0.9754 → 0.9665, nontrivial accuracy
  37.0% → 54.0%, and 71% wins versus random;
- joint policy/value: policy CE 0.9754 → 0.9668, nontrivial accuracy
  37.0% → 54.4%, value Brier 0.2563 → 0.2043, and 73% wins versus random;
- student versus teacher remained weak (1/10 policy-only and 2/10 joint), so
  this pilot proves the training substrate and value-learning signal—not
  teacher recovery or superhuman play.

The causal result is narrow but useful: terminal-value supervision materially
improved value prediction and did not measurably damage the distilled policy.
The overnight run scales data and teacher compute to determine whether the
remaining student/teacher gap is primarily sample-limited. The exact pilot
artifacts remain under the ignored local directory
`.runs/search-supervised-pilot/`.

## Done when

- Unit tests prove joint gradient flow, value label perspective, score masking,
  checkpoint round-trip, and deterministic game-level splits.
- A bounded smoke run generates fresh decisions, trains both arms, and writes a
  complete manifest with no NaNs or invalid actions.
- The overnight command is launched only after the smoke gates pass.
- Morning artifacts contain the dataset, checkpoints, training histories,
  teacher/student evaluation, provenance, and explicit experiment verdict.
