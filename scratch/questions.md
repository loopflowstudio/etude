# W2-200 assumptions

- The repository has no second optimized rules engine, and directive 1 excludes
  W2-197 plus branching drivers. W2-200 therefore treats `Game` with explicit
  singleton prompts (`skip_trivial = false`) as the readable reference reducer
  and production trivial-step collapse (`skip_trivial = true`) as the optimized
  executor. They share low-level rule/card primitives; the evidence must state
  that limitation and must not claim an independent Comprehensive Rules model.
- “Curated two-deck boundary” resolves to
  `content/semantic/v1/two_deck.source.json`, not duplicated prose decklists.
  That source currently contains 41 UR cards and 40 GW cards despite the older
  “two 40-card decks” wording.
- The Phase deliverable is an offline, machine-checked source-overlap matrix at
  commit `553b97bd5c9f1a28bf7a6ebe80f6cb3a0e296c0d`, not a Phase build in blocking
  CI. Live Phase execution is a declared practical exclusion, while local
  replay/property/fuzz evidence remains executable.
