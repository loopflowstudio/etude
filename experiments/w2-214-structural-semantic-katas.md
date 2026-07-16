# W2-214 structural semantic katas

Status: **PRE-REGISTERED / RESULTS PENDING**

World: **offline static semantic diagnostic; no gameplay ABI or world change**

Run only after the preregistration commit exists:

```bash
PYTHONHASHSEED=0 uv run experiments/runners/run_structural_semantic_katas.py \
  --contract experiments/workloads/structural-semantic-katas-v1.json \
  --suite experiments/katas/structural-semantic-katas-v1.json \
  --out experiments/data/structural-semantic-katas-v1.json \
  --report experiments/w2-214-structural-semantic-katas.md \
  --preregistration-revision <revision printed by lf commit>
```

## Question and claim boundary

Can one small relational encoder distinguish five known static semantic
relations that the landed mean/max bag encoder provably deletes, at matched
data, capacity, and bounded CPU cost?

This experiment may nominate `relational_semantic_encoder_v1` only as the
candidate for W2-213's held-out recombination and dynamic-binding probes. It
does not establish recombination, card transfer, runtime binding, executable
rules parity, gameplay strength, or gameplay-integration readiness.

## Frozen instrument

- Five families: operation order, parent-child hierarchy, field role,
  argument binding, and target/choice role.
- 80 equal-token opposite-label pairs per family: 400 pairs / 800 programs.
- Pair-safe content-addressed splits per family: 48 train / 16 validation /
  16 test pairs.
- Definition references and opaque program/card identity are forbidden from
  model input.
- Independent hand-written source fixtures check each compiled-IR label query.
- Normalized program, nuisance, and pair-template overlaps must be zero across
  splits. Every label-independent generator field must be exactly balanced.
- Arms share identical examples, candidate permutations, five family heads,
  optimizer budget, and five seeds. Trainable parameters must differ by no
  more than 5%.

The complete immutable contract is
`experiments/workloads/structural-semantic-katas-v1.json`. The checked source,
oracle fixtures, and canonical suite are under `experiments/katas/`.

## Predictions registered before training

- `bag_v1`: exactly 50% accuracy for every family and seed, zero paired
  prediction disagreements, Brier approximately 0.25, NLL approximately
  `ln(2)`.
- `relational_semantic_encoder_v1`: at least 95% aggregate mean test accuracy,
  at least 90% mean on every family, and at least 40 percentage points uplift
  over the bag on every family.
- Structural calibration: Brier at most 0.10 and NLL at most 0.35 without
  post-hoc temperature scaling.
- Cost: batch-1 p95 at most 2.5 times the bag and batch-128 throughput at least
  40% of the bag, with projection, routing, and all five heads represented.

## Budget and kill criteria

- Seeds: `21401`, `21402`, `21403`, `21404`, `21405`.
- CPU only, one Torch thread, deterministic algorithms, CPython 3.12 through
  `uv`.
- 800 optimizer steps per arm/seed; 8,000 total steps; 512,000 presented
  examples; 30 wall-clock minutes.
- The structural arm must reach 99% train and 95% validation accuracy on every
  seed before a test null can be interpreted.
- Any authority/hash drift, oracle disagreement, identity leak, duplicate,
  split overlap, nuisance-label association, bag symmetry violation, partial
  seed table, or cap overrun invalidates the instrument rather than producing
  a model result.
- No in-run optimizer search, model growth, or result-contingent retuning.

## Pre-registered branches

1. **Nominate for W2-213:** every semantic, calibration, uncertainty,
   parameter, and cost gate passes.
2. **Instrument invalid:** repair the suite/authority and re-register before
   training again.
3. **Teacher or label error:** repair the independent oracle/compiler boundary.
4. **Optimization or capacity unresolved:** propose a separately registered
   diagnostic; do not scale in this task.
5. **Missing structural relation:** redesign the one or two failed projection
   relations.
6. **Encoder redesign:** reject the one-block relational encoder if aggregate
   or three-family evidence fails.
7. **Cost redesign:** preserve semantic evidence but withhold nomination if
   parameter, latency, or throughput gates fail.

## Results

Pending. This section must be generated from the complete raw result receipt
after the preregistration barrier.
