# Confirmed interpretation and assumptions

## INT-13 (this PR)

- **RUL-8 not yet publishable**: the `PossibleWorldSpace` and `WorldQuery`
  seam is defined as Python Protocols in `manabot/sim/conditional_search.py`.
  A local fixture adapter (`ScenarioWorldSpace`) implements them via the
  existing engine scenario API. When RUL-8 lands, its real implementation
  replaces the adapter; the Protocols stay unchanged.
- **No Rust changes**: the engine does not expose opponent hand cards to
  Python (deliberate viewer-safety). The fixture adapter avoids needing hand
  inspection by evaluating `WorldQuery.matches` against `WorldSpec` (the
  world definition), not live engine state. World configuration uses
  `scenario_clear_hand` + `scenario_force_card_in_hand` +
  `scenario_refresh` on forked legacy-backend clones.
- **Legacy clone backend** (`REFERENCE_BRANCH_DRIVER_ID`) for the fixture:
  the selected backend's `fork_exact` returns a guarded env
  (`selected_guard: true`). Scenario methods don't check the guard, but
  using them on a guarded env is outside the intended API. The legacy
  backend's `clone_env()` returns an unguarded env with full method access.
- **Five conditions**: True (baseline), Has(card), Lacks(=Not(Has)),
  Q(general), Not(Q). For the fixture: Has=HasCard("Counterspell"),
  Q=HasCard("Lightning Bolt"). Four worlds form a 2×2 factorial.
- **Planner name**: `"determinized_puct"`. No ISMCTS, equilibrium, or
  belief-inference claim.
- **Two-layer fixture**: authority-private (for INT-12) includes branch
  receipts and per-world arrays; viewer-safe (for GAM-6) includes only
  action distributions, Q-values, uncertainty, and comparison deltas.
  Condition IDs (e.g. `has:Counterspell`) are query labels, not hidden
  truth.
- **Wall-clock excluded** from canonical serialization (non-deterministic).
