# W2-182: whole-rollout branching baseline

Contract: `manabot.search-branching.v1` (`26e75b91d1df647a450ae4a977bde3b7421efd2733707d3803179e2c438353bc`)
Driver: `full_clone/current_game_v1`
Run: `2026-07-15T15:10:05.186920-07:00`; canonical: `true`

## Primary whole-rollout evidence

| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| `flat-single-64-v1` | 726.2 | 94407.5 | 0.157s / 0.170s / 0.170s | 8.6 MiB | 0.3 MiB | 3 | 0.000% |
| `flat-saturated-64-v1` | 5322.3 | 700562.2 | 0.175s / 0.202s / 0.202s | 69.3 MiB | 3.0 MiB | 3 | 0.000% |
| `retained-single-8-v1` | 676.6 | 65787.6 | 0.006s / 0.007s / 0.007s | 13.8 MiB | 3.5 MiB | 17 | 0.000% |
| `retained-saturated-16-v1` | 837.4 | 78302.1 | 0.090s / 0.092s / 0.092s | 97.6 MiB | 12.1 MiB | 264 | 0.000% |

Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.

## Step and clone diagnostics

| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |
|---|---:|---:|---:|---:|
| `step-v1` | 162707.6 | 5.5µs / 12.1µs / 16.2µs | 116 | 0.007s |
| `clone-v1` | 31518.6 | 30.8µs / 36.6µs / 42.1µs | 0 | 0.000s |

## Reproduction and evidence

```bash
uv run scripts/bench_branching.py run
uv run scripts/bench_branching.py verify
```

Equivalence: `true` across 8 fixture/seed checks, each replayed twice.
Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.
Artifact SHA-256: `aea588b65d9e3faff3f224bd7123cd3b579bbe98f80979f88b54f4c9ae64ec7f`.
Source SHA-256: `77510d5506cae056da82ba821ed301a2ad41935e9ee57b777c69477f57e4020e`.

The raw artifact contains hardware, versions, exact commands, fixture tapes and hashes, all worker records, all seeds, timings, outcomes, deterministic checksums, and RSS samples/summaries. No undo or page-COW implementation was selected or measured.
