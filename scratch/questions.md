# GAM-6: Prototype belief-to-strategy comparison

## Directive

- 2026-07-18: Acknowledged the current directive version (v3) with the
  compatible release binary
  (`LF_HOME=/Users/jack/.lf LF_BIN=/Users/jack/src/loopflow/target/release/lf
  /Users/jack/src/loopflow/target/release/lf`, `lf 0.11.3`,
  `LF_WAVE_ID=game`): `GAM-6 incorporated directive v3`
  (`current_directive_version` 3, `incorporated_directive_version` 3;
  receipt `dir_8c130eaed599478e91294ae3a619455f`). PR #139 (belief-conditioned
  system architecture) was integrated from `origin/main` as the execution-plan
  context via `lf rebase --manual origin/main` (reset-to-base; scratch restored
  from the `.lf/tmp/scratch-stash` snapshot). The directive text confirms:
  preserve the completed kickoff design and finish normally; do not redo
  research.
- 2026-07-18 (implementation turn): Re-acknowledged directive v3 with the
  absolute compatible lf. Checkpointed the WIP, then rebased onto `origin/main`
  via `lf rebase --manual origin/main` (clean direct rebase; PR #139 plus the
  two newer commits `522b2fb` INT-13 conditional_search and `f41180f` managym
  possible_worlds now in history). The INT-13 `ConditionalStrategyResult` /
  `ConditionResult` contracts landed on main, so the GAM-4/INT-13 adapter seams
  are now explicit and typed: `int13_condition_to_decision_evidence` and
  `int13_result_to_surface_deltas` in `etude/advice.py` (TYPE_CHECKING import
  preserves the runtime etude/manabot boundary; fixture-first at runtime), with
  the GAM-4 seam on `AdviceRequestIdentity` + `request_advice`. Four seam tests
  added. The kickoff design doc `scratch/prototype-belief-to-strategy-comparison.md`
  is preserved unchanged. Suite: 134 etude tests, 13 frontend advice unit tests,
  5 Playwright advice e2e (study-mode loop hardened with the play.spec retry
  pattern), `npm run check` and `npm run build` clean, ruff clean.

## Blockers (reported once, proceeding inline)

- 2026-07-17: `lf task acknowledge GAM-6 --directive 1` failed — the Loopflow
  registry on this machine was stale (`lf 0.11.3` vs a newer divergent local
  build; `lf doctor` reported migration `0.11.029_ci_incident_repaired_head`
  unknown) and the ambient `LF_WAVE_ID` pointed at a wave no longer in the
  registry. Resolved 2026-07-17: the compatible release binary
  (`LF_HOME=/Users/jack/.lf LF_BIN=/Users/jack/src/loopflow/target/release/lf
  /Users/jack/src/loopflow/target/release/lf`, `lf 0.11.3`, migrations
  applied/known both `0.11.029_ci_incident_repaired_head`) with
  `LF_WAVE_ID=game` recorded `GAM-6 incorporated directive v3`
  (`current_directive_version` 3, `incorporated_directive_version` advanced).
  No outstanding registry blocker. Re-confirmed 2026-07-18 (see Directive).

## Assumptions (executive decisions, correctable in review)

- The two belief scenarios are modeled as two `StudyLandmark` rows at the same
  `erd1` decision inside one `StudyArtifact`, each with real flat-MC evidence
  under a disjoint seed family. The existing `StudyArtifact` contract permits
  this (no duplicate-decision-id check). A thin `scenarios` metadata array
  carries the prototype's presentation layer (labels, descriptions, inferred
  ranges) without touching the Rust-owned schema.
- The "inferred starting range" is a concise, honest, pinned text
  characterization per scenario — not an interactive range editor (the
  directive forbids generic range tooling).
- The live play surface loads the fixture's pinned completed-match decision as
  the demonstration advice surface beside the ActionPanel. The live frame
  during play has no `erd1` address yet (the canonical replay finalizes at
  game close), so live-address advice is a future GAM-4 integration point via
  the `onRequestAdvice` seam.
- The fork/Retry/return UI is out of scope (GAM-4's substrate, not yet
  publishable). The Study-mode component is advisory + comparison only.
- The shared action vocabulary is the frame's legacy `offers` (the live UI and
  the existing fixture already use it); the structured offer surface at the
  forked decision exposes only `pass_priority` and is not the vocabulary here.
- PR #139 deliberately cleared stale scratch notes from prior merged PRs
  (`scratch/provide-exact-study-fork-and-2.md` and the pre-GAM-6
  `questions.md`); the rebase respects that cleanup. Those files survive only
  in the `.lf/tmp/scratch-stash` rebase snapshot.
- 2026-07-18: the current engine
  (`managym/_managym.cpython-312-darwin.so`, 2026-07-17) reproduces the pinned
  decision-6 structure (ordinal 6, revision 6, prompt 7, two offers:
  play_land + pass_priority) but its frame content drifted from the frozen
  `study-curated-decision.json` fixture (frame_hash `8ac9a4a7...` vs frozen
  `f714ccaf...`; canonical replay hash `e846d348...` vs frozen
  `692f3ae1...`). The advice fixture therefore pins to the CURRENT engine's
  canonical identity so its frame, erd1 address, and flat-MC evidence are all
  real and self-consistent. The frozen study fixture is left untouched (out of
  scope). The de-risked flat-MC numbers reproduce exactly on the current
  engine: scenario A policy `[0.75, 0.25]`, scenario B `[0.6875, 0.3125]`
  with value flipping toward Pass and favorable `[0/16, 5/16]` for B.

