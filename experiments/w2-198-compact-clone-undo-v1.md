# Whole-rollout branching evidence: `compact_clone_undo/current_game_v1`

Contract: `manabot.search-branching.v1` (`9c8dfc3cae6bb85642caf61c074bb3ba2c0845f555c3024238f3d2d6fa85708f`)
Driver: `compact_clone_undo/current_game_v1`
Run: `2026-07-16T08:59:41.896015Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 298.2 | 38763.8 | 0.288s / 0.306s / 0.306s | 24.6 MiB | 6.8 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 2126.1 | 279856.7 | 0.330s / 0.362s / 0.362s | 193.4 MiB | 20.4 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 334.3 | 32506.4 | 0.008s / 0.010s / 0.010s | 14.4 MiB | 3.4 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 370.4 | 34630.8 | 0.120s / 0.126s / 0.126s | 63.4 MiB | 14.1 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Branch lifecycle and journal counters

| Cell | eager forks | checkpoints | fork time | mark time | rollback time | journal peak | journal entries | marks / commits / rollbacks |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 128 | 3072 | 0.000s | 0.000s | 0.399s | 4.64 MiB | 34385 | 6144 / 3072 / 3072 |
| `flat-saturated-64-v1` | 1024 | 24576 | 0.004s | 0.001s | 3.457s | 4.65 MiB | 35727 | 49152 / 24576 / 24576 |
| `retained-single-8-v1` | 192 | 0 | 0.001s | 0.000s | 0.000s | 0.11 MiB | 86 | 128 / 128 / 0 |
| `retained-saturated-16-v1` | 3072 | 0 | 0.009s | 0.000s | 0.000s | 1.66 MiB | 86 | 2048 / 2048 / 0 |

`null` counters are unsupported by this driver, not observed zeros: system allocator has no counting hook; clone-plus-undo does not implement page COW.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 130258.3 | 6.6µs / 16.5µs / 22.1µs | 116 | 0.001s |
| `clone-v1` | 223591.1 | 4.5µs / 4.6µs / 4.8µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run --driver compact_clone_undo/current_game_v1
uv run scripts/bench_branching.py verify --driver compact_clone_undo/current_game_v1
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `3414aa3f80f4c803de31040b4f75a2232a22e65d50429b2572095888dace3d87`.
Source SHA-256: `7285d2c8288296ea8ad9e253f66898e7544f05f9ed982c7378e105c1810fe63f`.

`source_sha256` hashes the whole tree except generated evidence, so this receipt is bound to one exact source state: it must be generated at the final landing tree and landed before `origin/main` moves under it. Any later rebase or source edit, even one touching no benchmark file, invalidates it and requires re-running `run`; re-check `verify` immediately before submitting. Regenerating this receipt is also what repairs the W2-182 baseline, which had been left failing `verify` with a contract-digest mismatch after the witness refactor edited the contract and benchmark without regenerating the artifact.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums.

This artifact measures one candidate. Comparing candidates means comparing two artifacts from the same host and source state, matched by their equal per-cell result checksums. No page-COW representation is implemented, measured, or selected here.
