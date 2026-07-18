# Run Determinized PUCT Over Conditional World Priors

## Problem

The Belief-Aware Play project (Linear 8ec95ad5) needs to answer a canonical
question: *given that the opponent might or might not hold a specific card,
how should the hero's strategy change?* Today the determinized PUCT teacher
(`manabot/sim/mcts.py`) samples worlds **uniformly** and builds a separate
tree per world, then aggregates visits and values into one `PuctResult`. It
cannot separate "what if the opponent has Counterspell?" from "what if they
don't?" — both hypotheses are mixed into the same aggregate.

INT-13 is integration step 3 from the INT-10 search-learning architecture:
extend determinized PUCT to accept an immutable `ConditionalWorldPrior` and
paired query plan, then return one **aligned** `ConditionalStrategyResult`
for five conditions — True, Has, Lacks, Q, Not(Q) — at the same semantic
root. The result must be consumable by GAM-6 (the Etude Study advisor) and by
INT-12 (the lower-level contract later exposed by Intelligence).

RUL-8 (the Rules wave task that owns the viewer-relative `PossibleWorldSpace`
and `WorldQuery` seam) is active but not yet publishable. This PR depends on
its narrow public shape via a local fixture/adapter rather than duplicating
managym world-space ownership.

## The demo

```bash
uv run python scripts/generate_conditional_strategy_fixture.py
uv run pytest tests/sim/test_conditional_search.py -q
```

The generator constructs a mid-game Priority decision where the hero holds a
creature and the opponent's hand is unknown. It defines four exact worlds
(opponent holds Counterspell+Bolt, Counterspell only, Bolt only, or neither),
runs conditional determinized PUCT for all five conditions at matched budget,
and writes a checked identity-pinned JSON fixture. The test regenerates it,
asserts byte-identical output, and verifies that the Has(Counterspell)
condition shifts Q-values and visit mass away from the spell-cast action
relative to Lacks(Counterspell), with aligned action identities and
zero hidden-truth leakage in the viewer-safe layer.

## Approach

### Architecture overview

```
 ┌─────────────────────────────────────────────────────────┐
 │  RUL-8 narrow public shape (Protocols in manabot)       │
 │  WorldQuery · WorldSpec · PossibleWorldSpace            │
 │  (RUL-8 will provide the real implementation; this PR   │
 │   defines the interface + a local fixture adapter)      │
 └───────────────┬─────────────────────────────────────────┘
                 │ consumed by
 ┌───────────────▼─────────────────────────────────────────┐
 │  ConditionalWorldPrior (immutable)                      │
 │  world_space + normalized weights + viewer + identity   │
 └───────────────┬─────────────────────────────────────────┘
                 │ filtered by
 ┌───────────────▼─────────────────────────────────────────┐
 │  ConditionalQueryPlan                                   │
 │  Has(card) · Q(general)  →  True, Has, Lacks, Q, Not(Q)│
 └───────────────┬─────────────────────────────────────────┘
                 │ searched by
 ┌───────────────▼─────────────────────────────────────────┐
 │  conditional_determinized_puct(root_env, prior, plan,  │
 │    simulations, worlds, seed, c_puct, ...)              │
 │  For each condition:                                    │
 │    1. Filter worlds by condition, renormalize weights   │
 │    2. Sample N worlds from the conditioned subset       │
 │    3. Fork root → configure world (RUL-8 seam)          │
 │    4. Run PUCT (reuses _Node/_select_action/_simulate)  │
 │    5. Aggregate visits/Q/value, weighted by mass        │
 │  Return aligned ConditionalStrategyResult               │
 └───────────────┬─────────────────────────────────────────┘
                 │ serialized to
 ┌───────────────▼─────────────────────────────────────────┐
 │  Checked fixture (experiments/data/...)                 │
 │  Authority-private layer (for INT-12):                  │
 │    full per-condition results, identities, receipts     │
 │  Viewer-safe layer (for GAM-6):                         │
 │    action distributions, Q, uncertainty, deltas —       │
 │    no opponent hands, no world labels, no hidden truth  │
 └─────────────────────────────────────────────────────────┘
```

