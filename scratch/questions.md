# Review Decisions

- 2026-07-17: Parent-Project review `ir_61999e0c4a6848419888a3420780594b`
  approved freezing the complete anchor round robin once and content-addressing
  it; subsequent candidates play only challenger-versus-anchor cells on the
  same 24 paired deal blocks.
- 2026-07-17: The preregistered numerical choices are approved: flat-MC budgets
  4/16/64, a visible 400-Elo Gaussian MAP prior, a +25 Elo promotion margin,
  10% relative latency/throughput/RSS tolerances, and a 10-percentage-point
  competency noninferiority margin.
- 2026-07-17: Base v1 freezes exactly five code-only anchors: random,
  scripted-greedy, and flat-MC 4/16/64. It has no learned-checkpoint or
  cross-Task dependency.
- 2026-07-17: A checkpoint challenger may receive a production rating without
  an incumbent. Missing exact same-compute incumbent bytes yield typed
  `rated_not_promotion_eligible/exact_incumbent_unavailable`; fixture
  checkpoints remain smoke-only.
- 2026-07-17: Adding any learned anchor requires a new cohort contract and
  `anchor_cohort_sha256`. Frozen base-v1 evidence is never appended, mutated,
  or joined across cohort keys.

No implementation-blocking design questions remain.
