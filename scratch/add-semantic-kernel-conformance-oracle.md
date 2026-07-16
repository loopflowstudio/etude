# W2-200: Semantic-kernel conformance oracle CI

## Outcome

A rules maintainer changing the curated semantic kernel gets one blocking
`Semantic Kernel Conformance` CI result. The job replays checked deterministic
UR Lessons versus GW Allies traces through a readable explicit-step reference
reducer and the production trivial-step-collapsing executor, runs property and
metamorphic checks, and performs bounded valid-action fuzzing. A divergence
reports an exact replay command and leaves a machine-readable failure seed as a
CI artifact.

The repository also publishes a machine-checked Phase overlap matrix pinned to
phase-rs commit
`553b97bd5c9f1a28bf7a6ebe80f6cb3a0e296c0d`. The matrix distinguishes normalized
matches, known mismatches, and practical exclusions. It does not call Phase a
trusted oracle or require a Phase checkout in CI.

This delivers only Search Branching and Conformance KR3 implementation
evidence. Whether the KR holds remains a later evidence judgment.

## Boundary and source of truth

- `content/semantic/v1/two_deck.source.json` is the admitted matchup inventory.
  The harness resolves its semantic definitions through `registry_name`; it
  must not copy another deck constant. The current persisted source is the
  actual boundary even though older prose calls both lists 40-card decks: it
  contains 41 UR cards and 40 GW cards.
- The existing `GameState`, `ContentPack`, `Action`, and committed event ledgers
  remain authoritative runtime state. `GameState::deterministic_hash()` is the
  complete mutable-state comparison oracle; serialized `ActionSpace`,
  `PendingChoice`, and terminal result cover authoritative `Game` facts outside
  that hash that matter to this task.
- `conformance/semantic-kernel-v1/replays/*.json` is the persisted replay
  corpus. Each record contains schema/contract versions, matchup source and
  content digests, seat orientation, setup and choice seeds, step cap, the
  selected semantic action JSON plus its diagnostic index at every external
  prompt, and expected checkpoint/final digests. Semantic actions, rather than
  bare indices, keep a replay stable across harmless action ordering changes.
- `conformance/semantic-kernel-v1/phase-overlap.json` is the authored Phase
  comparison record. A derived
  `conformance/semantic-kernel-v1/evidence.json` summarizes replay results,
  exercised prompt/action kinds, corpus digests, fuzz configuration, and Phase
  status counts. `check` regenerates the summary in memory and requires exact
  equality; the summary is evidence, not a second authority.

## Reference and optimized executors

Add a focused Rust conformance module and CLI, keeping the reference path
deliberately small and readable:

- `reference/explicit-step-v1` creates `Game` with `skip_trivial = false`.
  Before and after each persisted external semantic command it repeatedly
  applies only the single legal action until the next nontrivial prompt or
  terminal state. It never chooses among alternatives on its own.
- `optimized/skip-trivial-v1` creates the production `Game` with
  `skip_trivial = true` and applies the same semantic command at its published
  prompt.
- At every normalized external boundary compare deterministic state hash,
  serialized legal action space, serialized pending choice, event boundaries,
  and winner/game-over state. A missing or duplicate semantic command is drift,
  not permission to fall back to the old index.
- The only deliberately excluded `Game` fields are `decision_epoch` and
  `skip_trivial_count`, which count different publication/scheduling work by
  construction, plus behavior trackers because learning is outside W2-200.

This is independent evidence for deterministic command replay and the
trivial-step optimization boundary, not an independently authored
Comprehensive Rules implementation. It shares the existing low-level card and
rules primitives. That limitation must appear in the published evidence.

## Executable evidence

Implement `managym/src/conformance.rs`, a thin
`managym/src/bin/semantic_conformance.rs`, and focused integration tests. The
CLI has four noninteractive operations:

