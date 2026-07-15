# W2-182: whole-rollout branching baseline

Contract: `manabot.search-branching.v1` (`a3a6c721bd4a349787d32491e04103d81ce0ee3d71318bb83046414e3ace6089`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-15T22:58:54.614508Z`; canonical: `true`
Evidence scope: current driver at this source state only; this is not a W2-179 before/after comparison.

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 739.7 | 96164.4 | 0.151s / 0.161s / 0.161s | 8.0 MiB | 0.2 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 5494.2 | 723191.4 | 0.167s / 0.184s / 0.184s | 66.4 MiB | 1.5 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 768.6 | 74736.1 | 0.005s / 0.006s / 0.006s | 8.1 MiB | 1.3 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 898.8 | 84043.1 | 0.076s / 0.080s / 0.080s | 55.4 MiB | 18.0 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 159161.0 | 5.7µs / 12.5µs / 16.5µs | 116 | 0.001s |
| `clone-v1` | 317040.1 | 3.0µs / 3.8µs / 4.0µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `bd937fb4abc03359f622e175425916abbbbbe535b98b15952170e67296fcfd36`.
Source SHA-256: `ace6bb6f61f659d896bc584d290ac888010a108b964c894d70118f9dd9485ee3`.

The raw artifact contains the pre-execution UTC start, exact orchestrator and worker argv, a fresh process group per cell/repeat, barrier and sampler receipts, the complete timestamped 5 ms aggregate-worker RSS series, hardware, versions, fixture tapes and hashes, all worker results, seeds, timings, outcomes, and deterministic checksums. No undo or page-COW implementation was selected or measured.
