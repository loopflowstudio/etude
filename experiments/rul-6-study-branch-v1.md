# Production Study fork/apply/return evidence

Contract: `etude.study-branch.contract.v1` (`91d522aa824566b5dc32922960ea307370c49232c442c33048d5e7e0bd10fb53`)
Driver: `full_clone/current_game_v1`
Command path: `structured_offers/step_structured_v1`
Run: `2026-07-18T06:18:21.666483Z`; canonical: `true`

## Interactive lifecycle

| Phase | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| Fork | 0.636 ms | 0.998 ms | 2.569 ms | 45.761 ms |
| Publish offers | 0.008 ms | 0.021 ms | 0.047 ms | 0.212 ms |
| Structured apply | 0.177 ms | 0.230 ms | 0.589 ms | 5.102 ms |
| Return | 0.245 ms | 0.328 ms | 0.989 ms | 27.808 ms |
| End to end | 1.067 ms | 1.825 ms | 4.561 ms | 52.884 ms |

Sustained rate: **678.4 cycles/s** across 2000 checked cycles.

## Retained siblings

Retained 512 simultaneous production branches; applied 256 and re-checked 256 untouched siblings.
Peak RSS delta: **53.5 MiB** (peak 147.0 MiB).

## Exactness, privacy, incarnation, and failure

- Source digest: `068c7029a81cf0ee08a0a338b7a6f6c2f1f00d80c442a40f0f62a61029c99fa2`.
- Bound object: entity `16`, incarnation `0`.
- Child-only zone-change checks: 2257; object-ref mismatches: 0.
- Viewer-private observation checks: 2257; opponent-hand exposures: 0.
- Typed failure cases: 9; rejected-branch mutations: 0.
- Study fallbacks: 0; source authority fallback counters remained zero.

## Reproduction and identity

```bash
uv run scripts/bench_study_branch.py measure
uv run scripts/bench_study_branch.py verify
```

Host: `macOS-26.0.1-arm64-arm-64bit`; Python `3.12.12`; logical CPUs: `16`.
Compiled extension SHA-256: `2335165aff82c05cdc53c011d0622626bc72de590c9a7556db32e53a8cc3b6a4`.
Source closure SHA-256: `9ac8a33f2b848156637efd45adced5d7dee5d804cb38e33b71cdc566eb2ee2cf` over 109 recorded paths.
Artifact SHA-256: `262d8697d2cf3bbcc9751851c0600cd0da3faebd54c7852b9cf8b85094255ee7`.

Raw evidence retains every latency sample, all RSS phase samples, exact source and binary identity, the workload address/object/return digests, failure receipts, and zero-mismatch counters.
