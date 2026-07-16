# Search branching decision v1

Decision: **retain compact full clone as the production default**.

Do not integrate either optimized representation. Keep both as conformance/benchmark drivers and remove them from production hot paths.

The decision applies the pre-registered whole-rollout thresholds in `dense-page-cow-prereg-v1.md`. Correctness and matched provenance are absolute gates; clone latency is diagnostic only.

## Matched primary evidence

| Driver | Cell | sims/s | vs full | p99 root | vs full | peak RSS | vs full | RSS delta | vs full |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| full clone | `flat-saturated-64-v1` | 1888.6 | 1.000x | 0.319s | 1.000x | 129.5 MiB | 1.000x | 2.5 MiB | 1.000x |
| full clone | `flat-single-64-v1` | 272.0 | 1.000x | 0.272s | 1.000x | 16.1 MiB | 1.000x | 0.6 MiB | 1.000x |
| full clone | `retained-saturated-16-v1` | 319.7 | 1.000x | 0.134s | 1.000x | 73.9 MiB | 1.000x | 22.3 MiB | 1.000x |
| full clone | `retained-single-8-v1` | 295.6 | 1.000x | 0.010s | 1.000x | 15.5 MiB | 1.000x | 3.7 MiB | 1.000x |
| clone + undo | `flat-saturated-64-v1` | 1689.9 | 0.895x | 0.474s | 1.486x | 200.3 MiB | 1.547x | 21.4 MiB | 8.544x |
| clone + undo | `flat-single-64-v1` | 244.8 | 0.900x | 0.395s | 1.456x | 26.0 MiB | 1.619x | 6.1 MiB | 9.800x |
| clone + undo | `retained-saturated-16-v1` | 333.5 | 1.043x | 0.136s | 1.016x | 79.0 MiB | 1.069x | 25.6 MiB | 1.149x |
| clone + undo | `retained-single-8-v1` | 303.0 | 1.025x | 0.010s | 0.989x | 15.3 MiB | 0.982x | 3.6 MiB | 0.974x |
| event-page COW + undo | `flat-saturated-64-v1` | 718.5 | 0.380x | 1.381s | 4.333x | 200.0 MiB | 1.544x | 22.7 MiB | 9.094x |
| event-page COW + undo | `flat-single-64-v1` | 259.4 | 0.954x | 0.361s | 1.329x | 24.4 MiB | 1.520x | 4.9 MiB | 7.900x |
| event-page COW + undo | `retained-saturated-16-v1` | 337.3 | 1.055x | 0.145s | 1.081x | 38.1 MiB | 0.516x | 2.3 MiB | 0.102x |
| event-page COW + undo | `retained-single-8-v1` | 156.4 | 0.529x | 0.022s | 2.074x | 14.0 MiB | 0.903x | 3.3 MiB | 0.902x |

## Threshold results

- Clone-plus-undo sequential bar: `fail`.
- Page-COW retained-memory bar: `fail`.
- Page-COW general-driver bar: `fail`.

## Provenance

- `full_clone/current_game_v1`: `experiments/data/w2-182-search-branching-v1.json`; artifact `181e1155714584b9a2bf1321fd672efff8c7a7ec1810d8644156cd6a6c0c99f7`.
- `compact_clone_undo/current_game_v1`: `experiments/data/w2-198-compact-clone-undo-v1.json`; artifact `42f0818a098fd81527689a6038e278637e40d3d771927c282760ec1e2a944311`.
- `dense_page_cow_undo/event_pages_4k_v1`: `experiments/data/w2-199-dense-page-cow-undo-v1.json`; artifact `671c35bb179acd15f4eea6678558065107157550721aba3be0a775371bb1cf41`.

All three receipts use source `63e73de34add80719522d57f76606e172e645a29d3c690bbdf55556996e10170`, revision `81b189624265c46a96c25609d9f6dd29e339e166`, and release binary `af197d6d7f8297cdf2cdf72a2b417af328310cac33ef5f78bffaa6e2a5e80a83` on the identical recorded host.
Logical fixture summaries, seeds, workload dimensions, result checksums, outcomes, caps, and sampled final hashes match exactly across candidates.
