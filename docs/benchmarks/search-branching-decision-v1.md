# Search branching decision v1

Decision: **retain compact full clone as the production default**.

Do not integrate either optimized representation. Keep both as conformance/benchmark drivers and remove them from production hot paths.

The decision applies the pre-registered whole-rollout thresholds in `dense-page-cow-prereg-v1.md`. Correctness and matched provenance are absolute gates; clone latency is diagnostic only.

## Matched primary evidence

| Driver | Cell | sims/s | vs full | p99 root | vs full | peak RSS | vs full | RSS delta | vs full |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| full clone | `flat-saturated-64-v1` | 1636.4 | 1.000x | 0.399s | 1.000x | 129.4 MiB | 1.000x | 2.6 MiB | 1.000x |
| full clone | `flat-single-64-v1` | 283.3 | 1.000x | 0.259s | 1.000x | 16.0 MiB | 1.000x | 0.6 MiB | 1.000x |
| full clone | `retained-saturated-16-v1` | 286.6 | 1.000x | 0.140s | 1.000x | 75.8 MiB | 1.000x | 24.2 MiB | 1.000x |
| full clone | `retained-single-8-v1` | 229.3 | 1.000x | 0.013s | 1.000x | 16.1 MiB | 1.000x | 4.6 MiB | 1.000x |
| clone + undo | `flat-saturated-64-v1` | 599.4 | 0.366x | 1.452s | 3.640x | 205.0 MiB | 1.585x | 21.2 MiB | 8.065x |
| clone + undo | `flat-single-64-v1` | 237.5 | 0.838x | 0.385s | 1.488x | 24.5 MiB | 1.532x | 4.9 MiB | 7.850x |
| clone + undo | `retained-saturated-16-v1` | 188.4 | 0.657x | 0.305s | 2.170x | 76.4 MiB | 1.008x | 22.9 MiB | 0.948x |
| clone + undo | `retained-single-8-v1` | 136.1 | 0.594x | 0.023s | 1.782x | 15.5 MiB | 0.964x | 3.9 MiB | 0.832x |
| event-page COW + undo | `flat-saturated-64-v1` | 611.2 | 0.374x | 1.296s | 3.249x | 201.6 MiB | 1.558x | 22.2 MiB | 8.458x |
| event-page COW + undo | `flat-single-64-v1` | 152.2 | 0.537x | 0.671s | 2.592x | 24.1 MiB | 1.508x | 5.1 MiB | 8.125x |
| event-page COW + undo | `retained-saturated-16-v1` | 146.9 | 0.513x | 0.326s | 2.320x | 38.1 MiB | 0.503x | 2.3 MiB | 0.096x |
| event-page COW + undo | `retained-single-8-v1` | 135.6 | 0.592x | 0.024s | 1.834x | 13.6 MiB | 0.847x | 3.0 MiB | 0.646x |

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

- `full_clone/current_game_v1`: `experiments/data/w2-182-search-branching-v1.json`; artifact `e954f8514753861a5a0f15d4ef6a7d0b0ad378862fe4f520392da1cd91e47682`.
- `compact_clone_undo/current_game_v1`: `experiments/data/w2-198-compact-clone-undo-v1.json`; artifact `c0bb1c408b8d430244e20f6a3ee65f1d582947d316e127ee04782088b86864c0`.
- `dense_page_cow_undo/event_pages_4k_v1`: `experiments/data/w2-199-dense-page-cow-undo-v1.json`; artifact `8609d92cdf00c1b13166aa72078f22ba687c6c425fec32ebce6a4c392a0bf20d`.

All three receipts use source `d5036a39bf5f04a2008f48e6c0a5909cf5cb787dd73cc1b5b066635647569764`, revision `ce15504097654bfbb28555621d1bc827610d53a0`, and release binary `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83` on the identical recorded host.
Logical fixture summaries, seeds, workload dimensions, result checksums, outcomes, caps, and sampled final hashes match exactly across candidates.
