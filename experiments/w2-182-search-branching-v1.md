# Whole-rollout branching evidence: `full_clone/current_game_v1`

Contract: `manabot.search-branching.v1` (`9c8dfc3cae6bb85642caf61c074bb3ba2c0845f555c3024238f3d2d6fa85708f`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-16T17:32:31.018668Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 272.0 | 35366.3 | 0.258s / 0.272s / 0.272s | 16.1 MiB | 0.6 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 1888.6 | 248593.9 | 0.299s / 0.319s / 0.319s | 129.5 MiB | 2.5 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 295.6 | 28739.5 | 0.008s / 0.010s / 0.010s | 15.5 MiB | 3.7 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 319.7 | 29892.4 | 0.125s / 0.134s / 0.134s | 73.9 MiB | 22.3 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Branch lifecycle and journal counters

| Cell | eager forks | checkpoints | fork time | mark time | rollback time | journal peak | COW peak | journal entries | marks / commits / rollbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 128 | 3072 | 0.001s | 0.013s | 0.027s | null | null | null | null / null / null |
| `flat-saturated-64-v1` | 1024 | 24576 | 0.005s | 0.111s | 0.241s | null | null | null | null / null / null |
| `retained-single-8-v1` | 192 | 0 | 0.001s | 0.000s | 0.000s | null | null | null | null / null / null |
| `retained-saturated-16-v1` | 3072 | 0 | 0.015s | 0.000s | 0.000s | null | null | null | null / null / null |

`null` counters are unsupported by this driver, not observed zeros: system allocator has no counting hook; full-clone baseline has no undo journal or page-COW counters.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 123878.8 | 7.0µs / 17.3µs / 23.5µs | 116 | 0.002s |
| `clone-v1` | 154460.2 | 6.3µs / 7.9µs / 8.1µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run --driver full_clone/current_game_v1
uv run scripts/bench_branching.py verify --driver full_clone/current_game_v1
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `181e1155714584b9a2bf1321fd672efff8c7a7ec1810d8644156cd6a6c0c99f7`.
Source SHA-256: `63e73de34add80719522d57f76606e172e645a29d3c690bbdf55556996e10170`.
Measurement revision: `81b189624265c46a96c25609d9f6dd29e339e166`.
Release binary SHA-256: `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83`.
Source method: `git-ls-tree-sha256-v1` over 126 tracked paths recorded in the raw receipt.

`source_sha256` hashes the canonical NUL-delimited Git tree entries for the receipt's explicit source closure. Run and verification fail closed when an admitted input is dirty, missing, or gains an undeclared production compile-time dependency. Checkout paths, `.git` representation, `.claude` worktrees, generated evidence, and unrelated tracked files are excluded by construction; changing an admitted engine, embedded-content, harness, dependency, test, or contract input requires regenerating every candidate receipt.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums.

This artifact measures one candidate. The matrix verifier compares all three candidates from the same host, source revision, and release binary and requires equal logical work before the decision record is accepted.
