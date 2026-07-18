# INT-14 kickoff — interpretation and assumptions

- "Belief-conditioned" here means the student is conditioned on a **provided**
  condition/belief label carried by the shard. It does **not** mean a learned
  belief head, range net, or PBS value vector. The `02-beliefs-design.md` wave
  stays dormant/trigger-armed.
- "Immutable conditional shard/manifest contract" = a `schema_version: 2`
  snapshot mirroring `run_teacher0_partial_snapshot.py` (CLAIM_BOUNDARY,
  SnapshotError, atomic write, `snapshot_identity_sha256`, freeze/verify
  fail-closed), extended with a condition axis in the NPZ and a `condition_schema`
  block in the manifest.
- `ConditionalStrategyResult` (INT-13) does not exist yet. This task pins a
  frozen, digest-bound shape contract and exercises the adapter with a
  **synthetic uniform-determinization toy producer** that conforms to the same
  shape. When INT-13 ships, ingestion requires no contract change.
- "Smallest ablation" = matched arms `policy_only`/`policy_value` vs
  `belief_conditioned_policy_only`/`belief_conditioned_policy_value` from one
  frozen conditional fixture, reusing `train_search_supervised` + the existing
  `_matchup`/`_student_vs_random` arena. Only difference: conditioning on/off.
- "Do not overclaim toy strength" = the toy condition is an uninformative
  uniform determinization; the pre-registered prediction is **~0 strength gap**
  (consistent with `02-beliefs-design.md:192-194`). The receipt is
  plumbing/measurement integrity, not strength. `CLAIM_BOUNDARY.strength_claim`
  is `False`.
- The frozen 512-game Teacher-0 snapshot is **not on disk** in this worktree
  (`.runs/` is gitignored). The toy conditional fixture is a new small frozen
  shard generated in this PR; the design does not depend on the 512-game
  snapshot being present.
- No Rust, no arena/rating, no Study/protocol changes. Conditioning is a Python
  obs-field + shard-key extension; the arena is reused as-is.

## Open questions (none blocking; proceed with best judgment)

- Exact K for the toy fixture: K=1 (trivial, proves plumbing ≡ no-op) vs a small
  K from `determinized_puct` per-world arrays (proves the student *can*
  condition). Assumption: start with K=1 for the contract tests, add a small
  multi-condition toy for the ablation. Resolve at implement time.
- Whether `condition_root_value` is needed in this slice. Assumption: optional
  key, omitted unless the toy producer yields per-condition values cheaply.
