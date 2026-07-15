# W2-182: whole-rollout branching baseline

Contract: `manabot.search-branching.v1` (`a3a6c721bd4a349787d32491e04103d81ce0ee3d71318bb83046414e3ace6089`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-15T22:41:04.074392Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 731.8 | 95140.3 | 0.148s / 0.160s / 0.160s | 8.0 MiB | 0.8 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 2541.5 | 334537.5 | 0.230s / 0.766s / 0.766s | 66.2 MiB | 3.7 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 649.5 | 63149.6 | 0.006s / 0.009s / 0.009s | 9.0 MiB | 2.1 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 884.2 | 82676.2 | 0.076s / 0.081s / 0.081s | 46.5 MiB | 11.9 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 163054.6 | 5.5µs / 12.0µs / 15.8µs | 116 | 0.001s |
| `clone-v1` | 339602.3 | 2.9µs / 3.5µs / 3.7µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `20c403beed10408a84b80428986e7cb6acbcc4f09782260bfededba34c897bf1`.
Source SHA-256: `00b9dfa232c4f5ac7167c6c5f3e9566886af2ec5fb6103accef8fe4fd9ba8cd6`.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums. No undo or page-COW implementation was selected or measured.