### RUL-8 narrow public shape

Three Protocols in `manabot/sim/conditional_search.py`. When RUL-8 lands,
its real `PossibleWorldSpace` implementation replaces the local fixture
adapter; the Protocols stay unchanged.

```python
class WorldQuery(Protocol):
    """Predicate over a possible world's hidden-state definition."""
    query_id: str
    def matches(self, world: WorldSpec) -> bool: ...

@dataclass(frozen=True)
class WorldSpec:
    """One exact compatible world."""
    world_index: int
    label: str               # stable human-readable id, audit-private
    weight: float            # prior probability mass (pre-condition)
    # Hidden-state metadata (audit-private, never in viewer-safe layer):
    opponent_hand: tuple[str, ...]

class PossibleWorldSpace(Protocol):
    """Viewer-relative set of exact compatible worlds (RUL-8 seam)."""
    space_id: str
    viewer: int
    worlds: tuple[WorldSpec, ...]

    def configure(self, env: Any, *, world_index: int) -> None:
        """Configure a forked env to the hidden state of world_index."""
        ...
```

`WorldSpec.opponent_hand` is audit-private. The `ConditionalStrategyResult`
never exposes it in the viewer-safe layer. The `WorldQuery.matches` method
evaluates against `WorldSpec` (the world definition), not against live engine
state — this avoids needing opponent-hand inspection from Python (which the
engine deliberately does not expose).

### Local fixture adapter: `ScenarioWorldSpace`

Implements `PossibleWorldSpace` using the existing engine scenario API
(`scenario_clear_hand` + `scenario_force_card_in_hand` +
`scenario_refresh`). This is the stand-in for RUL-8.

```python
@dataclass(frozen=True)
class ScenarioWorldSpace:
    space_id: str
    viewer: int
    worlds: tuple[WorldSpec, ...]
    opponent_seat: int

    def configure(self, env: Any, *, world_index: int) -> None:
        world = self.worlds[world_index]
        env.scenario_clear_hand(self.opponent_seat)
        for card_name in world.opponent_hand:
            env.scenario_force_card_in_hand(self.opponent_seat, card_name)
        env.scenario_refresh()
```

The adapter uses the **legacy clone backend** (`REFERENCE_BRANCH_DRIVER_ID`)
because:
1. `SelectedBranchRuntime.fork_exact` returns a guarded env
   (`selected_guard: true`). The scenario methods do not check the guard, but
   using them on a guarded env is outside the intended API.
2. The legacy backend's `fork_exact` calls `source.clone_env()`, returning an
   unguarded `PyEnv` with full method access.
3. The legacy backend is an explicit named oracle, never a fallback — it is
   the correct backend for a fixture/adapter that needs unrestricted engine
   access.

When RUL-8 lands, the production path switches to the selected backend with
RUL-8's world-configuration mechanism (which will handle the guard correctly
from the Rust side).

### ConditionalWorldPrior

```python
@dataclass(frozen=True)
class ConditionalWorldPrior:
    world_space: PossibleWorldSpace
    weights: np.ndarray          # normalized, shape (len(worlds),)
    viewer: int
    prior_sha256: str            # hash of world definitions + weights
```

Immutable. The weights represent the belief-update result (likelihood-
weighted determinization). For the fixture, weights are uniform. When the
belief tracker is built, the weights reflect public-history updates.

### ConditionalQueryPlan

```python
@dataclass(frozen=True)
class ConditionalQueryPlan:
    has: WorldQuery              # the "Has" condition (e.g., HasCard("Counterspell"))
    q: WorldQuery                # the general "Q" condition (e.g., HasCard("Lightning Bolt"))
    plan_sha256: str

    @property
    def conditions(self) -> tuple[tuple[str, WorldQuery], ...]:
        return (
            ("true", TrueQuery()),
            (self.has.query_id, self.has),
            (f"not({self.has.query_id})", NotQuery(self.has)),
            (self.q.query_id, self.q),
            (f"not({self.q.query_id})", NotQuery(self.q)),
        )
```