1. `check` validates corpus schemas/digests and the Phase matrix, replays every
   checked case in both seat orientations, compares every checkpoint, replays
   checked regression seeds, and compares the derived evidence byte-for-byte.
2. `record` deliberately regenerates checked replay/evidence artifacts from a
   fixed seed manifest. It is never invoked by CI and documents that semantic
   baseline changes require review.
3. `fuzz` generates only currently legal semantic actions from fixed game and
   choice seeds, runs both executors, and checks the same boundary after every
   command. It accepts explicit case and command caps and a failure directory.
4. `replay` accepts one checked or failure receipt and reproduces its exact
   orientation, seeds, semantic command tape, and expected failure/pass point.

Property tests cover independently constructed same-seed replay, semantic
action-tape stability across action-order lookup, nonempty legal prompts before
terminal state, and clone/root isolation. Metamorphic tests cover:

- explicit singleton stepping versus production trivial-step collapse;
- replacing the shared `ContentPack` allocation with equal content;
- renaming compatibility-only physical-card names after setup, proving runtime
  semantics do not dispatch on card names; and
- repeating the same corpus case from both fresh construction and a cloned
  root with identical checkpoints.

The checked replay manifest includes both seat orientations and fixed seeds
selected to reach the two-deck prompt/action families. A hard 2,000 external
command cap classifies a nonterminal replay as `capped`; it never silently calls
that terminal or drops the case.

For every fuzz case, catch both reported mismatches and panics. Before returning
nonzero, write
`target/semantic-conformance/failures/<case>.json` with the matchup/content
digests, orientation, exact game and choice seeds, step cap, semantic command
tape through the failure, last matching checkpoints, failing step, and error.
`replay <path>` reproduces that file exactly. Checked files under
`conformance/semantic-kernel-v1/regressions/` are always replayed; CI also
uploads the ephemeral failure directory on failure so a new seed survives the
runner and can be promoted in the next fix.

## Phase overlap matrix

The JSON schema records the exact repository and full 40-character Phase
revision once, and every Phase source URL must contain that same revision. Its
rows close over the 15 rule families already listed in
`content/semantic/v1/coverage.evidence.json`; no new card/rules coverage model
is invented here.

Each row contains a normalized case/assertion, local executable replay or trace
test references, pinned Phase source/test references, and exactly one status:

- `match`: both pinned evidence sets establish the same normalized outcome;
- `mismatch`: the normalized outcomes differ, with both outcomes stated; or
- `excluded`: a precise practical reason prevents comparison.

Known curated-boundary simplifications from
`experiments/card-conformance-audit.md` (including learn without a sideboard,
Suki hybrid payment/legend behavior, random bottom ordering, and ward scope)
must appear as mismatches or exclusions rather than being hidden in prose. The
matrix has derived nonempty status counts and an explicit nonempty
`mismatches` list for those known differences. Missing families, unpinned URLs,
an empty matrix, or an unsupported status fail `check`.

CI does not clone, compile, fetch card data for, or execute Phase. The pinned
matrix is source-inspection evidence because Phase's generated data/assets and
large workspace make a live cross-engine dependency impractical for this
bounded blocking job. Network absence therefore does not weaken or skip the
gate; changing the Phase pin is an authored artifact change.

## End-to-end proof

One checked scenario starts from the exact persisted matchup source, with UR in
the first seat and GW in the second, and records fixed setup/choice seeds. The
optimized executor publishes a nontrivial prompt; the corpus-selected semantic
action is applied there. The reference reducer finds the same serialized action
at its explicit prompt, applies it, and consumes only forced singleton actions.
After every command, including target/payment/combat/mid-resolution prompts,
both sides must have the same authoritative state hash, legal prompt, pending
choice, event boundary, and terminal result. The reverse seating is checked the
same way. Replaying the checked tape must reproduce the checked evidence digest.

The local/CI proof commands are:

