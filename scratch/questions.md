# INT-7 Kickoff Assumptions and Review Questions

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
- The INT-6 smoke schedule is the evaluation authority. The INT-6 contract
  remains byte-for-byte unchanged; new player/evaluator identities belong to
  an additive INT-7 contract.
- No files under `managym/`, INT-9 belief/world authority, or Study are in
  scope.

## Items for review-design

- Confirm that ten fixed epochs are preferable to reproducing INT-4's single
  epoch. Ten epochs avoids an undertraining confound but increases overfit risk
  on three training games; the held-out game and fixed schedule expose rather
  than tune around that risk.
- Confirm the complete 17-player matrix (544 games) rather than a smaller
  candidate-versus-anchor-only matrix. The full matrix costs more but makes
  “full matchups” literal and keeps the diagnostic rating connected without
  opponent selection.
- Confirm the `0.05` paired-score separation threshold for `continue_<arm>`.
  The point-estimate winner is named regardless, so changing this threshold
  affects only whether follow-up scaling is recommended.

No item blocks kickoff. Until review changes it, the implementation should use
the decisions above and must not run training before the review-design gate
approves them.
