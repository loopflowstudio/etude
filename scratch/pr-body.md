## Try it!

```bash
uv run experiments/runners/run_semantic_runtime_policy.py \
  --out-dir .runs/int-11-semantic-runtime-policy-v1
```

This regenerates 24 authoritative runtime decisions, trains three matched arms
at seeds 1101/1102/1103, loads and verifies nine checkpoints, evaluates the
holdouts and 35-target/64-attacker frontiers, benchmarks inference, plays 36
paired-seat w2 games, and independently replays every retained Command.

The checked run produced 36/36 terminal games, 144 accepted Commands, zero
illegal Commands, zero replay mismatches, and zero viewer-private feature
mismatches. The result is intentionally not a semantic win: semantic transfers
on the identity holdout where identity-only does not, but structure-shuffled
matches semantic and composition transfer is seed-noisy. The checked verdict
is `null_or_ambiguous_structure_evidence`.

## Intent

Build the narrowest learned manabot above INT-2's real viewer-safe runtime join
instead of another static semantic kata. The policy consumes ExperienceFrame
facts, typed ability programs, visible bindings, and authoritative ragged
offers, then emits ordinary structured Commands through managym.

## Assumptions

- The bounded priority/Igneous-target/combat workload is a useful first
  executable semantic-policy slice, not general card-play coverage.
- The Otter-Penguin terminal combat fixture proves complete paired execution
  but is too simple to rank the arms.
- Three training seeds are the method-level unit; evaluation rows and paired
  games are not additional training replicates.
- Cached immutable definition representations are the serving latency
  boundary.

## Key decisions

- Use a two-layer positional/depth-aware Transformer with exact-capacity
  identity-only and fixed structure-shuffled controls.
- Keep runtime IDs out of features and retain them only for replay and Command
  execution.
- Hard-fail any arena game that does not terminate within the declared
  32-Command cap; never retain capped draws as completed evidence.
- Keep the arena local and non-promotional because no callable approved INT-6
  contract exists at this integration boundary.
- Preserve the measured null/ambiguous structure result rather than changing
  thresholds or widening scope after the run.

## Not included

No Rules, Study UI, card/opcode coverage, Teacher-0, search/value, static-proof,
INT-6 rating, promotion, or superhuman scope.
