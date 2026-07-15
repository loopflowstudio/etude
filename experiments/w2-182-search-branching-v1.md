# W2-182: whole-rollout branching baseline

Contract: `manabot.search-branching.v1` (`e3c9ead46f3d5cf7b53f21bfa47ed4b7f4cae10399256a5a01694c737133587b`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-15T15:27:14.534980-07:00`; canonical: `true`

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 611.7 | 79528.5 | 0.172s / 0.218s / 0.218s | 8.3 MiB | 0.5 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 2130.0 | 280372.9 | 0.397s / 0.860s / 0.860s | 66.5 MiB | 1.5 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 743.7 | 72317.8 | 0.005s / 0.006s / 0.006s | 8.3 MiB | 1.5 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 900.8 | 84228.4 | 0.075s / 0.078s / 0.078s | 46.5 MiB | 10.4 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 158889.0 | 5.7µs / 12.5µs / 16.5µs | 116 | 0.001s |
| `clone-v1` | 333154.5 | 2.9µs / 3.2µs / 3.7µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `3251d5ad45083a69298219c4c162e6cde431977db3430e819873307ff854a1a5`.
Source SHA-256: `1b49d48dec3d47c6fa4923467c7f9957865c3b3b8ca642b193d2d583b8b66774`.

The raw artifact contains hardware, versions, exact commands, fixture tapes and hashes, all worker records, all seeds, timings, outcomes, deterministic checksums, and RSS samples/summaries. No undo or page-COW implementation was selected or measured.
