# Whole-rollout branching evidence: `compact_clone_undo/current_game_v1`

Contract: `manabot.search-branching.v1` (`9c8dfc3cae6bb85642caf61c074bb3ba2c0845f555c3024238f3d2d6fa85708f`)
Driver: `compact_clone_undo/current_game_v1`
Run: `2026-07-16T17:41:21.455519Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 237.5 | 30878.5 | 0.371s / 0.385s / 0.385s | 24.5 MiB | 4.9 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 599.4 | 78896.1 | 1.155s / 1.452s / 1.452s | 205.0 MiB | 21.2 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 136.1 | 13236.4 | 0.021s / 0.023s / 0.023s | 15.5 MiB | 3.9 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 188.4 | 17614.9 | 0.212s / 0.305s / 0.305s | 76.4 MiB | 22.9 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Branch lifecycle and journal counters

| Cell | eager forks | checkpoints | fork time | mark time | rollback time | journal peak | COW peak | journal entries | marks / commits / rollbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 128 | 0 | 0.001s | 0.001s | 0.519s | 4.64 MiB | null | 34385 | 6144 / 3072 / 3072 |
| `flat-saturated-64-v1` | 1024 | 0 | 0.028s | 0.004s | 11.828s | 30.23 MiB | null | 263176 | 49152 / 24576 / 24576 |
| `retained-single-8-v1` | 192 | 0 | 0.003s | 0.000s | 0.000s | 0.11 MiB | null | 86 | 128 / 128 / 0 |
| `retained-saturated-16-v1` | 3072 | 0 | 0.030s | 0.000s | 0.000s | 1.66 MiB | null | 86 | 2048 / 2048 / 0 |

`null` counters are unsupported by this driver, not observed zeros: system allocator has no counting hook; clone-plus-undo does not implement page COW.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 119390.5 | 7.2µs / 18.0µs / 24.5µs | 116 | 0.002s |
| `clone-v1` | 143884.0 | 6.7µs / 7.5µs / 8.8µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run-matrix
uv run scripts/bench_branching.py verify-matrix
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `c0bb1c408b8d430244e20f6a3ee65f1d582947d316e127ee04782088b86864c0`.
Source SHA-256: `d5036a39bf5f04a2008f48e6c0a5909cf5cb787dd73cc1b5b066635647569764`.
Measurement revision: `ce15504097654bfbb28555621d1bc827610d53a0`.
Release binary SHA-256: `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83`.
Source method: `git-ls-tree-sha256-v1` over 126 tracked paths recorded in the raw receipt.

`source_sha256` hashes the canonical NUL-delimited Git tree entries for the receipt's explicit source closure. Run and verification fail closed when an admitted input is dirty, missing, or gains an undeclared production compile-time dependency. Checkout paths, `.git` representation, `.claude` worktrees, generated evidence, and unrelated tracked files are excluded by construction; changing an admitted engine, embedded-content, harness, dependency, test, or contract input requires regenerating every candidate receipt.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums.

This artifact measures one candidate. The matrix verifier compares all three candidates from the same host, source revision, and release binary and requires equal logical work before the decision record is accepted.