Five conditions: True (baseline), Has, Lacks (= Not(Has)), Q, Not(Q).

### ConditionalStrategyResult

```python
@dataclass(frozen=True)
class ConditionResult:
    condition_id: str
    condition_mass: float        # total weight of satisfying worlds
    support: int                 # number of satisfying worlds
    sampled_worlds: int          # worlds actually searched
    visit_counts: np.ndarray     # (action_count,)
    q_values: np.ndarray         # (action_count,)
    root_value: float
    world_q_values: np.ndarray   # (sampled_worlds, action_count)
    world_root_values: np.ndarray  # (sampled_worlds,)
    uncertainty: float           # between-world Q standard error
    simulations: int
    cap_hits: int
    tree_nodes: int
    max_depth: int
    branch_driver_id: str
    branch_receipt: Mapping[str, Any]

@dataclass(frozen=True)
class ConditionalStrategyResult:
    conditions: tuple[ConditionResult, ...]  # 5, in plan order
    action_count: int
    action_labels: tuple[str, ...]           # aligned across all conditions
    root_state_digest: str
    planner: str                              # "determinized_puct"
    search_params: Mapping[str, Any]          # simulations, worlds, c_puct, seed, max_steps
    prior_sha256: str
    plan_sha256: str
    identities: Mapping[str, Any]             # world tag, engine source hash, content digest, etc.
    realized_compute: Mapping[str, Any]       # total simulations, wall-clock, total tree_nodes
    comparison_deltas: Mapping[str, Mapping[str, float]]  # condition_id → {field: delta_vs_true}
```

### The search function

```python
def conditional_determinized_puct(
    root_env: Any,
    *,
    prior: ConditionalWorldPrior,
    query_plan: ConditionalQueryPlan,
    simulations: int,
    worlds: int,
    seed: int,
    c_puct: float = 1.5,
    max_steps: int = DEFAULT_MAX_PLAYOUT_STEPS,
    evaluator: LeafEvaluator | None = None,
    root_observation: Any | None = None,
    branch_driver_id: str = REFERENCE_BRANCH_DRIVER_ID,
    branch_backend: SearchBranchBackend | None = None,
    branch_audit: bool = True,
    branch_match_id: str | None = None,
) -> ConditionalStrategyResult:
```

For each of the five conditions:
1. Filter `prior.world_space.worlds` by the condition's `WorldQuery.matches`
2. Renormalize weights over the satisfying subset
3. **Fail closed** if support is empty (condition mass = 0)
4. Sample `worlds` worlds from the subset (deterministic, seed-derived)
5. For each sampled world:
   a. `fork = branch_session.fork_exact(root_env, "world")`
   b. `prior.world_space.configure(fork, world_index=world_index)`
   c. Assert `fork.action_count() == root_action_count` (alignment)
   d. Build `_Node.from_env(fork, ...)` and run `per_world` PUCT simulations
   e. Reuse `_Node`, `_select_action`, `_simulate` from `mcts.py` unchanged
6. Aggregate: weighted visit counts, Q-values, root value, world-level arrays
7. Compute uncertainty (between-world Q standard error)
8. Compute comparison deltas vs the True condition

**Held fixed across all conditions**: planner (`determinized_puct`),
evaluator, budget (`simulations`, `worlds`, `c_puct`, `max_steps`), and
paired seeds (derived from the same base `seed` via `_mix_seed`).

**Named**: the result's `planner` field is `"determinized_puct"`. No ISMCTS,
equilibrium, or belief-inference claim is made.

### The fixture

**File**: `experiments/data/int-13-conditional-strategy-fixture-v1.json`
**Generator**: `scripts/generate_conditional_strategy_fixture.py`

The generator:
1. Constructs a mid-game Priority position via scenario API (hero has
   creatures + a spell; opponent has lands + unknown hand)
2. Defines 4 exact worlds (2×2 factorial: Has/Not Counterspell × Has/Not
   Lightning Bolt), uniform weights
3. Defines the query plan: Has = HasCard("Counterspell"),
   Q = HasCard("Lightning Bolt")
