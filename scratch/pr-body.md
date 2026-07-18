## Try it!

```bash
./scripts/verify-tla-jeong-increment
```

The verifier runs focused debug Rust tests, checks both semantic compiler
snapshots, rebuilds the CPython extension through uv, runs the Etude pack/session
tests, and verifies the retained evidence. It reports literal seeds 1 and 3
reaching terminal through production WebSocket, direct headless execution, and
persisted canonical replay with zero state/event mismatches or fallbacks.

Measured on the recorded host: live inner semantic p95 2.477 ms,
headless/replay 1041.4/1039.8 steps/s, and peak RSS below 512 MiB. The inherited
live outer p95 and completion budgets remain honest report-only misses at
106.653 ms and 0.324 games/s.

## Intent

Advance the Playable Curated World with the smallest creator-selected TLA
increment that proves real terminal play rather than a coverage marker. One
Jeong Jeong's Deserters replaces one Water Tribe Rallier in a separate GW deck
variant and executes through admitted typed programs, structured Commands,
committed events, and canonical replay.

## Assumptions

- Exact complete decklists, not a requested pack string, select compiled rules
  semantics.
- The original authored world and frozen RUL-9 evidence remain immutable.
- A future learning-schema migration owns the new `Rebel` subtype; this Rules
  slice preserves exact card semantics and does not silently encode it.

## Key decisions

- Add one immutable `tla-jeong-increment-v1` pack and one exact fail-closed pack
  catalog as the sole reusable kernel extension.
- Preserve all default pack/deck identities and compatibility hashes.
- Freeze the two literal terminal tapes with linked cast → target Command →
  +1/+1 counter witnesses and retain their canonical replays.
- Treat live outer latency/throughput as observed product misses without
  broadening this bounded admission into optimization.

## Not included

No new opcode or schema, frontend interaction changes, Intelligence work,
broad TLA completion, frozen RUL-9 mutation, or live-release optimization.
