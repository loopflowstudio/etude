# Assumptions and review notes

- 2026-07-17: Headless review assumption: "selected matchup" means the
  symmetric world-w2 `INTERACTIVE_DECK` already pinned by the Teacher-1
  contract, not UR Lessons versus GW Allies. This keeps the exact opening
  support at the documented 10,832 hand-count vectors and aligns with the
  current search evidence.
- 2026-07-17: The production protocol-v1 `Command` envelope used by Etude and
  Teacher-1 is the required command path. A small executor will validate the
  envelope and then call normal authoritative `Env.step`; INT-9 will not expand
  the separate experimental atomic structured-offer prototype, whose current
  coverage is intentionally partial.
- 2026-07-17: Exactness is at the card-definition multiset level with exact
  combinatorial physical-copy mass. Physical copies of one definition are
  exchangeable in this selected content boundary; positional duplicate action
  probabilities and split prompt paths are summed into one public semantic
  action. Physical-copy invariance is an executable gate, not an assumption
  hidden from the receipt.
- 2026-07-17: A missing frozen likelihood or arena opponent artifact is a
  contract failure, never permission to substitute a convenient checkpoint.
  Implementation should prefer the byte-identical world-w2 `policy_value`
  checkpoint being resolved for the Teacher-1 control lock, but must not name
  it in the INT-9 contract until its path and SHA-256 are independently valid.
- 2026-07-17: The uniform control is the current-snapshot combinatorial
  baseline. It intentionally forgets earlier public history, while still
  running the full posterior computation for matched end-to-end cost; only the
  belief arm claims complete-history compatibility.
- 2026-07-17: Raw positional opponent commands are not viewer history.
  Targeted casts and combat declarations span private intermediate prompts, so
  the trusted adapter groups them and releases only the final public action.
  Viewer-equivalent-root tests must prove that grouping does not leak source,
  target, or declaration information early.
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
- 2026-07-17: Fixed-viewer replay must omit unchanged raw steps. A targeted
  cast and combat declaration can span several authoritative prompts before a
  public commitment; even logging an otherwise empty row per prompt would
  reveal private prompt count. The tracker now records only a public semantic
  action or a viewer-visible hidden-pool transition, and the known-truth hand
  is read solely by the separate evaluation audit after such a transition.
- 2026-07-17: Matched compute is exact at the configured root budget, not at
  aggregate playout count. Belief and uniform use the same worlds per action,
  rollouts per world, native entry point, and end-to-end likelihood work, but
  divergent play changes decision counts and legal branching. The frozen
  contract now reports raw totals without requiring them to be equal.
