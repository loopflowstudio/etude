# jack-heart.first-light.20260305_1906

## Current reality

Rebased cleanly onto `origin/main`. PR #35 is OPEN and MERGEABLE with no
reviews; CI checks (Rust Tests, Python Unit Tests) not yet conclusive.

This branch delivers wave goal #4 ("Verification harness — done") plus the
reward-shaping and periodic-eval plumbing it depends on:

- Progress reward shaping (`land_play`, `creature_play`, `opponent_life_loss`)
  in both `Match` (single-env) and `VectorEnv` (batched tensor path), wired
  through `Hypers`.
- Periodic in-training eval logging win/land/cast/attack rates and action
  probability breakdowns.
- First-light harness: `run_first_light`, `report_first_light`,
  `first_light.py` orchestration, SQLite `store.py`, expanded eval utilities
  in `util.py`, `diagnose_initial_policy.py`.
- Value head: mean-pool + ReLU instead of max-pool.
- Wave bookkeeping: closed items 01–04, README goals 1–4 struck through;
  removed stale `wave/structure/` and `wave/rules/` planning docs.
- Latest commit drops `could_spell` from recommendation gating and report
  rendering (metric is still captured in the store).

Tests: `tests/verify/test_first_light.py` passes (3 tests) after the
`could_spell` change; `could_spell` still consistently referenced in
`train.py`/`util.py`/`store.py` (storage only — intentional).

## Done

- ~~Fix PPO bugs / single-agent training / clean observation space~~ (shipped on main)
- Reward shaping in `Match` and `VectorEnv`, exposed via `Hypers`
- Periodic evaluation with behavioral metrics
- First-light orchestration, SQLite store, report generation, eval utilities
- Value head mean-pool change
- `could_spell` removed from recommendation/report logic
- Wave roadmap updated (items 01–04 closed, stale structure/rules docs removed)

## Remaining

- Land PR #35: get CI green (Rust Tests, Python Unit Tests) and merge.
- No code changes expected — this branch is feature-complete for goal #4.

## Deferred

- Wave goal #5 (auxiliary prediction heads for dense training signal) —
  tracked in `wave/first-light/05-auxiliary-heads.md`, future branch.
- `run_first_light eval` does not resume from a saved checkpoint; it records
  an eval-only run with `total_timesteps=0` (see `scratch/questions.md`).
  Acceptable for this PR; revisit when checkpoint persistence lands.

## Risks / blockers

- None blocking merge. CI not yet conclusive at time of writing — verify
  before landing.

## Strategy: ship bias

- Finish only what's trivial and in-scope for this branch
- Defer anything non-trivial into the wave's roadmap, or `scratch/questions.md` if waveless
- Prefer landing over comprehensive — a wave doc captures intent
