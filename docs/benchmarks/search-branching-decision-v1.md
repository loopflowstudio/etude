# Search branching decision v1

Decision: **retain compact full clone as the production default**.

Do not integrate either optimized representation. Keep both as conformance/benchmark drivers and remove them from production hot paths.

The decision applies the pre-registered whole-rollout thresholds in `dense-page-cow-prereg-v1.md`. Correctness and matched provenance are absolute gates; clone latency is diagnostic only.

## Matched primary evidence

| Driver | Cell | sims/s | vs full | p99 root | vs full | peak RSS | vs full | RSS delta | vs full |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| full clone | `flat-saturated-64-v1` | 1317.1 | 1.000x | 0.672s | 1.000x | 130.2 MiB | 1.000x | 2.6 MiB | 1.000x |
| full clone | `flat-single-64-v1` | 285.2 | 1.000x | 0.266s | 1.000x | 16.1 MiB | 1.000x | 0.6 MiB | 1.000x |
| full clone | `retained-saturated-16-v1` | 337.7 | 1.000x | 0.153s | 1.000x | 74.1 MiB | 1.000x | 22.6 MiB | 1.000x |
| full clone | `retained-single-8-v1` | 313.1 | 1.000x | 0.010s | 1.000x | 16.0 MiB | 1.000x | 4.6 MiB | 1.000x |
| clone + undo | `flat-saturated-64-v1` | 1264.7 | 0.960x | 0.757s | 1.126x | 195.9 MiB | 1.504x | 21.8 MiB | 8.500x |
| clone + undo | `flat-single-64-v1` | 267.2 | 0.937x | 0.358s | 1.348x | 24.6 MiB | 1.522x | 5.1 MiB | 7.951x |
| clone + undo | `retained-saturated-16-v1` | 291.8 | 0.864x | 0.468s | 3.061x | 82.3 MiB | 1.110x | 28.9 MiB | 1.281x |
| clone + undo | `retained-single-8-v1` | 270.2 | 0.863x | 0.012s | 1.153x | 15.9 MiB | 0.994x | 4.3 MiB | 0.935x |
| event-page COW + undo | `flat-saturated-64-v1` | 1115.9 | 0.847x | 0.984s | 1.464x | 197.0 MiB | 1.513x | 21.6 MiB | 8.421x |
| event-page COW + undo | `flat-single-64-v1` | 209.2 | 0.734x | 0.649s | 2.439x | 23.6 MiB | 1.464x | 5.8 MiB | 9.000x |
| event-page COW + undo | `retained-saturated-16-v1` | 340.0 | 1.007x | 0.144s | 0.944x | 37.8 MiB | 0.510x | 2.1 MiB | 0.093x |
| event-page COW + undo | `retained-single-8-v1` | 227.0 | 0.725x | 0.027s | 2.598x | 14.0 MiB | 0.877x | 3.4 MiB | 0.736x |

## Threshold results

- Clone-plus-undo sequential bar: `fail`.
- Page-COW retained-memory bar: `fail`.
- Page-COW general-driver bar: `fail`.

Clone plus undo is rejected unless both flat cells reach 1.20x full-clone throughput without exceeding 1.10x p99 latency or RSS. Page COW is rejected for retained use unless both retained cells preserve at least 0.90x throughput and meet every registered RSS reduction; general use also requires the flat guardrails. The table above makes each failed comparison explicit.

## Branch lifecycle diagnostics

| Driver | eager full forks | full checkpoints | max journal peak | max COW peak |
|---|---:|---:|---:|---:|
| full clone | 4416 | 27648 | null | null |
| clone + undo | 4416 | 0 | 30.23 MiB | null |
| event-page COW + undo | 0 | 0 | 30.23 MiB | 1.01 MiB |

Full-clone and clone-plus-undo eager counts represent deep outer forks; only full clone takes full mark snapshots. Page COW reports zero full logical-state forks and checkpoints, with numeric copied-page peaks. Unsupported allocator totals remain null in the raw receipts.

## Provenance

- `full_clone/current_game_v1`: `experiments/data/w2-182-search-branching-v1.json`; artifact `a9a0d71bc6087526b1cdd593c50f6f57ad7a52b91b0cc680559dac49ba990c85`.
- `compact_clone_undo/current_game_v1`: `experiments/data/w2-198-compact-clone-undo-v1.json`; artifact `cd9940a51c0bb36d40bd9e9136fb65218e28001ae78360af1bd10fa20bfb1df1`.
- `dense_page_cow_undo/event_pages_4k_v1`: `experiments/data/w2-199-dense-page-cow-undo-v1.json`; artifact `207b335bf7857c31f06a98092057d6bc22a32c72505b25aefe331b80150e0a85`.

All three receipts use source `3c2ca12e87cbe7f5e739f89e5c394d379024739a107b3ea3f8eb5a421af9931d`, revision `16ac1a5f75660cad4af17cdf167a48dc790d5db7`, and release binary `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83` on the identical recorded host.
Logical fixture summaries, seeds, workload dimensions, result checksums, outcomes, caps, and sampled final hashes match exactly across candidates.
