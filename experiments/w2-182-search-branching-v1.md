# W2-182: whole-rollout branching baseline

Contract: `manabot.search-branching.v1` (`26e75b91d1df647a450ae4a977bde3b7421efd2733707d3803179e2c438353bc`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-15T15:13:12.726581-07:00`; canonical: `true`

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 592.8 | 77064.2 | 0.175s / 0.245s / 0.245s | 8.8 MiB | 0.2 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 2658.5 | 349938.7 | 0.348s / 0.512s / 0.512s | 72.5 MiB | 1.7 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 463.1 | 45032.7 | 0.009s / 0.011s / 0.011s | 13.2 MiB | 2.9 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 677.9 | 63383.2 | 0.107s / 0.143s / 0.143s | 99.7 MiB | 15.0 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 113975.2 | 7.7µs / 17.3µs / 25.1µs | 116 | 0.011s |
| `clone-v1` | 24720.6 | 39.3µs / 48.9µs / 57.2µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `b571f604520f5be371e30f644e1559d2e7eec3153b5f209057fd4c294680999b`.
Source SHA-256: `69b55738768776a52ca94e57b1d03980c8ad3f785fa336dd785555f505c3a29a`.

The raw artifact contains hardware, versions, exact commands, fixture tapes and hashes, all worker records, all seeds, timings, outcomes, deterministic checksums, and RSS samples/summaries. No undo or page-COW implementation was selected or measured.
