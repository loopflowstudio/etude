# W2-182: whole-rollout branching benchmark harness

Status: implemented and verified against directive v3.

The canonical design and cross-Task measurement authority is
`docs/benchmarks/search-branching-contract-v1.md`. W2-179 was given that
contract through its Task follow-up observation. This scratch note records the
Task boundary and receipts without redefining the contract.

## User-visible outcome

An engine/search developer can run:

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

The first command produces an exact full-clone baseline at the current
single-worker and saturated worker x actor x rollout shapes. The second
recomputes all contract, source, artifact, repeat-checksum, cell, and summary
checks. The derived report leads with whole-rollout simulations/s,
transitions/s, root latency, and peak RSS; clone latency is secondary.

## End-to-end proof

The release binary reconstructs the exact 48-action midgame and 80-action
heavy fixtures, validates their drift fields, and passes all four equivalence
seeds on both fixtures. The `uv` runner launches fresh native worker groups,
waits for warmup/ready, samples aggregate worker RSS every 5 ms after a start
barrier, preserves every raw worker record, derives summaries, writes
atomically, and verifies the artifact. Every primary cell repeats its first
root in a fresh group and must reproduce the ordered result checksum.

The checked-in proof is:

- `experiments/data/w2-182-search-branching-v1.json`
- `experiments/w2-182-search-branching-v1.md`
- `uv run scripts/bench_branching.py verify`

## Source of truth

- `Game` plus its current `ActionSpace` is the rules/legality authority.
- `BenchmarkManifest` in `managym/src/benchmark.rs` is the executable workload
  authority and matches the canonical Markdown contract.
- The checked-in raw JSON is the measurement authority; the Markdown report
  is generated only from it.
- Semantic hashes use ordered logical JSON plus BLAKE3. Physical cards carry
  object ID, stable registry key, and owner; definitions occur once via a
  sorted content digest. The snapshot contains no randomized map iteration,
  pointers, allocator state, timing, or RSS.

## Affected surfaces and consumers

- Native rules/search state and release benchmark binary
- Python process/RSS orchestration (always invoked through `uv`)
- Raw JSON schema and derived experiment report
- W2-179, which shares the exact deck, fixture recipes, seeds, and diagnostic
  timing boundaries
- Future clone-plus-undo and page-COW-plus-undo drivers through the same
  `BranchDriver` hooks

The Python extension ABI, policy observation ABI, play UI, and current search
entry points remain unchanged.

## Absent and error states

Fixture drift, empty nonterminal action spaces, terminal reuse, step-cap
overflow, failed equivalence, mismatched repeated checksums, worker failure,
missed ready/RSS sampling, malformed JSON, missing required metadata, or stale
summary/report data invalidates the run. Journal, page-COW, and allocator
counters are `null` with an explicit unsupported reason for the full-clone
driver; they are never reported as zero.

## Operational boundary

Native release workers contain all timed rules work. Fixture construction,
warmup, semantic correctness hashing, process startup, RSS polling, and JSON
serialization are excluded from native root timers. Aggregate process RSS is
sampled at 5 ms and may double-count shared pages. The canonical saturated
cell requires at least eight physical cores unless explicitly labeled
oversubscribed.

## Exclusions

This Task does not implement or select clone-plus-undo or dense page-COW. It
does not add neural inference, change search policy, establish a regression
threshold, or claim that clone latency alone justifies a storage design.

## Verification receipts

- Rust: 16 unit tests, 2 conformance, 13 engine, 162 rules, 6 scenario, and 10
  search tests pass; clippy is clean with warnings denied.
- Python: 4 focused benchmark tests pass; Ruff check/format pass.
- Artifact SHA-256: `aea588b65d9e3faff3f224bd7123cd3b579bbe98f80979f88b54f4c9ae64ec7f`.
- Loopflow Task observation was published with the exact contract before the
  local Loopflow binary became incompatible with its registry. The final
  evidence broadcast was dropped with that explicit local-tool blocker; the
  implementation and verification remained computable inline.