4. Runs `conditional_determinized_puct` with small budget (simulations=16,
   worlds=4, seed=197)
5. Serializes the `ConditionalStrategyResult` to JSON with:
   - Authority-private layer: full per-condition results, identities,
     branch receipts, world indices (audit-private)
   - Viewer-safe layer: action distributions, Q-values, uncertainty,
     comparison deltas — **no opponent hands, no world labels, no
     hidden truth**
6. Runs the generator twice, asserts byte-identical output (determinism)
7. Writes the checked fixture JSON

**Identities pinned in the fixture**:
- World tag (`w2`)
- Engine source SHA-256 (from `teacher1_evidence.source_bundle_sha256`)
- Content digest (from `env.content_pack_manifest()`)
- Root state digest (from `env.state_digest()`)
- Root action count and action labels
- `prior_sha256` and `plan_sha256`
- Branch driver ID
- Search parameters

### Test structure

`tests/sim/test_conditional_search.py`:

1. **`test_checked_fixture_is_deterministic_and_identity_pinned`** —
   regenerate, assert byte-identical to checked file, assert all identity
   fields match
2. **`test_condition_results_are_aligned`** — all 5 conditions have the same
   `action_count` and `action_labels`
3. **`test_condition_mass_and_support`** — mass and support match the prior
   and query definitions (True: 4/1.0, Has: 2/0.5, Lacks: 2/0.5, Q: 2/0.5,
   Not(Q): 2/0.5)
4. **`test_comparison_deltas`** — deltas vs True are correct; Has(CS) shifts
   Q away from the spell-cast action
5. **`test_fail_closed_on_empty_support`** — a condition no world satisfies
   raises `ConditionalSearchError`
6. **`test_fail_closed_on_identity_mismatch`** — tampered fixture identity
   raises `ConditionalSearchError`
7. **`test_fail_closed_on_action_misalignment`** — if world configuration
   changes the root action count, the search raises
8. **`test_world_query_partition`** — TrueQuery/HasCard/NotQuery correctly
   partition the 4 worlds
9. **`test_viewer_safe_layer_has_no_hidden_truth`** — the viewer-safe
   projection contains no opponent hand cards or world labels
10. **`test_planner_named_determinized_puct`** — `result.planner ==
    "determinized_puct"`, no ISMCTS/equilibrium claim in the fixture
11. **`test_search_does_not_mutate_root`** — root state digest unchanged
    after all 5 conditions

## De-risking

