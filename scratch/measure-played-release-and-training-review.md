# RUL-9 Review Guide

## What was implemented

RUL-9 measures the fixed UR Lessons versus GW Allies release tape across live
WebSocket play, direct headless execution, and persisted canonical replay, plus
the selected four-worker `full_clone/current_game_v1` PUCT training workload.
It retains command latency, step and complete-game throughput, peak RSS,
semantic-program capacity, terminal/parity identities, and every required
fallback/cap counter under pre-registered budgets.

The checked evidence has one immutable schema-v1 measurement authority and one
small schema-v2 derivation receipt. The latter contains no raw corpus: it binds
the origin path and identities, canonical raw hash and byte length, current
derivation identity, rederived summary, verdict, and contract.

## Key choices

- Keep artifact `498df1` and its 2.3 MB schema-v1 origin byte-for-byte as the
  sole raw measurement source. The schema-v2 receipt never duplicates or
  claims authorship of those samples.
- Verify the immutable origin completely, rederive its summary and verdict
  from raw samples, then compare the metadata-only derived receipt. Report
  rendering consumes only that verified derivation.
- Gate release and training independently so fast headless or training work
  cannot hide player-facing live latency.
- Retain `full_clone/current_game_v1`. The training, semantic-capacity, memory,
  and fallback gates pass; the observed release miss does not provide
  decision-bearing evidence for a branch-representation change.

## How it fits together

The contract pre-registers workload shapes and budgets. The immutable
measurement origin records all raw release and training samples, while the
metadata-only receipt binds that origin to the current verifier/report
derivation. `./scripts/verify-rul9-played-workloads` rebuilds the pinned native
extension, verifies both identity layers, rederives all metrics from origin
raw, and applies the product budgets fail-closed.

## Risks and bottlenecks

- The strongest confound is single-host performance. The fixed run misses only
  live WebSocket Command p95 (`150.492 ms` vs `100 ms`) and live complete-game
  throughput (`0.347 games/s` vs `1.0 games/s`).
- Native semantic apply p95 is `4.291 ms` and passes its `10 ms` attribution
  bound, pointing at presentation/protocol/process scheduling rather than the
  rules engine or branch representation.
- The verifier intentionally exits `2` for the checked MISS. A zero exit would
  require all product gates to pass; identity, tamper, or derivation failures
  remain hard errors rather than budget misses.
- Source, binary, origin-file, raw-corpus, summary, verdict, or derivation drift
  invalidates the receipt.

## What's not included

- No workload rerun, budget adjustment, or mutation of artifact `498df1`.
- No rules, Command, offer, replay, search, branch, semantic projection, or
  binding-kernel change.
- No second branch representation and no optimization based solely on the
  host-sensitive release miss.
- No changes to RUL-5 evidence or the concurrent INT-9 frontier.

## Validation

- `uv run --extra dev pytest -q tests/experiments/test_rul9_played_workloads.py`
  — 17 passed.
- `./scripts/verify-rul9-played-workloads` — rebuilt the release extension,
  verified origin and derivation identity, then exited `2` only for the two
  pre-registered live release misses.
- Current-head CI passed on approved head `0ce3afa`.

