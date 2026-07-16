# Whole-rollout branching evidence: `compact_clone_undo/current_game_v1`

Contract: `manabot.search-branching.v1` (`9c8dfc3cae6bb85642caf61c074bb3ba2c0845f555c3024238f3d2d6fa85708f`)
Driver: `compact_clone_undo/current_game_v1`
Run: `2026-07-16T19:35:38.291789Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 267.2 | 34738.0 | 0.327s / 0.358s / 0.358s | 24.6 MiB | 5.1 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 1264.7 | 166467.1 | 0.573s / 0.757s / 0.757s | 195.9 MiB | 21.8 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 270.2 | 26273.4 | 0.011s / 0.012s / 0.012s | 15.9 MiB | 4.3 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 291.8 | 27284.3 | 0.130s / 0.468s / 0.468s | 82.3 MiB | 28.9 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Branch lifecycle and journal counters

| Cell | eager forks | checkpoints | fork time | mark time | rollback time | journal peak | COW peak | journal entries | marks / commits / rollbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 128 | 0 | 0.001s | 0.000s | 0.459s | 4.64 MiB | null | 34385 | 6144 / 3072 / 3072 |
| `flat-saturated-64-v1` | 1024 | 0 | 0.009s | 0.002s | 6.025s | 30.23 MiB | null | 263176 | 49152 / 24576 / 24576 |
| `retained-single-8-v1` | 192 | 0 | 0.001s | 0.000s | 0.000s | 0.11 MiB | null | 86 | 128 / 128 / 0 |
| `retained-saturated-16-v1` | 3072 | 0 | 0.029s | 0.000s | 0.000s | 1.66 MiB | null | 86 | 2048 / 2048 / 0 |

`null` counters are unsupported by this driver, not observed zeros: system allocator has no counting hook; clone-plus-undo does not implement page COW.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 95464.5 | 8.2µs / 22.2µs / 37.5µs | 116 | 0.002s |
| `clone-v1` | 115365.8 | 7.5µs / 7.8µs / 14.0µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run-matrix
uv run scripts/bench_branching.py verify-matrix
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `cd9940a51c0bb36d40bd9e9136fb65218e28001ae78360af1bd10fa20bfb1df1`.
Source SHA-256: `3c2ca12e87cbe7f5e739f89e5c394d379024739a107b3ea3f8eb5a421af9931d`.
Measurement revision: `16ac1a5f75660cad4af17cdf167a48dc790d5db7`.
Release binary SHA-256: `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83`.
Source method: `git-ls-tree-sha256-v1` over 126 tracked paths recorded in the raw receipt.

`source_sha256` hashes the canonical NUL-delimited Git tree entries for the receipt's explicit source closure. Run and verification fail closed when an admitted input is dirty, missing, or gains an undeclared production compile-time dependency. Checkout paths, `.git` representation, `.claude` worktrees, generated evidence, and unrelated tracked files are excluded by construction; changing an admitted engine, embedded-content, harness, dependency, test, or contract input requires regenerating every candidate receipt.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums.

This artifact measures one candidate. The matrix verifier compares all three candidates from the same host, source revision, and release binary and requires equal logical work before the decision record is accepted.