```bash
cargo test --locked --manifest-path managym/Cargo.toml --test semantic_conformance_tests
cargo run --locked --manifest-path managym/Cargo.toml --bin semantic_conformance -- check --root conformance/semantic-kernel-v1
cargo run --locked --manifest-path managym/Cargo.toml --bin semantic_conformance -- fuzz --root conformance/semantic-kernel-v1 --seed 24301 --cases 32 --max-commands 512 --failure-dir target/semantic-conformance/failures
```

The documented failure handoff is:

```bash
cargo run --locked --manifest-path managym/Cargo.toml --bin semantic_conformance -- replay target/semantic-conformance/failures/<case>.json
```

The new CI job runs those commands on Ubuntu, uploads failure JSON with
`actions/upload-artifact` under `if: failure()`, and becomes a required input to
the existing aggregate `result` job. No Python helper is needed; if one becomes
necessary during implementation, every invocation and documented command must
use `uv run`.

## Absent and error states

- Missing, empty, duplicate, or malformed replay/regression artifacts; unknown
  schema/contract versions; source/content digest drift; or a replay command
  absent/duplicated at the live prompt are hard failures with file and step.
- A mismatch writes the exact replay seed/tape before the command exits
  nonzero. Failure-artifact write failure is itself reported and must not hide
  the original mismatch.
- Terminal-before-tape, tape-exhausted-before-terminal, command cap, empty legal
  prompt, unexpected executor error, and panic are distinct outcomes. Only an
  expected checked outcome passes.
- Missing Phase rows, a revision other than the exact pin, unpinned source
  references, or a `match` without evidence on both sides fail validation.
  External network unavailability is not consulted.
- An empty fuzz regression directory is valid. An empty checked baseline replay
  corpus or a fuzz invocation with zero cases/commands is invalid.

## Operational boundary

- The conformance job is offline after checkout and has no Phase, Scryfall,
  browser, Python-extension, learning, or product-service dependency.
- Corpus replay and 32 by 512 bounded fuzzing should finish within 90 seconds
  after Rust compilation on a standard two-core GitHub Ubuntu runner. A case
  cannot exceed 2,000 external commands; fuzz cannot exceed its explicit command
  cap. No unbounded random loop or wall-clock seed is allowed.
- JSON is written atomically to the requested failure directory. Checked files
  are never rewritten by `check` or `fuzz`.
- The harness records deterministic logical hashes and counts only; timing,
  pointer addresses, allocator state, and git revision do not enter checked
  replay receipts.

## Affected consumers and compatibility

- Rust maintainers gain the conformance CLI/module and integration tests.
- GitHub Actions gains the blocking conformance job, failure artifact, and
  aggregate-result dependency.
- Reviewers gain checked replay/evidence JSON and a documented Phase matrix.
- Existing fixed actions, structured offers, PyO3/Python, search drivers,
  frontend/product UI, learning observations, and card registration remain
  wire- and behavior-compatible. The harness reads their public/logical facts;
  it does not add a new runtime API consumer.

## Exclusions

- W2-197 fork/mark/rollback contract repair and every branching representation
  or benchmark
- An independent full Magic/Comprehensive Rules engine or a live Phase runtime
  dependency
- New cards, rules families, semantic-program interpretation, card-data
  fetching, or changes to sanctioned two-deck behavior
- Viewer-safe projection, learning/observation, model, Python extension,
  product UI, browser, replay UI, and performance claims
- Search KR1/KR2/KR4 evidence or claiming/closing Search KR3 from this PR alone

## One-PR build order

1. Add the versioned corpus/failure/Phase schemas and their strict validation.
2. Add the explicit reference reducer, optimized adapter, checkpoint comparison,
   replay/record CLI, and focused property/metamorphic tests.
3. Add bounded valid-action fuzzing and durable failure receipts/replay.
4. Record the reviewed baseline corpus/evidence and the pinned Phase matrix.
5. Add the documented CI job, failure upload, aggregate dependency, and run the
   three proof commands before headless landing.
