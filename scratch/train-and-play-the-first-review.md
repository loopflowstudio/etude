# INT-11 Implementation Review

## What was implemented

INT-11 adds the first learned manabot directly on the landed INT-2
`SemanticDecision` boundary. A small matched-capacity Transformer projects
viewer-safe frame facts, admitted typed programs, visible runtime bindings,
and authoritative ragged offers; its decoded submission becomes an ordinary
structured `Command` through the existing managym mutation path.

The one-command experiment regenerates authoritative priority, targeting, and
combat rows; trains semantic, identity-only, and structure-shuffled arms at
three seeds; verifies and reloads nine checkpoints; evaluates ordinary,
identity-holdout, composition-holdout, and >32-choice frontier rows; benchmarks
single and true ragged-batch inference; plays 36 paired-seat terminal w2 games;
and independently replays all evaluation and arena Commands.

## Key choices

- The semantic encoder is a two-layer positional/depth-aware Transformer. It
  does not reuse the killed bag or relational-pooling designs.
- All arms share 31,970 parameters, public facts, heads, ragged decoder,
  optimizer, examples, epochs, and seeds. Only the definition representation
  changes.
- Runtime IDs remain replay addresses and joins. They are never model inputs;
  the one-command workload checks feature invariance after opponent-private
  determinization.
- The arena uses a training-admitted Otter-Penguin terminal combat fixture.
  Policies that do not terminate within 32 Commands hard-fail the experiment;
  nonterminal games cannot appear in a completed receipt.
- Arena output is `development_paired_arena_v1` with
  `promotion_authority=false`. No INT-6 compatibility, rating, or promotion is
  claimed.
- The result verdict is data-derived and explicitly null/ambiguous for intact
  structure. Semantic beats identity-only on the identity holdout but matches
  structure-shuffled; composition transfer is seed-noisy.

## How it fits together

`RuntimePolicyProjector` turns one already-authoritative `SemanticDecision`
into ID-invariant tensors. `SemanticRuntimePolicy` scores the authority's
offers and candidates, and the existing `RaggedPolicyDecoder` lowers those
scores back to authority-minted IDs. The experiment runner reconstructs exact
managym roots, executes Commands only through `SemanticDecision.step()`, and
pins the resulting dataset, checkpoints, metrics, and replays in one manifest.

## Risks and bottlenecks

- The deterministic oracle and 24-row dataset are intentionally tiny. The
  observed structure null may reflect workload sensitivity or optimization,
  not a general equivalence of intact and shuffled programs.
- The terminal combat arena validates complete play and legality but produces
  a flat payoff matrix; it is not discriminative strength evidence.
- Composition transfer ranges from zero to perfect across seeds. A follow-up
  must change the working workload or model, not reinterpret this run.
- Batch throughput is variable on the shared CPU host (241-3,948 decisions/s),
  so exact per-seed measurements remain in the checked artifact.

## What's not included

No Rules or card/opcode expansion, Teacher-0 substitution, search/value
learning, Study/UI work, static kata extension, INT-6 rating/promotion,
open-ended card coverage, or superhuman claim.

## Verification

```bash
uv run experiments/runners/run_semantic_runtime_policy.py \
  --out-dir .runs/int-11-semantic-runtime-policy-v1

uv run pytest tests/semantic/test_runtime_policy.py \
  tests/semantic/test_semantic_policy.py \
  tests/sim/test_structured_policy.py -q
```

Observed: 36/36 terminal arena games, 144 accepted and zero illegal Commands,
zero replay/private-feature mismatches, nine verified checkpoints, and 19
focused tests passing. The checked result is
`experiments/data/int-11-semantic-runtime-policy-v1.json`.
