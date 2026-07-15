# W2-182: whole-rollout branching baseline

Contract: `manabot.search-branching.v1` (`c5c035997f39ba1658f4b0cc66e5958778f5266a086cfc10a7e8f6d4ef4bd88a`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-15T22:39:47.613085Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 746.1 | 96996.3 | 0.147s / 0.156s / 0.156s | 7.9 MiB | 0.9 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 3154.0 | 415159.4 | 0.238s / 0.734s / 0.734s | 67.0 MiB | 2.4 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 628.0 | 61061.7 | 0.006s / 0.007s / 0.007s | 8.4 MiB | 1.7 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 776.9 | 72638.4 | 0.088s / 0.097s / 0.097s | 46.8 MiB | 9.2 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 162131.2 | 5.5µs / 12.2µs / 16.3µs | 116 | 0.001s |
| `clone-v1` | 332956.4 | 2.9µs / 3.7µs / 3.8µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `abd58035bcfcd4cf0abaa2d6ee430ec92accf0ac501d535877f98394863a982c`.
Source SHA-256: `5e1868c5150afb6d4558884098fcf51c1fadf2826354d9b8995b484057d1eaa0`.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums. No undo or page-COW implementation was selected or measured.
