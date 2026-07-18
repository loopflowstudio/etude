# Assumptions and review notes

- 2026-07-18, coordination resolved: rebasing onto main `5416dd6` preserved
  RUL-5's exact `ObjectRef` preconditions and atomic stale-object/stale-revision
  rejection while adding INT-9's provider-owned `PublicCommitment`. The
  combined semantic wire contract is version 3. Debug Rust, focused belief,
  semantic-boundary, environment, and flat-MC checks pass against the rebuilt
  extension.

- 2026-07-18, coordination: RUL-5 concurrently owns exact `ObjectRef`
  incarnation binding plus atomic stale-object and stale-revision rejection
  across `managym/decision.py`, `managym/src/agent/env.rs`,
  `managym/src/agent/structured_offer.rs`, `managym/src/decision.rs`, and
  `managym/src/python/bindings.rs`. INT-9 may develop its canonical world
  space/materializer and viewer-visible `PublicCommitment` receipt locally but
  must not publish before RUL-5 lands; then integrate current main through
  `lf`, explicitly preserve both authority contracts across all five seams,
  and rerun focused debug Rust plus belief tests before publication. The
  combined seam will retain RUL-5's native semantic step/reward surface and
  object preconditions, add `PublicCommitment` to its frame/receipt, and bump
  the semantic schema because the combined wire projection differs from the
  RUL-5 schema.

- 2026-07-17, directive v3: The accepted architecture supersedes the earlier
  proposed `ExactHandRange`/`HandKey`, Python `PublicAction`, hidden-pool
  snapshot, and direct exact-hand determinization contracts. INT-9 will express
  probability as a bounded manabot `BeliefState` whose rows are the canonical
  worlds of one managym `PossibleWorldSpace`; it will consume managym
  `WorldQuery`, viewer Observation/history, materialization, semantic
  `DecisionFrame`, and `Command` authority rather than mirroring them.
- 2026-07-17, directive v3: The first executable comparison remains the
  symmetric world-w2 matchup and matched flat search. Missing frozen likelihood
  and arena checkpoints are an explicit evidence wait. Fixture likelihoods may
  verify mechanics but cannot satisfy smoke or arena gates, and kickoff will
  not create another PR or activate unrelated backlog to obtain them.
- 2026-07-17, directive v3: Kickoff and gate use deferred interaction. The
  owning Belief-Aware Play Project Session is the reviewer and decision owner;
  the absent user is not assigned a review wait.

- 2026-07-17: Headless review assumption: "selected matchup" means the
  symmetric world-w2 `INTERACTIVE_DECK` already pinned by the Teacher-1
  contract, not UR Lessons versus GW Allies. This keeps the exact opening
  support at the documented 10,832 hand-count vectors and aligns with the
  current search evidence.
- 2026-07-17, directive v3 supersession: INT-9 selects a semantic `Command`
  from the current canonical managym `DecisionFrame` and submits it through
  normal managym authority. It defines no Etude protocol-v1 envelope executor,
  `Env.step` adapter, or parallel structured-offer command path.
- 2026-07-17: Exactness is at the card-definition multiset level with exact
  combinatorial physical-copy mass. Physical copies of one definition are
  exchangeable in this selected content boundary. Likelihood mass may be
  summed only across authoritative offers that managym identifies as the same
  viewer-visible semantic commitment; INT-9 does not group positional actions
  or prompt paths. Physical-copy invariance is an executable gate, not an
  assumption hidden from the receipt.
- 2026-07-17: A missing frozen likelihood or arena opponent artifact is a
  contract failure, never permission to substitute a convenient checkpoint.
  Implementation should prefer the byte-identical world-w2 `policy_value`
  checkpoint being resolved for the Teacher-1 control lock, but must not name
  it in the INT-9 contract until its path and SHA-256 are independently valid.
- 2026-07-17: The uniform control is the current-snapshot combinatorial
  baseline. It intentionally forgets earlier public history, while still
  running the full posterior computation for matched end-to-end cost; only the
  belief arm claims complete-history compatibility.
- 2026-07-17, directive v3 supersession: The tracker consumes only canonical
  viewer-visible Observation/history and transition identities. It never
  observes, groups, or reconstructs raw positional commands or private prompt
  paths. If managym cannot provide a complete viewer-visible semantic identity
  for a selected commitment, the run fails closed with a typed Rules provider
  gap. Viewer-equivalent-root tests prove those canonical inputs do not leak
  private source, target, declaration, or prompt information.
- 2026-07-17: Range support is never silently pruned. If selected-matchup hand
  growth exceeds the contract memory cap, that is a measured task failure and
  the exact representation must be redesigned before claiming complete play.
- 2026-07-17: Smoke is not Task completion. The expensive arena stage, its
  seed blocks, and its positive/ambiguous decision thresholds must be frozen
  before results and executed (or fail explicitly under the frozen caps).
- 2026-07-17: A filesystem search across `/Users/jack/src` found no `.pt`,
  `.pth`, or `.ckpt` artifact outside virtual environments and build outputs.
  The checked-in INT-9 contract therefore records the likelihood, policy-only,
  and frozen-learned checkpoints as unresolved required artifacts and the
  runner fails closed before play. The substrate is runnable in unit tests with
  an explicitly test-only likelihood, but that model cannot produce smoke or
  arena evidence.
- 2026-07-17: Fixed-viewer replay records only canonical viewer-visible
  Observation/history and transition increments. The tracker neither observes
  nor filters raw steps or private prompt counts. The known-truth hand is read
  solely by the separate evaluation audit after a canonical visible
  transition.
- 2026-07-17: Matched compute is exact at the configured root budget, not at
  aggregate playout count. Belief and uniform use the same worlds per action,
  rollouts per world, native entry point, and end-to-end likelihood work, but
  divergent play changes decision counts and legal branching. The frozen
  contract now reports raw totals without requiring them to be equal.
