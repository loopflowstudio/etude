# First viewer-relative PossibleWorldSpace + WorldQuery slice

## Problem

INT-10's search-learning architecture needs a viewer-relative hypothesis
space before any belief-aware search or Study exploration can reason about
*why* the opponent might hold a card. Today `managym`'s only hidden-info
tool is `Game::determinize`, which uniformly resamples the opponent's
hand+library — it cannot express "worlds where the opponent has at least one
counterspell", cannot weight compatible deals exactly, cannot report when a
hypothesis is impossible, and cannot materialize a *specific* hypothesized
hand into an exact authority branch. Search and Study therefore cannot ask a
typed question about hidden hand counts and get a deterministic, exact,
viewer-safe answer back.

This slice adds the first such space to `managym`: a minimal viewer-relative
`PossibleWorldSpace` over the selected two-deck opponent hand-count domain,
a typed `WorldQuery` grammar (`True`, `Has`, `Lacks`, `Q`, `Not(Q)`), exact
compatible-deal weights, deterministic query digests, conditioning with
explicit empty-support failure, and a deterministic materializer into exact
`Game` branches. managym owns the domain and materializer; manabot beliefs,
learned inference, UI, a general query language, and information-set planning
claims are explicitly out of scope. RUL-7 owns the semantic Observation
contracts (no published parent PR yet), so this slice works behind a narrow
adapter on main and makes later Study/search integration mechanical.

## The demo

```
cargo test --manifest-path managym/Cargo.toml possible_worlds -- --exact
```

A Rust test builds the selected UR-Lessons-vs-GW-Allies match, steps it to a
non-terminal decision, and constructs a `PossibleWorldSpace` for the acting
viewer. It enumerates every compatible opponent-hand deal with exact
multivariate-hypergeometric weights, conditions on `Has("Firebending Lesson",
1)`, gets a `ConditioningReceipt` listing the support worlds, then
materializes one world into an exact `Game` branch and asserts: the viewer's
`Observation::for_player` is byte-identical to the source, the opponent's
hand card identities are absent from that projection, and the viewer-visible
legal decision surface is unchanged. An impossible query (`Has(..., 99)`)
returns `Err(EmptySupport)`; `Has(..., 0)` and `True` normalize to the same
digest and the same support.

## Approach

A new `managym/src/possible_worlds.rs` module (wired into `lib.rs`), plus an
integration test in `managym/tests/possible_worlds_tests.rs` built on the
authored two-deck match and the existing `interactive-midgame-48-v1` fixture.

### Domain

A `PossibleWorld` is one opponent-hand card-**name** multiset of the public
hand size `H`, drawn from the opponent's unseen pool (Hand ∪ Library). The
unseen pool multiset `{name -> n_i}` is derived purely from the live `Game`:
the names of all opponent cards in `Hand ∪ Library` (`zones.zone_cards` +
`cards[id].name`). The decklist is implicit; public cards are excluded by
construction (they are not in Hand ∪ Library).

A world's weight is the exact multivariate-hypergeometric count
`Π_i C(n_i, k_i)` — the number of physical `CardId` deals that yield that
name-multiset. Total weight `= C(N, H)`, `N = Σ n_i`. Weights are exact
`u128` integers, **not** probabilities; normalization to a distribution is a
manabot concern and is out of scope. Card **name** is the hypothesis key
(viewer-meaningful and stable across runtime-ID reordering), held in a
`BTreeMap<String, u32>` for deterministic enumeration.

### `WorldQuery` grammar

Exactly five forms over a world's per-name hand count `count[card]`:

- `True` — tautology.
- `Has { card, at_least }` — `count[card] >= at_least`.
- `Lacks { card, fewer_than }` — `count[card] < fewer_than`.
- `Q(CountQuery)` where `CountQuery::Exactly { card, count }` — `count == exactly`.
- `Not(CountQuery)` — negation of a `Q` ("Not(Q)"). `Not` applies only to a
  `CountQuery`, never to `Has`/`Lacks` — a deliberately minimal grammar, not
  a general boolean algebra.

