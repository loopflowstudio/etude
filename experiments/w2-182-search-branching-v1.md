# W2-182: whole-rollout branching baseline

Contract: `manabot.search-branching.v1` (`a3a6c721bd4a349787d32491e04103d81ce0ee3d71318bb83046414e3ace6089`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-15T22:47:47.275573Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 724.2 | 94148.2 | 0.153s / 0.163s / 0.163s | 8.0 MiB | 0.2 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 3680.4 | 484453.2 | 0.232s / 0.406s / 0.406s | 67.3 MiB | 1.3 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 763.2 | 74205.0 | 0.005s / 0.006s / 0.006s | 8.5 MiB | 1.7 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 883.9 | 82646.0 | 0.077s / 0.080s / 0.080s | 46.6 MiB | 10.5 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 159729.1 | 5.6µs / 12.3µs / 16.4µs | 116 | 0.002s |
| `clone-v1` | 325098.9 | 3.0µs / 3.2µs / 3.8µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `8f7157e4c0a384e3496fbf18af317e97f1fbba21467a2c76e50a0b4d5665d654`.
Source SHA-256: `65007fc33a1d84607a1fe11ded72303a69d09ab0567c32bacfa8a5de71ed9e47`.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums. No undo or page-COW implementation was selected or measured.
