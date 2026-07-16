# Whole-rollout branching evidence: `full_clone/current_game_v1`

Contract: `manabot.search-branching.v1` (`9c8dfc3cae6bb85642caf61c074bb3ba2c0845f555c3024238f3d2d6fa85708f`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-16T17:41:21.455519Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 283.3 | 36833.0 | 0.245s / 0.259s / 0.259s | 16.0 MiB | 0.6 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 1636.4 | 215391.8 | 0.347s / 0.399s / 0.399s | 129.4 MiB | 2.6 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 229.3 | 22294.3 | 0.010s / 0.013s / 0.013s | 16.1 MiB | 4.6 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 286.6 | 26794.4 | 0.137s / 0.140s / 0.140s | 75.8 MiB | 24.2 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Branch lifecycle and journal counters

| Cell | eager forks | checkpoints | fork time | mark time | rollback time | journal peak | COW peak | journal entries | marks / commits / rollbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 128 | 3072 | 0.001s | 0.011s | 0.025s | null | null | null | null / null / null |
| `flat-saturated-64-v1` | 1024 | 24576 | 0.006s | 0.132s | 0.280s | null | null | null | null / null / null |
| `retained-single-8-v1` | 192 | 0 | 0.001s | 0.000s | 0.000s | null | null | null | null / null / null |
| `retained-saturated-16-v1` | 3072 | 0 | 0.017s | 0.000s | 0.000s | null | null | null | null / null / null |

`null` counters are unsupported by this driver, not observed zeros: system allocator has no counting hook; full-clone baseline has no undo journal or page-COW counters.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 126363.0 | 6.8µs / 16.9µs / 22.7µs | 116 | 0.002s |
| `clone-v1` | 161520.7 | 6.2µs / 6.3µs / 7.5µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run-matrix
uv run scripts/bench_branching.py verify-matrix
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `e954f8514753861a5a0f15d4ef6a7d0b0ad378862fe4f520392da1cd91e47682`.
Source SHA-256: `d5036a39bf5f04a2008f48e6c0a5909cf5cb787dd73cc1b5b066635647569764`.
Measurement revision: `ce15504097654bfbb28555621d1bc827610d53a0`.
Release binary SHA-256: `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83`.
Source method: `git-ls-tree-sha256-v1` over 126 tracked paths recorded in the raw receipt.

`source_sha256` hashes the canonical NUL-delimited Git tree entries for the receipt's explicit source closure. Run and verification fail closed when an admitted input is dirty, missing, or gains an undeclared production compile-time dependency. Checkout paths, `.git` representation, `.claude` worktrees, generated evidence, and unrelated tracked files are excluded by construction; changing an admitted engine, embedded-content, harness, dependency, test, or contract input requires regenerating every candidate receipt.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums.

This artifact measures one candidate. The matrix verifier compares all three candidates from the same host, source revision, and release binary and requires equal logical work before the decision record is accepted.
