## Try it!

Run the focused lineage, tamper, derivation, and report tests:

```bash
uv run --extra dev pytest -q tests/experiments/test_rul9_played_workloads.py
```

They pass 17/17. Then run the product verifier:

```bash
./scripts/verify-rul9-played-workloads
```

It rebuilds the pinned release extension, fully verifies immutable measurement
artifact `498df1` and the metadata-only derivation, rederives every metric from
origin raw, and intentionally exits `2` for the checked product MISS. Only two
release gates miss: live WebSocket Command p95 is `150.492 ms` against `100 ms`,
and live complete-game throughput is `0.347 games/s` against `1.0 games/s`.
Training, capacity, memory, fallback, fixed-tape parity, and all other release
gates pass.

## Intent

This change gives Etude release and manabot training owners one source-bound,
fail-closed receipt for the current curated world as actually played. It runs
the fixed UR Lessons versus GW Allies authority tape across live, headless, and
persisted replay surfaces and measures the selected production PUCT workload
through revision-bound semantic Commands.

## Assumptions

- The checked schema-v1 origin is the sole immutable raw authority for this
  run; artifact `498df1`, its source closure, native binary, raw corpus, and
  original verdict remain unchanged.
- Performance is single-host evidence. The receipt keeps every raw sample and
  attribution needed to distinguish engine time from consumer/host overhead.
- A budget miss is admission evidence, not permission to tune budgets or
  replace the selected branch representation.

## Key decisions

- Keep the 2.3 MB raw corpus only in
  `rul-9-played-workloads-v1.measurement.json`. The small schema-v2 receipt
  binds its path, exact identities, raw hash/length, contract, summary,
  verdict, and current derivation/report source without duplicating samples.
- Fully verify and rederive the origin before accepting or rendering the
  derived receipt. Origin, lineage, summary, verdict, and derivation drift all
  fail closed.
- Report release and training verdicts independently. The live miss cannot be
  hidden by fast direct engine or training execution.
- Retain `full_clone/current_game_v1`: training delivers `4.520 roots/s`,
  `578.5 traversals/s`, and `0.0429 games/s`, with all training budgets and
  fallback counters passing.

## Not included

- No workload rerun, budget change, or rewrite of immutable origin evidence.
- No rules, Command, replay, search, branch, semantic projection, or shared
  kernel changes.
- No second representation, RUL-5 evidence changes, or INT-9 frontier edits.

