# Whole-rollout branching evidence: `dense_page_cow_undo/event_pages_4k_v1`

Contract: `manabot.search-branching.v1` (`9c8dfc3cae6bb85642caf61c074bb3ba2c0845f555c3024238f3d2d6fa85708f`)
Driver: `dense_page_cow_undo/event_pages_4k_v1`
Run: `2026-07-16T17:32:31.018668Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 259.4 | 33725.3 | 0.337s / 0.361s / 0.361s | 24.4 MiB | 4.9 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 718.5 | 94576.3 | 0.945s / 1.381s / 1.381s | 200.0 MiB | 22.7 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 156.4 | 15205.5 | 0.018s / 0.022s / 0.022s | 14.0 MiB | 3.3 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 337.3 | 31539.3 | 0.127s / 0.145s / 0.145s | 38.1 MiB | 2.3 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Branch lifecycle and journal counters

| Cell | eager forks | checkpoints | fork time | mark time | rollback time | journal peak | COW peak | journal entries | marks / commits / rollbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 0 | 0 | 0.000s | 0.000s | 0.504s | 4.64 MiB | 0.00 MiB | 34385 | 6144 / 3072 / 3072 |
| `flat-saturated-64-v1` | 0 | 0 | 0.009s | 0.004s | 10.617s | 30.23 MiB | 0.03 MiB | 263176 | 49152 / 24576 / 24576 |
| `retained-single-8-v1` | 0 | 0 | 0.001s | 0.000s | 0.000s | 0.11 MiB | 0.07 MiB | 86 | 128 / 128 / 0 |
| `retained-saturated-16-v1` | 0 | 0 | 0.003s | 0.000s | 0.000s | 1.66 MiB | 1.01 MiB | 86 | 2048 / 2048 / 0 |

`null` counters are unsupported by this driver, not observed zeros: system allocator has no counting hook; cow_bytes measures branch-private copied 4096-byte event pages.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 121494.1 | 7.0µs / 17.5µs / 24.1µs | 116 | 0.002s |
| `clone-v1` | 895667.8 | 1.1µs / 1.3µs / 1.4µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run --driver dense_page_cow_undo/event_pages_4k_v1
uv run scripts/bench_branching.py verify --driver dense_page_cow_undo/event_pages_4k_v1
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `671c35bb179acd15f4eea6678558065107157550721aba3be0a775371bb1cf41`.
Source SHA-256: `63e73de34add80719522d57f76606e172e645a29d3c690bbdf55556996e10170`.
Measurement revision: `81b189624265c46a96c25609d9f6dd29e339e166`.
Release binary SHA-256: `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83`.
Source method: `git-ls-tree-sha256-v1` over 126 tracked paths recorded in the raw receipt.

`source_sha256` hashes the canonical NUL-delimited Git tree entries for the receipt's explicit source closure. Run and verification fail closed when an admitted input is dirty, missing, or gains an undeclared production compile-time dependency. Checkout paths, `.git` representation, `.claude` worktrees, generated evidence, and unrelated tracked files are excluded by construction; changing an admitted engine, embedded-content, harness, dependency, test, or contract input requires regenerating every candidate receipt.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums.

This artifact measures one candidate. The matrix verifier compares all three candidates from the same host, source revision, and release binary and requires equal logical work before the decision record is accepted.
