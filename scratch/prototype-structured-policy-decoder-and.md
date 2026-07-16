# W2-189 — Prototype structured policy decoder and benchmark legacy ABI

## Directive and scope decision

Directive v2 opens the dependency gate at W2-188 merge
`75b8c1c090dbb7d0f7bf241bd806fbab39aae348`. This Task has been rebased onto
current `origin/main` with `lf rebase`. It will use W2-188's landed
`StructuredOfferSet` and independent legacy attacker adapter as the legality
and compatibility boundary; it will not reproduce offer generation or infer an
unmerged representation.

The implementation is one experimental, narrow decoder and one reproducible
benchmark in this existing Task worktree/serial PR. The decoder turns ragged
offer and candidate score rows into `OfferSubmission` IDs. It is not an
observation encoder or policy network: a deterministic seeded scorer supplies
the benchmark logits so both the structured path and legacy oracle receive the
same policy intent. The production `Agent`, its checkpoints, the 32-row tensor
observation, `Env.step(index)`, the GUI protocol, and rules behavior remain
unchanged.

The runtime integration will be thin rather than a parallel rules surface:

- W2-188 `Game::structured_offers()` publishes the exact typed projection.
- W2-188 `StructuredOfferSet::decode()` validates offer, role, candidate, and
  cardinality IDs.
- W2-188 `Game::apply_offer_submission()` applies the atomic path.
- An experiment-only legacy adapter lowers the same accepted semantic choice
  through existing positional `Game::step` prompts, including the sequential
  attacker chain. It is the independent comparison oracle and must produce the
  same canonical state/trace as the atomic path.
- A minimal PyO3 handle keeps the private `StructuredOfferSet` binding paired
  with its public JSON projection, so Python never reconstructs authority from
  display values and stale sets still fail in Rust.

No second Task, worktree, or simultaneous PR is needed.

## User-visible outcome

Rules and learning developers can run one checked-in command and receive a
machine-readable result plus a reviewed report showing whether a ragged
structured decoder can drive the admitted offer families without the legacy
32-row ceiling:

```sh
uv run scripts/bench_structured_policy.py \
  --workload experiments/workloads/structured-policy-v1.json \
  --out experiments/data/structured-policy-v1.json
```

The command exits nonzero unless the fixed workload observes all three landed
families (priority pass, a required single target, and declare attackers), at
least one decision with more than 32 legal choices, zero capacity overflows,
zero decoder-produced illegal submissions, and exact structured-versus-legacy
state agreement on every shared case. On success it reports:

- semantic action agreement on shared states;
- seat-balanced win rate for the structured hybrid and legacy adapter;
- p50 and p95 decoder/adapter decision latency;
- completed games, environment commands, and legacy-equivalent actions per
  second;
- isolated-process peak RSS;
- counts for maximum candidates, maximum represented legal branches,
  structured decisions, explicit legacy fallbacks, overflows, illegal decodes,
  and trace mismatches.

`experiments/structured-policy-decoder.md` explains the checked-in reference
run and its limitations. `experiments/data/structured-policy-v1.json` is the
raw receipt; performance values may vary by host, while seeds, cases, gates,
and agreement counts are reproducible.

## Source of truth and derived views

The authoritative live decision remains the current in-memory `Game` and its
legacy `ActionSpace`. W2-188's `StructuredOfferSet` is a bound, derived
projection of that decision with private ID-to-engine mappings. Its public
`StructuredOfferProjection` is decoder input; `OfferSubmission` is decoder
output; `AtomicCommand` is never forged or deserialized by Python.

The checked-in workload manifest is the authoritative benchmark recipe. It is
versioned and records:

- schema/workload version;
- exact arranged cases and deck lists;
- game, scorer, and latency seeds;
- seat-balanced game count and per-seat allocation;
- warmup and measured iteration counts;
- the required minimum frontier (`> 32`), admitted prompt kinds, and hard
  zero-error gates.

The result JSON and Markdown report are derived evidence. They record the
workload digest, repository commit, extension/build identity, OS/architecture,
Python/Rust package versions, command line, and all raw counters/distributions
needed to audit the summary. A result whose workload digest differs is not
silently combined with the reference run.

## Decoder contract

Add an experimental Python module, expected at
`manabot/sim/structured_policy.py`, with wire-shaped immutable records and a
`RaggedPolicyDecoder`. It consumes a batch represented by flat offer and
candidate rows plus offsets; no dimension is allocated from
`ObservationEncoderConfig.max_actions`.

For the admitted W2-188 grammar:

1. Offers receive one score each. Highest score wins; equal scores break by
   stable wire order.
2. A `Select(min=1,max=1)` target role chooses the highest-scored candidate.
3. The unordered attacker `Select(min=0,max=N)` role selects candidates whose
   score exceeds the seeded scorer's declared threshold, then enforces min/max
   deterministically in candidate order. It never enumerates the `2^N`
   declarations.
4. The result contains only the chosen `offer_id`, declared `role`, and emitted
   `candidate_id`s. The bound Rust offer set performs the authoritative decode
   and apply.

