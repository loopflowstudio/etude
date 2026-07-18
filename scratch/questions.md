# INT-12 assumptions

- `ReplayDecisionAddress` is the canonical DecisionAddress for advisor v1.
  Because canonical replay intentionally assigns no address to an unplayed
  prompt, “live” in this slice means a committed decision retained by the
  still-live Game session. Advising before commitment requires a future
  Game-owned address type and is out of scope.
- The first checked success fixture uses a code-only determinized-search
  advisor and a canonical `BeliefDistributionResolver` for player-authored
  metadata. The exact frozen INT-9 learned likelihood checkpoint bytes are not
  retained, so the model-inferred/checkpoint path must return typed artifact
  unavailability and cannot be demonstrated as a successful learned advisor.
