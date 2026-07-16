# Whole-rollout branching evidence: `dense_page_cow_undo/event_pages_4k_v1`

Contract: `manabot.search-branching.v1` (`9c8dfc3cae6bb85642caf61c074bb3ba2c0845f555c3024238f3d2d6fa85708f`)
Driver: `dense_page_cow_undo/event_pages_4k_v1`
Run: `2026-07-16T19:35:38.291789Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 209.2 | 27201.9 | 0.393s / 0.649s / 0.649s | 23.6 MiB | 5.8 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 1115.9 | 146882.8 | 0.618s / 0.984s / 0.984s | 197.0 MiB | 21.6 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 227.0 | 22069.1 | 0.011s / 0.027s / 0.027s | 14.0 MiB | 3.4 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 340.0 | 31791.2 | 0.127s / 0.144s / 0.144s | 37.8 MiB | 2.1 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Branch lifecycle and journal counters

| Cell | eager forks | checkpoints | fork time | mark time | rollback time | journal peak | COW peak | journal entries | marks / commits / rollbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 0 | 0 | 0.001s | 0.000s | 0.754s | 4.64 MiB | 0.00 MiB | 34385 | 6144 / 3072 / 3072 |
| `flat-saturated-64-v1` | 0 | 0 | 0.005s | 0.002s | 6.912s | 30.23 MiB | 0.03 MiB | 263176 | 49152 / 24576 / 24576 |
| `retained-single-8-v1` | 0 | 0 | 0.002s | 0.000s | 0.000s | 0.11 MiB | 0.07 MiB | 86 | 128 / 128 / 0 |
| `retained-saturated-16-v1` | 0 | 0 | 0.003s | 0.000s | 0.000s | 1.66 MiB | 1.01 MiB | 86 | 2048 / 2048 / 0 |

`null` counters are unsupported by this driver, not observed zeros: system allocator has no counting hook; cow_bytes measures branch-private copied 4096-byte event pages.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 97149.8 | 8.9µs / 22.1µs / 29.7µs | 116 | 0.002s |
| `clone-v1` | 680202.7 | 1.4µs / 1.6µs / 1.8µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run-matrix
uv run scripts/bench_branching.py verify-matrix
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `207b335bf7857c31f06a98092057d6bc22a32c72505b25aefe331b80150e0a85`.
Source SHA-256: `3c2ca12e87cbe7f5e739f89e5c394d379024739a107b3ea3f8eb5a421af9931d`.
Measurement revision: `16ac1a5f75660cad4af17cdf167a48dc790d5db7`.
Release binary SHA-256: `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83`.
Source method: `git-ls-tree-sha256-v1` over 126 tracked paths recorded in the raw receipt.

`source_sha256` hashes the canonical NUL-delimited Git tree entries for the receipt's explicit source closure. Run and verification fail closed when an admitted input is dirty, missing, or gains an undeclared production compile-time dependency. Checkout paths, `.git` representation, `.claude` worktrees, generated evidence, and unrelated tracked files are excluded by construction; changing an admitted engine, embedded-content, harness, dependency, test, or contract input requires regenerating every candidate receipt.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums.

This artifact measures one candidate. The matrix verifier compares all three candidates from the same host, source revision, and release binary and requires equal logical work before the decision record is accepted.