The decoder fails closed on duplicate IDs, missing/dynamic candidate sources,
unsupported choice kinds/dependencies, non-finite or length-mismatched score
rows, impossible cardinality, an empty offer set, or candidate-count integer
overflow. These are harness/configuration failures, not fallbacks. Intentional
negative tests submit fabricated, duplicate, over-cardinality, and stale IDs
directly to Rust and require typed rejection with unchanged state; they are
separate from the required zero illegal *decoder outputs*.

The benchmark `SeededSemanticScorer` is stateless apart from its declared seed
and decision ordinal. It derives stable scores from prompt kind, offer verb,
source subject, role, candidate subject, and ordinal. The same score tape drives
the structured decoder and legacy adapter so action agreement measures ABI
translation rather than different policy randomness. It is deliberately not a
claim about learned policy strength.

## W2-181/W2-188 integration adapters

The bridge must make the dependency boundaries explicit:

- **W2-181 priority/target adapter:** expose a bound offer-set handle and its
  serialized projection; apply a decoder submission through
  `Game::apply_offer_submission`. The legacy oracle finds the matching
  `CastSpell`/`PassPriority` action and, for a cast, the subsequent
  `ChooseTarget` action using the private decoded engine identities—not labels
  or echoed `SubjectRef`s.
- **W2-188 attacker adapter:** reuse the landed complete multi-select offer.
  The legacy oracle walks the sequential `DeclareAttacker` prompts in their
  authoritative order and chooses attack/decline for each permanent named by
  the decoded command, matching W2-188's differential test. The structured path
  applies one atomic declaration.
- **Compatibility fallback:** `structured_priority_offers()` intentionally
  omits lands, activated abilities, and unsupported cast shapes. In full-game
  evaluation, the shared seeded policy first scores the complete legacy
  action space. If its chosen action is not representable by the structured
  projection, both paired games take the same legacy index and increment an
  explicit `unsupported_fallbacks` counter. A structured prompt is never
  presented as complete when it is not. Declare attackers has no such fallback
  because W2-188's offer represents the complete declaration.

The PyO3 bridge is experimental and additive. Expected public shape:

- `Env.structured_offers() -> StructuredOfferSet`;
- `StructuredOfferSet.projection_json() -> str`;
- `Env.step_structured(offer_set, submission_json)`;
- an experiment-only `Env.step_legacy_submission(...)` and canonical snapshot
  digest used by the paired oracle.

The exact Python spelling may follow existing PyO3 conventions, but the bound
handle, stale-set behavior, independent legacy execution, and no-display-value
trust are non-negotiable. `managym/__init__.pyi` must describe the additive
surface. No existing signature or enum discriminant changes.

## Fixed workload and measurements

`experiments/workloads/structured-policy-v1.json` contains three layers:

### Frontier fixtures

1. **Bolt 35-target fixture:** Lightning Bolt at priority with 33 battlefield
   creatures plus both players. The decoder must see all 35 candidate IDs,
   select the seeded candidate, decode/apply legally, and match legacy
   cast-then-target state. This proves candidate count beyond 32.
2. **Six-attacker fixture:** six eligible creatures represent all 64 legal
   declarations in six candidate rows. At minimum, the seeded declaration and
   edge masks (none/all/alternating) run through both paths; the existing
   W2-188 exhaustive 64-mask Rust test remains the full legality oracle. This
   proves branch count beyond 32 without materialization.
3. **Priority pass fixture:** zero-choice pass decodes and matches the legacy
   pass action.

### Shared-state agreement corpus

Generate a fixed corpus from the arranged fixtures and seeded two-deck traces.
For every state where the seeded policy's choice is representable by both ABIs,
run the decoder once, apply the atomic path to one fork, apply the independent
legacy adapter to another, and compare canonical snapshot, visible observation,
events, pending events, current decision, terminal result, and winner. Report
agreement as `matching / shared`; any mismatch is a hard failure. Unsupported
legacy decisions are counted but excluded from the agreement denominator.

### Seat-balanced paired games

Run an even fixed number of UR Lessons versus GW Allies games. Each adjacent
seed pair swaps which deck is seat 0/on the play. For each seed, run a
structured-hybrid game and an all-legacy game from identical initial state and
the same semantic score tape. Structured-supported decisions use the atomic
path in the hybrid; unsupported decisions use the explicit compatibility
fallback in both. Record per-adapter, overall, on-play, and on-draw win rates,
draws/caps, structured-decision coverage, and whether winners/traces agree.

This is a migration benchmark, not a balance experiment. The acceptance gate
is exact paired outcome/trace agreement and zero errors; it does not require a
particular deck to win or claim statistical policy quality.

Latency uses warmup followed by fixed repetitions of projection + flatten +
score + decode, and separately end-to-end apply, with `perf_counter_ns` and raw
samples summarized as p50/p95. Environment throughput uses the paired game
workload and reports games/s, engine commands/s, and legacy-equivalent
actions/s so the atomic attacker/cast path is not credited merely for collapsing
sequential prompts. Peak RSS is measured in fresh child processes per adapter
using the platform-normalized high-water mark; structured and legacy runs are
never measured sequentially in the same process.