`Has(k)` and `Lacks(k)` are exact complements; together with `Q(=)` and
`NotQ(≠)` the grammar covers `≥, <, =, ≠` plus the tautology, which is the
complete set of single-card count comparisons a hand-count hypothesis needs.

### Canonicalization and digests

`WorldQuery::canonicalize(&self, space) -> CanonicalWorldQuery` folds
tautologies and impossibilities against the space's pool:

| Input | Condition | Canonical |
|---|---|---|
| `Has { card, 0 }` | — | `True` |
| `Has { card, k }` | `k > pool[card]` | `Empty` |
| `Lacks { card, 0 }` | — | `Empty` (`count < 0` impossible) |
| `Lacks { card, k }` | `k > pool[card]` | `True` (`count ≤ pool < k` always) |
| `Q { card, 0 }` | — | `Lacks { card, 1 }` (`==0 ⟺ <1`) |
| `Q { card, k }` | `k > pool[card]` | `Empty` |
| `Not(Q { card, 0 })` | — | `Has { card, 1 }` (`≠0 ⟺ ≥1`) |
| `Not(Q { card, k })` | `k > pool[card]` | `True` (`≠` impossible count) |
| otherwise | — | unchanged form |

`CanonicalWorldQuery` is a normalized enum (`True | Empty | Has | Lacks | Q |
NotQ`). The digest is `blake3` of a deterministic JSON serialization of the
canonical form. Idempotent by construction. Semantically-equivalent queries
share one canonical form, one digest, and one conditioned support.

### Conditioning and support receipts

- `space.support_receipt(query) -> SupportReceipt` — always succeeds; reports
  `query_digest`, `canonical_digest`, `support_size`, `total_weight`. Zero
  support is reported, not an error.
- `space.condition(query) -> Result<ConditioningReceipt, ConditioningError>` —
  returns the support worlds (with weights) and the receipt, or
  `Err(ConditioningError::EmptySupport { query_digest, canonical_digest })`
  when no world matches. Explicit empty-support failure, never a silent empty
  list.

### Materializer

`space.materialize(source: &Game, world: &PossibleWorld, seed: u64) -> Result<Game, MaterializeError>`:

1. Clone the source `Game` (exact fork).
2. Validate the world is realizable from the opponent's pool
   (`k_i ≤ n_i`, `Σ k_i = H`); else `Err(MaterializeError::InconsistentWorld)`.
3. Take the opponent's Hand ∪ Library `CardId`s, group by card name. For each
   name, place the `k_i` **lowest** `CardId`s of that name into the hand
   (deterministic), the remainder into the library.
4. Hand stored sorted by `CardId`; library shuffled by `ChaCha8Rng::seed_from_u64(seed)`
   for continuation variety. Update `card_zones[card]` for every moved card
   (the `resample_hidden` pattern).
