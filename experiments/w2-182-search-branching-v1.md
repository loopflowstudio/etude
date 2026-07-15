# W2-182: whole-rollout branching baseline

Contract: `manabot.search-branching.v1` (`a3a6c721bd4a349787d32491e04103d81ce0ee3d71318bb83046414e3ace6089`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-15T22:54:24.851674Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 734.3 | 95461.6 | 0.148s / 0.169s / 0.169s | 8.2 MiB | 1.2 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 5100.4 | 671355.7 | 0.178s / 0.245s / 0.245s | 64.9 MiB | 2.8 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 759.1 | 73806.8 | 0.005s / 0.006s / 0.006s | 7.5 MiB | 1.0 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 899.2 | 84077.6 | 0.076s / 0.079s / 0.079s | 51.0 MiB | 16.2 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 159014.6 | 5.6µs / 12.6µs / 17.5µs | 116 | 0.001s |
| `clone-v1` | 324094.6 | 3.0µs / 3.7µs / 4.0µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `7bfff3ed84c9bb5f59fb7543c3401ea1d6a5fae5165e9bf338ff8e33959882fa`.
Source SHA-256: `c81fc4d528678dbe94f610fda97ceb6252a8bc1dd3c58baaa802459094ca9828`.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums. No undo or page-COW implementation was selected or measured.