---

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

## Implement-time assumptions (INT-14, after INT-13 PR #140 landed)

INT-13 shipped on main as `522b2fb` (`manabot/sim/conditional_search.py` +
`experiments/data/int-13-conditional-strategy-fixture-v1.json`). The branch is
rebased onto it. Decisions below resolve the open questions and the design's
"INT-13 does not exist yet" framing now that it does.

- **Frozen shape contract = INT-13's `ConditionalStrategyResult` shape.** The
  digest-pinned shape is derived from `conditional_search.serialize_result`'s
  viewer-safe layer (`action_count`, `action_labels`, per-condition
  `condition_id`/`condition_mass`/`action_distribution`/`q_values`/`root_value`).
  A test asserts the real INT-13 fixture conforms to the pinned digest, proving
  "no contract change when INT-13's real result arrives."

- **K=5 for the toy, matching INT-13's condition count.** The toy producer emits
  5 conditions per decision (True, Has, Lacks, Q, Not(Q) ids) over cheap flat-MC
  self-play decisions. **Uniform-determinization toy:** every condition's
  `condition_scores` = the flat-MC per-action scores for that decision
  (identical across conditions), with uniform weights 1/5. The condition_index
  tag varies but carries no hidden-info → pre-registered ~0 strength gap holds
  by construction. This is the strongest honest "plumbing receipt": it cannot
  show strength because the label is definitionally uninformative.

- **Matched ablation (2x2 factorial).** All four arms share ONE conditioned
  Agent architecture (`AgentHypers.max_conditions > 0` → one extra neutral
  object row + a `condition_embedding`; policy/value heads unchanged). All arms
  train on the SAME D×K expanded rows with the SAME per-row policy targets
  (each row's `condition_scores`, identical across k for the toy). The ONLY
  difference across the conditioning dimension: the unconditioned arms
  (`policy_only`/`policy_value`) see a NEUTRAL condition input
  (`condition_index=0, condition_weight=1.0`) for every row; the conditioned
  arms (`belief_conditioned_*`) see the REAL per-row condition
  (`condition_index=k, condition_weight=1/5`). The value dimension isolates
  `value_weight` 0 vs 1. Same fixture/init/split/optimizer/seed; only the
  condition feature content + value_weight differ.

- **Inference uses the neutral condition.** The arena path
  (`_student_vs_random`/`BatchedSampler`/`ObservationSpace`) does not produce
  condition keys. The Agent defaults to `condition_index=0,
  condition_weight=1.0` (the True/uniform condition — the uninformative belief)
  when condition keys are absent. All arms evaluate under this neutral
  condition, so the conditioned arm trains with varied labels but plays under
  the uninformative prior — the train/test mismatch the ~0 prediction covers.

- **Condition channel config lives in `AgentHypers.max_conditions`** (default 0
  = no condition channel, fully backward compatible). `load_checkpoint_agent`
  rebuilds `AgentHypers(**hypers["agent_hypers"])`, so the field round-trips
  through checkpoints with no checkpoint-format change.

- **`train_search_supervised` algorithm is unchanged; only `_batch_observations`
  is extended** to also batch `condition_index`/`condition_weight` when present
  in the dataset (backward compatible — existing datasets lack these keys).
  This honors the design's "reuse unchanged" intent (same loss, same optimizer,
  same split) while letting the condition side input reach the Agent. Deviation
  from the literal "unchanged" wording is recorded here.

- **Per-row `scores` = `condition_scores[d, k, :]`** (win-prob-style in [0,1],
  -1 padding on invalid), so `policy_target_kind = score_softmax` reuses
  `soft_targets_from_scores` verbatim. `value_target_kind = terminal_outcome`
  (winner==seat), unchanged. No belief head; the value head stays scalar.

- **`condition_root_value` omitted** in this slice (optional key); the toy
  yields no cheap per-condition value distinct from the terminal outcome.

- **Frozen evidence:** the toy conditional shard + schema_version 2 snapshot
  manifest are generated and frozen into `experiments/data/` (not `.runs/`,
  which is gitignored) as checked-in evidence, mirroring the INT-13 fixture.

## Pre-existing failure noted (not caused by INT-14)

- `tests/sim/test_conditional_search.py::test_checked_fixture_is_deterministic_and_identity_pinned`
  fails after rebasing onto RUL-8 (#141): RUL-8 added `managym/src/possible_worlds.rs`
  to the engine source bundle, changing `identities.engine_source_sha256` versus
  the INT-13 fixture's pinned value. This is a RUL-8/INT-13 fixture-staleness
  issue, not an INT-14 regression. All 21 `test_conditional_snapshot.py` tests
  pass; the ablation smoke completes.
