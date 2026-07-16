# W2-234 interim Teacher-0 prefix snapshot

> **Pre-run status on 2026-07-16:** authorized by Project directive v8; not yet
> run. This is a bounded recovery experiment, not the 3,000-game
> preregistration and not Teacher-1 evidence.

## Question and claim boundary

Can the existing matched policy-only and joint policy/value students train
cleanly from an immutable prefix of the live Teacher-0 dataset while the
canonical 3,000-game generator continues unchanged?

Every result from this slice is **partial interim flat-determinized-Monte-Carlo
Teacher-0 evidence**. Its policy target is the existing score softmax, not MCTS
root visits. It cannot complete the full preregistration, satisfy Search Project
KRs 2-5, authorize Teacher-1, or authorize later distillation.

## Frozen configuration

- Source: the first 64 complete durable shards (indexes 0-63) from
  `search-supervised-overnight-recovery-20260716`, exactly 512 of the declared
  3,000 games. The source remains read-only and continues generating.
- Snapshot: copied atomically into
  `.runs/w2-234-teacher0-partial-v1/snapshot`; every NPZ and JSON sidecar is
  bound by SHA-256 along with the parent run fingerprint, explicit cutoff,
  dataset source, and trainer source.
- Student arms: `policy_only` (`value_weight=0`) and `policy_value`
  (`value_weight=1`). Both use score-softmax policy targets, terminal outcomes,
  the same snapshot, initialization seed 197, split, capacity, optimizer,
  learning rate 0.001, batch size 1024, 25 epochs, and MPS device.
- Evaluation: the fixed held-out game split produced by seed 197, 64
  seat-balanced games versus random per arm, and identical single/batched MPS
  inference measurements.
- Cap: 45 wall minutes total, checked before every epoch and arm. Exceeding it
  records `stopped_wall_cap`; it does not extend the run.

Predictions: both arms reduce held-out policy loss from their identical initial
state; the joint arm reaches held-out value Brier below 0.25 without losing
more than 10 points versus random relative to policy-only. Failure is diagnostic
only: policy failure points to optimization/data volume, value-only failure to
the outcome target or loss balance, and a cap failure to experiment economics.

## Commands

Freeze the declared prefix without writing to the source run:

```bash
uv run experiments/runners/run_teacher0_partial_snapshot.py \
  --stage freeze \
  --source-run /Users/jack/src/manabot.intelligence-overnight/.runs/search-supervised-overnight-recovery-20260716 \
  --snapshot-dir .runs/w2-234-teacher0-partial-v1/snapshot \
  --shard-count 64
```

Verify the copied snapshot, substituting the identity printed by `freeze`:

```bash
uv run experiments/runners/run_teacher0_partial_snapshot.py \
  --stage verify \
  --snapshot-dir .runs/w2-234-teacher0-partial-v1/snapshot \
  --snapshot-identity <SNAPSHOT_SHA256>
```

Run the matched bounded arms:

```bash
uv run experiments/runners/run_teacher0_partial_snapshot.py \
  --stage train \
  --snapshot-dir .runs/w2-234-teacher0-partial-v1/snapshot \
  --snapshot-identity <SNAPSHOT_SHA256> \
  --out-dir .runs/w2-234-teacher0-partial-v1/training \
  --epochs 25 \
  --batch-size 1024 \
  --lr 0.001 \
  --val-fraction 0.1 \
  --seed 197 \
  --policy-temperature 0.05 \
  --device mps \
  --evaluation-games 64 \
  --wall-cap-minutes 45
```

The checked-in result will replace `<SNAPSHOT_SHA256>` with the exact identity
and carry content-addressed checkpoint receipts, held-out metrics, gameplay,
latency, throughput, and total wall time. Raw copied shards and checkpoint
bytes remain under ignored `.runs/`; their cryptographic receipts are the
reviewable evidence.