## End-to-end proof

The concrete acceptance scenario is the 35-target Bolt fixture followed by the
six-attacker fixture and paired two-deck run:

1. Rust publishes every ragged candidate with no `.take(32)` or tensor encode.
2. Python flattens the projection with explicit offsets and produces a valid
   ID-only submission.
3. Rust decodes and applies it atomically.
4. A fork applies the same semantic choice through legacy positional prompts.
5. Their canonical/visible traces agree.
6. The paired full games preserve winners and traces while producing all
   required latency, throughput, RSS, coverage, and error counters.

Focused proof after the Rust extension is rebuilt:

```sh
cd managym && cargo test structured_offer
cd managym && cargo test structured_policy
uv run --with maturin maturin build --release -i .venv/bin/python \
  -m managym/Cargo.toml
uv run pytest tests/sim/test_structured_policy.py tests/bench/test_structured_policy_benchmark.py
uv run scripts/bench_structured_policy.py \
  --workload experiments/workloads/structured-policy-v1.json \
  --out experiments/data/structured-policy-v1.json
```

Place the cp312 extension from the built wheel at
`managym/_managym.cpython-312-darwin.so` before Python tests, as required by
`AGENTS.md`. Before handoff, run the full relevant Rust and Python suites plus
format/lint checks. Every Python, pytest, and maturin invocation uses `uv run`.

The benchmark command itself validates the workload digest and hard gates;
unit tests use a small smoke workload rather than asserting host-dependent
timing or RSS numbers. The checked-in Markdown report cites the exact result
JSON and command.

## Affected surfaces and consumers

- **Rust structured boundary:** no wire-shape change; add environment-level
  bound-handle/legacy-oracle access around W2-188 types.
- **PyO3 and stubs:** additive experimental offer-set handle and structured/
  legacy benchmark stepping methods.
- **Python simulation code:** new isolated ragged decoder, deterministic scorer,
  and benchmark adapters; no import from the production training path.
- **Benchmark assets:** versioned workload JSON, runner, raw result JSON, and
  report.
- **Tests:** decoder shape/error tests, PyO3 binding/staleness tests,
  structured-versus-legacy differential tests, >32 gates, deterministic
  workload smoke, and result-schema tests.
- **Existing Python/search/training/GUI/replay:** unchanged and still consume
  fixed positional actions. Existing checkpoints remain loadable.

There are no schema migrations, persisted match changes, network calls, or live
frontend changes.

## Absent and error states

- Game over or no active decision: no structured handle; the benchmark treats
  an attempted decode as a harness error.
- Decision kind outside priority/declare attackers: explicit legacy fallback in
  paired games; never an empty fabricated structured prompt.
- Priority projection omits the seeded legacy choice: both paths take the same
  legacy action and record one unsupported fallback.
- Covered offer with missing candidates or an impossible cardinality: hard
  invariant failure; no fallback.
- Empty ragged batch, duplicate IDs, non-finite/misaligned scores, unsupported
  dependencies/choice kind, or integer overflow: decoder error before apply.
- Unknown, duplicate, extra, over-cardinality, or stale IDs: typed Rust error
  and byte-for-byte unchanged state.
- Any decoder-produced rejection, state/trace mismatch, game cap, missing
  metric, workload digest mismatch, or child-process failure: nonzero runner
  exit and no successful report status.
- Timing/RSS drift across hosts: retained as host-stamped measurement, not a
  correctness failure. Missing p50/p95 or peak RSS is a failure.

## Operational boundary

- Flattening and decoding are `O(offers + candidates)` in time and memory.
- No fixed candidate/action width, padding to 32, complete target-command
  enumeration, or attacker-subset enumeration is permitted.
- IDs and tie-breaking are deterministic for a fixed offer projection and score
  tape.
- The benchmark is offline, single-host, and network-free. Child processes are
  used only to isolate peak RSS.
- Full-game work is bounded by manifest game/step caps. Any cap hit is reported
  and fails the reference workload.
- Performance comparison is descriptive. Correctness gates are zero overflow,
  zero illegal decoder output, exact shared-state agreement, and complete
  required metrics; this Task does not invent an unrequested latency or
  throughput pass threshold.

## Exclusions

- Replacing, retraining, or modifying the production policy/value network,
  PPO, checkpoints, vectorized training, or search policies.
- Removing or widening the legacy 32-row observation/action tensor.
- Changing W2-188 offer grammar, rules legality, combat ordering, card
  semantics, or adding new cards/rule families.
- Structured blockers, payments, modes, optional/multiple targets, or other
  decision families not admitted by W2-181/W2-188.
- Live GUI/Game protocol adoption, revisions, prompt persistence, reconnect,
  command receipts/idempotency, or viewer projections.
- Treating benchmark win rate as evidence of policy strength or deck balance.
- A new Task, worktree, stacked PR, or general policy-architecture migration.
