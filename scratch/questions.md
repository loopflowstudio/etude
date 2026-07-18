# Assumptions, accepted decisions, and open questions

## Accepted in the interactive review

- managym is the authoritative rules, match execution, semantic Command,
  Observation, replay, fork, and possible-world engine Etude Fantasia needs.
  Etude adapts it for the product; manabot plans and learns over it.
- Observation is a central viewer-bound protocol object composed from current
  visible state, ordered visible events, and the current complete semantic
  decision. managym preserves lossless viewer history; agent memory belongs to
  manabot.
- managym defines the viewer-relative possible-world domain and typed
  `WorldQuery` grammar. manabot owns beliefs and conditioning over that domain.
  Etude exposes questions such as `Has(Bolt)` without revealing actual truth.
- The student gains a belief head. Policy and value are conditioned on its
  `BeliefState`, and the primary product/search result compares full action
  distributions across belief conditions.
- The first supervised conditional teacher uses the canonical compatible-deal
  prior restricted by a versioned query curriculum. `Has(Bolt)` is uniform over
  compatible physical deals with at least one Bolt, which induces
  combinatorial rather than uniform weights over collapsed count vectors.
- Actual hidden truth supervises belief calibration. It is not fed to policy as
  a clairvoyant training belief. Counterfactual teacher queries need not hold in
  the recorded actual world.

## Current and in-flight evidence

- Current main's `StudyForkProvider` retains an authority-private managym root,
  exact-forks siblings, executes normal structured Commands, and returns a
  consuming `StudyReturnReceipt` bound to `Env.state_digest()`. Root drift
  fails closed. That digest is the Study adapter's source-return witness, not a
  replacement for the search BranchDriver witness, and the persisted replay
  schema remains unchanged.
- INT-6 arena work and INT-9 exact-range work were inspected in their active
  sibling Task worktrees. Their contracts are in flight, not present on current
  main. INT-9 should converge on the shared managym world domain and manabot
  belief contract; INT-6 remains the future promotion authority.
- Missing exact frozen checkpoint bytes remain a hard blocker for production
  gameplay/evidence claims. Frozen evidence is never repaired by substituting
  another checkpoint or replay.
- The frozen INT-4 smoke contract does not match the current engine source or
  rebuilt extension digest. That is successful fail-closed behavior; historical
  inputs remain immutable.

## Open but non-blocking for first integration

- Whether the first `BeliefHead` emits a fixed categorical distribution over a
  frozen exact support or scores structured candidate hypotheses and normalizes
  over the current support.
- Whether viewer history reaches policy/value only through `BeliefState` or
  also through a separate agent-memory path.
- Exact mass/coverage thresholds and selector balancing for
  `QuerySamplerSpec`.
- Which information-set-consistent planner family follows determinized PUCT.

## Integration disposition

- The reviewed map has been distilled into `docs/ARCHITECTURE.md` and linked
  from the repository, package, docs, and Rules/Intelligence roadmap entry
  points. This scratch file remains the detailed review record.