5. The viewer's zones, all public zones, and any suspended-decision revealed
   cards (the viewer's) are untouched.
6. Assert the resulting hand name-multiset equals `world.hand`; fail closed
   otherwise.

Deterministic per `(source, world, seed)`. The materialized `Game` is a valid
authoritative state any `BranchDriver` can consume. No `journal_zones` call:
the branch is an independent clone; undo/rollback is the driver's job.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Is the unseen pool derivable from `Game` without the decklist? | Yes. Opponent unseen pool (name multiset) = names of opponent cards in Hand ∪ Library, read via `zones.zone_cards` + `cards[id].name`. Public cards are excluded by construction. | No decklist API; build the space from live state only. |
| Are same-named cards exchangeable for weights? | Yes. The engine deals in `CardId`s but same-named cards are rules-identical; `resample_hidden` already relies on this. `Π C(n_i, k_i)` is exactly the physical-deal count for a name-multiset. | Use card name as the key; weight = product of binomials. |
| Do weights overflow? | No. Two-deck pool `N ≤ 40`, `H ≤ 7`; `C(40,7) ≈ 1.86e7`. `u128` is ample for per-world weights and the total. | Store weights as `u128`. |
| Does materialization preserve engine invariants? | Yes. Moving cards between Hand/Library and updating `card_zones` is exactly `resample_hidden`'s operation. Public zones and the viewer's zones are untouched. | Reuse the `resample_hidden` zone-update pattern; no new invariant. |
| Does materialization disturb a suspended decision's revealed cards? | No. Suspended-decision revealed cards belong to the deciding viewer's library; materialization touches only the opponent's hand+library. | No revealed-card pinning needed (unlike `determinize`, which also reshuffles the viewer's library). |
| Is the viewer's `Observation` preserved? | Yes by construction: `Observation::for_player` never adds opponent hand cards; it exposes only the viewer's hand + public zones + the hand-size count. Materialization changes only opponent hand membership. | The proof is a structural/byte equality assertion, not a new mechanism. |
| Is the viewer-visible legal decision preserved? | Yes. If the viewer is acting, their action space depends on their hand + public state (unchanged). If the opponent is acting, `for_player` suppresses action candidates (`expose_actions=false`), so the projected space is empty in both source and branch. | Assert action-space equality on the viewer projection. |
| Does RUL-7 own anything this slice needs? | RUL-7 owns semantic Observation contracts not yet on main. This slice uses only `Observation::for_player` (on main) + existing fork/zone primitives. No RUL-7 contract is duplicated. | Build behind a narrow adapter on main; later Study/search integration is mechanical. |
| Should materialization emit a probability? | No. The directive forbids belief probabilities. Weights are exact combinatorial counts; normalization is a manabot concern. | Receipt carries integer weights + total; no `f64` distribution. |
| Is full enumeration tractable? | Yes. ≤ ~10k distinct 7-card hands for the two-deck pool; recursive enumeration over sorted names is fast. | Enumerate eagerly; cache the full space on first condition. |
| Will adding a Rust module break the Python extension? | No new PyO3 surface, but the on-disk `.so` is stale until rebuilt. CI rebuilds via maturin. | Rebuild the cp312 `.so` locally so `uv run pytest` stays green. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Build it in Python/etude over the existing `determinize` | Faster to wire to Study, but the hypothesis domain, exact weights, and materializer are rules-owned authority, and the directive assigns them to managym. Python would duplicate combinatorics and fork logic. | managym owns the domain and materializer. |
| Add a general boolean algebra (And/Or/Not over all predicates) | More expressive, but the directive limits the grammar to `True/Has/Lacks/Q/Not(Q)` and forbids "a general query language." | Out of scope; the minimal grammar covers the hand-count hypothesis decisions. |
| Use runtime `registry_key` (i32) as the hypothesis key | Tied to runtime table order, which the semantic-kernel direction says must not be meaning. | Use card-name strings (sorted) as the stable, viewer-meaningful key. |
| Sample worlds instead of enumerating | Matches `determinize`'s uniform sampling, but the directive requires "enumerate compatible physical deals with exact weights" and conditioning receipts. Sampling cannot prove exact support or empty-support. | Enumerate the full space; sampling remains the existing search path's job. |
| Materialize by reshuffling both players' libraries (like `determinize`) | Would change the viewer's library order and break the "preserve source Observation" proof. | Materialize only the opponent's hand+library split; leave the viewer's zones as the authority has them. |
| Add Python/Study bindings now | Would enable immediate Study consumption, but RUL-7's parent PR isn't published and the directive says make later integration mechanical, not now. | Rust-only slice; bindings land with the RUL-7 integration. |

## Key decisions

- Card **name** (`String`) is the hypothesis key; `BTreeMap<String, u32>`
  for deterministic enumeration. Runtime `registry_key` is deliberately not
  the key.
