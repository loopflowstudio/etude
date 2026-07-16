# W2-214 semantic-program transfer probe

Status: **PASS**. Decision: **positive_transfer**.

Run with:

```sh
PYTHONHASHSEED=0 uv run scripts/run_semantic_transfer.py \
  --workload experiments/workloads/semantic-transfer-v1.json \
  --out experiments/data/semantic-transfer-v1.json \
  --report experiments/semantic-transfer-v1.md
```

## Claim boundary

This probe does not measure gameplay strength, PPO transfer, search strength, or win rate.
This result does **not** close Rules Semantic KR6. It is a causal
capability probe over admitted ability programs, not gameplay or win-rate
transfer. The production policy/action ABI is unchanged.

## Four-arm result

| Arm | input | head | zero-shot heldout (95% CI) | limited retrain (95% CI) |
|---|---|---|---:|---:|
| `card_id_legacy` | opaque CardDefId | legacy fixed | 0.0% [0.0%, 3.8%] | 100.0% [96.2%, 100.0%] |
| `card_id_structured` | opaque CardDefId | structured | 0.0% [0.0%, 3.8%] | 100.0% [96.2%, 100.0%] |
| `semantic_card_id_structured` | program + CardDefId | structured | 8.3% [4.3%, 15.6%] | 100.0% [96.2%, 100.0%] |
| `semantic_only_structured` | program only | structured | 91.7% [84.4%, 95.7%] | 100.0% [96.2%, 100.0%] |

The same training programs, candidate permutations, model seeds,
optimizer budget, held-out rows, deck split, and seat duplication are used
for every arm. Card identity slots are hash-ordered opaque symbols, not IR
rows, and the held-out identity embeddings receive no zero-shot updates.

## Robustness and capacity

- Program tokens: p50 29, p95 64, max 75 against an exact 75-token budget; 0 overflows and 0 silent truncations.
- ContentPack reorder + checkpoint rebind: 100.0% exact.
- Token-kind/opcode numeric-ID permutation: 100.0% exact.
- Unknown opcodes and changed enum-domain schemas fail closed.
- The live structured frontier represents 64 choices/branches and remains legacy-adapter equivalent.

## Performance

Latency, throughput, parameter bytes, and process peak RSS are recorded
for every arm/seed in the JSON receipt. RSS is the shared-process high-water
mark, so it is suitable as a reproducibility receipt, not an isolated model
memory comparison.

## What remains

- Join the semantic encoder and structured decoder to PPO/all admitted decision families.
- Add enum-domain identity to the projection before claiming arbitrary schema-table rebinding.
- Run held-out gameplay and win-rate transfer after that join.

The enum follow-on is structural: v1 emits one flat `enum` token without
its source domain. This experiment can safely rebind token-kind and opcode
IDs, but arbitrary enum-table permutation would be ambiguous and is
therefore rejected rather than counted as robustness.
