# INT-7 Kickoff Assumptions

## Resolved assumptions

- The exact INT-8-retained INT-4 bundle is the sole authoritative input. A
  digest named by an absent artifact, a regenerated corpus, or a substituted
  checkpoint is not acceptable.
- “Matched compute” means the same 32 PUCT traversals, four worlds, one CPU
  checkpoint forward per expanded nonterminal node, and paired search seeds.
  The policy-only arm returns neutral value `0.5` after that forward; it does
  not use random terminal playouts or an arbitrary untrained value output.
- “Blended” means the preregistered probability target
  `0.5 * terminal_outcome + 0.5 * teacher_root_value`. The coefficient is not
  selected from calibration results.
- Three model seeds (`197`, `198`, `199`) are the smallest useful seed block.
  They measure initialization sensitivity only; the retained corpus still has
  one teacher/data seed.
- The fixed split uses split seed `197` for every model seed so row identity and
  volume stay matched across arms.
- The policy target is always the normalized retained visit distribution.
- The strongest point-estimate complete player is selected by arena rating and
  paired gameplay, never by Brier score. A separate threshold decides whether
  the smoke evidence is separated enough to continue.
- Competency noninferiority is computed once per value-target method by summing
  its correct-line outcomes across three model checkpoints, five scenarios,
  and two frozen competency seeds (30 binary rows), then subtracting the
  corresponding 30-row `visit_policy_only` aggregate. A nonnegative difference
  passes, including a tie. Per-model-seed and per-scenario deltas are retained
  as diagnostics and never become additional gates.
- The INT-6 smoke schedule is the evaluation authority. The INT-6 contract
  remains byte-for-byte unchanged; new player/evaluator identities belong to
  an additive INT-7 contract.
- No files under `managym/`, INT-9 belief/world authority, or Study are in
  scope.
- Training uses ten fixed epochs rather than reproducing INT-4's single epoch.
  This avoids an undertraining confound while the fixed held-out game exposes
  overfit risk without result-dependent stopping or tuning.
- Evaluation uses the complete 17-player, 136-cell, 544-game matrix rather than
  candidate-versus-anchor-only matches. This makes “full matchups” literal and
  keeps the diagnostic rating connected without opponent selection.
- The `continue_<arm>` paired-score separation threshold is preregistered at
  `0.05`. The point-estimate winner is named regardless; the threshold affects
  only whether follow-up scaling is recommended.

All kickoff design questions are resolved. Implementation and training remain
blocked until the review-design gate approves these assumptions.
