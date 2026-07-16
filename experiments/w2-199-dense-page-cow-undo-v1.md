# Whole-rollout branching evidence: `dense_page_cow_undo/event_pages_4k_v1`

Contract: `manabot.search-branching.v1` (`9c8dfc3cae6bb85642caf61c074bb3ba2c0845f555c3024238f3d2d6fa85708f`)
Driver: `dense_page_cow_undo/event_pages_4k_v1`
Run: `2026-07-16T17:41:21.455519Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 152.2 | 19787.9 | 0.582s / 0.671s / 0.671s | 24.1 MiB | 5.1 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 611.2 | 80450.8 | 1.138s / 1.296s / 1.296s | 201.6 MiB | 22.2 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 135.6 | 13187.9 | 0.021s / 0.024s / 0.024s | 13.6 MiB | 3.0 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 146.9 | 13735.0 | 0.278s / 0.326s / 0.326s | 38.1 MiB | 2.3 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Branch lifecycle and journal counters

| Cell | eager forks | checkpoints | fork time | mark time | rollback time | journal peak | COW peak | journal entries | marks / commits / rollbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 0 | 0 | 0.001s | 0.001s | 0.856s | 4.64 MiB | 0.00 MiB | 34385 | 6144 / 3072 / 3072 |
| `flat-saturated-64-v1` | 0 | 0 | 0.015s | 0.011s | 12.484s | 30.23 MiB | 0.03 MiB | 263176 | 49152 / 24576 / 24576 |
| `retained-single-8-v1` | 0 | 0 | 0.001s | 0.000s | 0.000s | 0.11 MiB | 0.07 MiB | 86 | 128 / 128 / 0 |
| `retained-saturated-16-v1` | 0 | 0 | 0.006s | 0.000s | 0.000s | 1.66 MiB | 1.01 MiB | 86 | 2048 / 2048 / 0 |

`null` counters are unsupported by this driver, not observed zeros: system allocator has no counting hook; cow_bytes measures branch-private copied 4096-byte event pages.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 58205.7 | 14.5µs / 36.4µs / 50.9µs | 116 | 0.004s |
| `clone-v1` | 440797.7 | 2.1µs / 2.3µs / 2.5µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run-matrix
uv run scripts/bench_branching.py verify-matrix
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `8609d92cdf00c1b13166aa72078f22ba687c6c425fec32ebce6a4c392a0bf20d`.
Source SHA-256: `d5036a39bf5f04a2008f48e6c0a5909cf5cb787dd73cc1b5b066635647569764`.
Measurement revision: `ce15504097654bfbb28555621d1bc827610d53a0`.
Release binary SHA-256: `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83`.
Source method: `git-ls-tree-sha256-v1` over 126 tracked paths recorded in the raw receipt.

`source_sha256` hashes the canonical NUL-delimited Git tree entries for the receipt's explicit source closure. Run and verification fail closed when an admitted input is dirty, missing, or gains an undeclared production compile-time dependency. Checkout paths, `.git` representation, `.claude` worktrees, generated evidence, and unrelated tracked files are excluded by construction; changing an admitted engine, embedded-content, harness, dependency, test, or contract input requires regenerating every candidate receipt.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums.

This artifact measures one candidate. The matrix verifier compares all three candidates from the same host, source revision, and release binary and requires equal logical work before the decision record is accepted.
