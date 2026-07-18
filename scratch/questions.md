# Review Decisions

- 2026-07-17: Parent-Project review `ir_61999e0c4a6848419888a3420780594b`
  confirmed the production base is the complete five-player code-only cohort
  and approved freezing its round robin once; subsequent candidates play only
  challenger-versus-anchor cells on the same 24 paired deal blocks.
- 2026-07-17: The parent-reviewed preregistered numerical choices are final:
  flat-MC budgets 4/16/64, a visible 400-Elo Gaussian MAP prior, a +25 Elo
  promotion margin, 10% relative latency/throughput/RSS tolerances, and a
  10-percentage-point competency noninferiority margin.
- 2026-07-17: Base v1 freezes exactly random, scripted-greedy, and flat-MC
  4/16/64. It requires no learned checkpoint or cross-Task artifact and is not
  a preliminary cohort awaiting one.
- 2026-07-17: Missing exact learned incumbent bytes are a documented
  limitation: learned-class promotion is unavailable in base v1. Adding a
  learned anchor requires separate future authorization, a new cohort contract,
  and a new `anchor_cohort_sha256`; frozen base-v1 ratings are never appended,
  mutated, or joined across cohort keys.
- 2026-07-18: Parent review selected the first production challenger as
  `determinized-puct-32-w4-v1` through the existing
  `flat_mc.make_player(kind="determinized_puct")` seam. Its identity pins 32
  total traversals, four worlds, `c_puct=1.5`, a 2,000-step cap,
  `full_clone/current_game_v1`, uniform-prior/random-terminal-leaf semantics,
  both seed derivations, the complete source bundle, and compute class. With no
  same-class incumbent it returns
  `rated_not_promotion_eligible/incumbent_not_in_cohort`.
- 2026-07-18: Checkpoint challengers remain content-addressed registrations,
  but available fixture bytes prove only
  `engineering_smoke_non_promotion`. A production checkpoint candidate with
  missing or digest-invalid bytes fails preflight and emits no rating; the
  current production path has no checkpoint dependency.

No implementation-blocking design questions remain.