- Weights are exact `u128` multivariate-hypergeometric counts; no
  probabilities, no `f64`.
- The grammar is exactly five forms; `Not` applies only to a `CountQuery`
  (`Q`), per "Not(Q)".
- Canonicalization uses the space's pool to fold tautologies/impossibilities,
  so equivalent queries share one canonical form and one blake3 digest.
- Empty support is an explicit `Err(ConditioningError::EmptySupport)`, never a
  silent empty list. A separate `support_receipt` reports the count without
  materializing worlds.
- Materialization reassigns only the opponent's hand+library, deterministically
  (lowest `CardId`s to hand; seeded library shuffle), reusing the
  `resample_hidden` zone-update pattern. Deterministic per
  `(source, world, seed)`.
- The viewer's `Observation` and viewer-visible legal decision are preserved
  by construction and asserted in tests — the property that makes this
  viewer-safe rather than a leakage vector.
- managym-only Rust; no Python bindings, no UI, no learned components.

## Scope

- In scope: `managym/src/possible_worlds.rs` (`PossibleWorldSpace`,
  `PossibleWorld`, `CountQuery`, `WorldQuery`, `CanonicalWorldQuery`,
  `SupportReceipt`, `ConditioningReceipt`, `ConditioningError`,
  `MaterializeError`, `materialize`); `pub mod possible_worlds;` in `lib.rs`;
  inline unit tests + `managym/tests/possible_worlds_tests.rs` on the authored
  UR-vs-GW match and the `interactive-midgame-48-v1` fixture; rebuild the
  cp312 extension.
- Out of scope: Python/PyO3 bindings; Study or search consumer integration;
  manabot belief probabilities or Bayesian update; learned inference; UI
  wording; a general boolean query language; information-set-consistent
  planning claims; the viewer's own library-order hypothesis; persisted
  schema or replay migration; new latency/RSS budgets.

## Done when

- `cargo test --manifest-path managym/Cargo.toml possible_worlds -- --exact`
  and `cargo test --manifest-path managym/Cargo.toml` (full suite) pass in
  **debug**.
- `cargo clippy --all-targets --all-features -- -D warnings` is clean and
  `cargo fmt --check` is clean.
- The three required proofs pass:
  1. Equivalent queries normalize to identical canonical forms, digests, and
     conditioned support (`Has(...,0)≡True`; `Not(Q(...,99))≡True` when the
     pool is smaller; `Q(...,0)≡Lacks(...,1)`; `Lacks(...,99)≡True`).
  2. Hidden truth never enters the viewer's public values: a materialized
     world's `Observation::for_player` contains no opponent hand card
     identities (only the public hand-size count), and two worlds that differ
     in opponent hand contents yield identical viewer projections.
  3. Materialized worlds preserve the source `Observation` and viewer-visible
     legal decision: `for_player(materialized, viewer) ==
     for_player(source, viewer)` across worlds; the viewer-visible action
     surface is unchanged; the source `Game` is not mutated.
- Empty-support queries return `Err(EmptySupport)` and `support_receipt`
  reports `support_size == 0`.
- Weights are exact: total `== C(N, H)` and a sampled world's weight
  `== Π C(n_i, k_i)`, cross-checked against a brute-force physical-deal count
  on a small fixture.
- The cp312 extension is rebuilt and `uv run pytest tests/ -q` still passes
  (no Python surface changed, so this is a regression check).

## Measure

Binary correctness gate, no new quantitative target in this slice: zero
query-digest drift across equivalent constructions; zero opponent-hand
identity leakage into the viewer projection; zero source-`Observation` or
viewer-legal-decision drift across materialized worlds; explicit
empty-support failure on impossible queries. The wave's KR4/KR5 latency/RSS
budgets remain a later production-integration receipt; this slice does not
select or tune a representation.