| Question | Finding | Impact on design |
|----------|---------|-----------------|
| Can I inspect the opponent's hand after determinization from Python? | **No.** No PyO3 method exposes opponent hand cards. `Observation::populate_cards` walks `ZoneType::Hand` only for the agent player; opponent hand is never in any observation, tensor, or JSON. `state_digest` and `search_witness_json` return only hashes. This is deliberate viewer-safety. | `WorldQuery.matches` evaluates against `WorldSpec` (the world definition), not live engine state. The fixture adapter knows hand contents by construction (it sets them via scenario API). No engine hand-inspection method is needed. |
| Can the scenario API configure a forked mid-game env? | **Yes.** `scenario_game_mut` only checks game exists and is not over — works on any non-terminal env. Scenario methods (`clear_hand`, `force_card_in_hand`, `refresh`) do not check `selected_guard`. But `scenario_refresh_priority` only works at Priority decision points. | Use the legacy clone backend (unguarded forks). Construct the fixture at a Priority decision. Assert action count preserved after configuration. |
| Does `scenario_clear_hand` + `scenario_force_card_in_hand` round-trip correctly? | **Yes.** `scenario_clear_hand` moves hand cards to the bottom of the library. `scenario_force_card_in_hand` finds cards by name in the library (or graveyard) and moves them to the hand. After clearing, all original hand cards are in the library, so forcing specific cards back works. | This is the world-configuration mechanism: clear → force → refresh. The remaining library order is irrelevant for PUCT (rollouts reseed anyway). |
| Does changing the opponent's hand change the root action space? | **No** at Priority decisions. The hero's available actions (cast from hand, play land, pass) depend on the hero's hand, mana, and board — not the opponent's hand. `scenario_refresh_priority` recomputes from the hero's state. | Assert `fork.action_count() == root_action_count` after configuration. Fail closed if it changes. |
| Can I reuse the existing PUCT machinery? | **Yes.** `_Node`, `_select_action`, `_simulate` from `mcts.py` use `branch_session` for child forks and action application. With the legacy backend, all of these work unchanged. Only the root-world configuration step is new. | Import and reuse `_Node`, `_select_action`, `_simulate`, `_mix_seed`, `_uniform_priors`, `LeafEvaluator`, `UniformRandomLeafEvaluator` from `mcts.py`. |
| Can I use the SelectedBranchRuntime for the fixture? | The forked env has `selected_guard: true`. Scenario methods don't check the guard, but using them on a guarded env is outside the intended API. `determinize` is guard-blocked, but I'm not using `determinize` — I'm using scenario API. | Use the legacy backend for the fixture. When RUL-8 lands, the production path uses the selected backend with RUL-8's Rust-side configuration (which handles the guard correctly). |
| What if `scenario_force_card_in_hand` fails (card not in library/graveyard)? | This happens if the card is on the battlefield, in exile, or already in the hand. After `scenario_clear_hand`, all hand cards are in the library, so the card must be on the battlefield or in exile to fail. | Construct the fixture position so that the cards to force (Counterspell, Lightning Bolt, etc.) are in the opponent's library, not on the battlefield. Assert no failure. |
| Is the fixture deterministic? | The PUCT search is seeded and deterministic (same seed → same visit counts, Q-values, tree structure). The scenario API is deterministic (same state → same configuration). The fixture generation is deterministic. | Assert byte-identical output on two runs, following the `test_checked_*` pattern from `tests/semantic/` and `tests/etude/`. |
| Does the fixture need to be viewer-safe? | **Yes** for the GAM-6 consumable layer. The authority-private layer (for INT-12) includes world indices and receipts. The viewer-safe layer includes only action distributions, Q-values, uncertainty, and deltas — no opponent hands or world labels. | Two-layer serialization: `authority_private` and `viewer_safe`. The test asserts no hidden truth in `viewer_safe`. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Add a Rust `opponent_hand_names()` PyO3 method | Would allow runtime hand inspection after `determinize`, enabling WorldQuery to evaluate against live engine state. | Duplicates RUL-8's world-space ownership. The directive says "depend on its narrow public shape via a local fixture/adapter rather than duplicating managym world-space ownership." A Rust change also requires rebuild and is outside this PR's scope. |
| Modify the core `determinized_puct` function to accept a prior | Reuses the existing function with an extra parameter. | The existing function is the Teacher-1 production path with its own evidence/contract. Modifying it risks breaking the existing tests and contracts. A new `conditional_determinized_puct` function is cleaner and keeps the existing path unchanged. |
| Use `determinize` with controlled seeds and post-hoc condition inference | No scenario API needed; reuse the existing determinize flow. | Cannot inspect the opponent's hand post-determinization (no PyO3 method). Cannot evaluate conditions without hand inspection. The scenario API approach constructs worlds with known hands by design. |
| Enumerate all 10,832 possible hands | Full exact range, the beliefs doc's vision. | Too expensive for a vertical slice (10,832 worlds × simulations each). The fixture needs 4 worlds to prove the concept. Full enumeration is a later production capability. |
| Use the selected backend with a Rust-side world configuration | Production-quality, no guard issues. | Requires Rust changes (RUL-8's job). The directive says RUL-8 is not yet publishable; use a local fixture/adapter instead. |
| Build the conditional search as a method on `DeterminizedPuctPlayer` | Integrates with the existing player infrastructure. | The player is a matchup-loop actor, not a conditional query interface. The conditional search is a one-shot query, not a player. A standalone function is the right shape. |

## Key decisions

- **Legacy clone backend for the fixture.** The fixture adapter needs
  unrestricted engine access (scenario API on unguarded forks). The selected
  backend's guard prevents direct mutation. When RUL-8 lands, the production
  path uses the selected backend with Rust-side configuration.

- **WorldQuery evaluates against WorldSpec, not live engine state.** The
  engine deliberately does not expose opponent hand cards to Python.
  Evaluating against the world definition (which the fixture adapter owns by
  construction) avoids needing hand inspection. This is the clean separation:
  RUL-8 owns world definitions; Intelligence owns conditions and search.

- **Reuse PUCT internals from `mcts.py`.** `_Node`, `_select_action`,
  `_simulate`, `_mix_seed`, `_uniform_priors`, `LeafEvaluator`, and
  `UniformRandomLeafEvaluator` are imported and reused unchanged. Only the
  root-world configuration step (scenario API instead of `determinize`) is
  new. This keeps the conditional search a thin layer above the existing
  PUCT machinery.

- **Five conditions in one call, aligned by action identity.** The function
  runs all five conditions (True, Has, Lacks, Q, Not(Q)) in one call, holding
  planner/evaluator/budget/seeds fixed. Results are aligned by action count
  and action labels. Comparison deltas vs True are computed in the result.

- **Two-layer fixture: authority-private + viewer-safe.** The authority-
  private layer (for INT-12) includes world indices, branch receipts, and
  full per-condition detail. The viewer-safe layer (for GAM-6) includes only
  action distributions, Q-values, uncertainty, and deltas — no hidden truth.

- **Named "determinized_puct", no ISMCTS or equilibrium claim.** The
  `planner` field in the result is `"determinized_puct"`. The fixture and
  code explicitly do not claim information-set-consistent search, equilibrium
  solving, or belief inference. Separate trees per world, uniform priors,
  random leaf playouts — same as Teacher-1, just over conditioned subsets.

- **Fail closed on: empty support, identity mismatch, missing artifacts,
  action misalignment.** Each failure mode is tested explicitly. Empty
  support (no world satisfies the condition) raises before any search.
  Identity mismatch (tampered fixture) raises on validation. Action
  misalignment (world configuration changed the root action count) raises
  during search.

## Scope

- In scope: `ConditionalWorldPrior`, `ConditionalQueryPlan`,
  `ConditionalStrategyResult`, `WorldQuery`/`WorldSpec`/`PossibleWorldSpace`
  Protocols, `ScenarioWorldSpace` fixture adapter, `conditional_determinized_puct`
  function, checked fixture JSON, generator script, tests, viewer-safe
  projection, comparison deltas, fail-closed validation.

- Out of scope: Rust/PyO3 changes, RUL-8 implementation, selected-backend
  support for conditional search, belief tracker (likelihood-weighted
  updates), full 10,832-hand enumeration, arena evaluation, multi-seed
  evidence, GAM-6 advisor integration, INT-12 contract exposure, training
  label generation, student distillation from conditional targets.

## Done when

- `uv run pytest tests/sim/test_conditional_search.py -q` passes
- `uv run python scripts/generate_conditional_strategy_fixture.py` produces
  a byte-identical file on two consecutive runs
- The checked fixture at
  `experiments/data/int-13-conditional-strategy-fixture-v1.json` matches the
  regenerated output
- All 5 conditions have aligned action identities (same count, same labels)
- Has(Counterspell) and Lacks(Counterspell) produce different Q-value
  distributions with nonzero comparison deltas
- The viewer-safe layer contains no opponent hand cards or world labels
- `result.planner == "determinized_puct"` and no ISMCTS/equilibrium claim
  appears in the fixture
- Empty support, identity mismatch, and action misalignment each fail closed
  with typed errors
- Root state digest is unchanged after all 5 conditions
- `uv run pytest tests/sim/test_mcts.py -q` still passes (existing PUCT
  tests unchanged)

## Measure

This is a vertical slice, not a strength evaluation. The outcome is binary:
the conditional search runs, produces aligned results for all five
conditions, the fixture is deterministic and identity-pinned, and the
comparison deltas are nonzero where expected (Has vs Lacks diverge on the
spell-cast action's Q-value). No arena, calibration, or latency target is
set — those belong to the Belief-Aware Play KRs that consume this primitive.
