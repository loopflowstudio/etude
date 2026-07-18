# Unify the semantic decision contract (R1 vertical slice)

Task-local design for the bounded recovery turn. Authority: PR #139
`docs/ARCHITECTURE.md` (belief-conditioned system architecture). Scope is
integration step 1 / Rules R1: the **smallest shared semantic**
`DecisionFrame`/`Command`/`Observation`/`TransitionReceipt` vertical slice
with one Etude consumer and one manabot consumer. No broad protocol rewrite;
no RUL-8 world-space (`PossibleWorldSpace`/`WorldQuery`) work.

## What exists today (do not rewrite)

- `managym/src/experience.rs` — Etude protocol-v1 wire types
  (`experience::Command`, `CommandReceipt`, `ExperienceFrame`). Rust-internal;
  Python mirror is `etude/experience_protocol.py`. Used by `study.rs`,
  `canonical_replay.rs`, and internally in `apply_policy_choice`.
- `managym/src/agent/structured_offer.rs` — the action-aligned structured
  surface actually wired to PyO3: `StructuredOfferSet`,
  `OfferSubmission`, `AtomicCommand`, `OfferSetBinding = decision_epoch`.
  `structured_search_offers()` covers **every** action-space kind (one offer
  per legal action) and is the universal legal-action identity.
- `managym/src/agent/observation.rs` — `agent::Observation::for_player(viewer)`
  is the existing viewer-safe projection (opponent hand suppressed; non-acting
  viewer's candidates/focus suppressed).
- `Game::decision_epoch` (`pub(crate)`) is the revision. `Game::take_observation_events()`
  drains the viewer-safe observation event log.

## The slice

New managym module `managym/src/decision.rs` defines the four shared semantic
types and projects them from the authoritative `Game`. Positional action
indices stay private; offers come from `structured_search_offers()`.

```rust
DecisionFrame { revision, actor, fingerprint, offers }   // revision-bound
Command { command_id, expected_revision, offer_id, answers } // precondition-bound
Observation { identity, viewer_state, events, decision }     // viewer-safe composite
TransitionReceipt { before_revision, after_revision, command_id, events, next_decision } // fail-closed
```

- `DecisionFingerprint` = sha256 hex of canonical `{ revision, projection }`
  (projection = `StructuredOfferProjection`). Any change to the legal offer
  set changes the fingerprint -> exact legal-action identity preserved.
- `ObservationIdentity` = `{ schema_version, revision, viewer, viewer_state_hash }`.
  `viewer_state_hash` = sha256 of the viewer-safe `agent::Observation::for_player(viewer).to_json()`
  (NOT the authority-private `MatchStateHash`). Match identity stays Etude-owned;
  this slice does not invent a match id.
- `EventIdentity` = sha256 hex of a canonical `EventData` projection (reuses
  `agent::observation::event_data`, made `pub(crate)`).
- `Observation.decision` is `Some` only when `viewer == actor`; a non-acting
  viewer sees state + events but not the acting player's legal offers.
- `Observation.events` is the ordered newly-visible event increment produced
  by `execute`; a static `observe` returns `events: []`.

## Game / Env methods

- `Game::semantic_decision_frame()` — projects `structured_search_offers()`
  into a `DecisionFrame` at `decision_epoch`. Fails closed on game over / no
  active decision.
- `Game::semantic_observation(viewer)` — composite `Observation` via
  `agent::Observation::for_player`. Asserts viewer safety (no opponent hand)
  and returns `SemanticError::ViewerSafetyViolation` otherwise.
- `Game::execute_semantic_command(&Command) -> (TransitionReceipt, Observation)`:
  1. game over -> `GameOver` (no mutation).
  2. `expected_revision != decision_epoch` -> `StaleRevision` (no mutation).
  3. re-project `structured_search_offers()` (deterministic at this revision);
     decode `OfferSubmission { offer_id, answers }` (fail-closed).
  4. `apply_offer_submission` (atomic, rollback on error) -> `done`.
  5. drain observation events; `after_revision = decision_epoch`.
  6. receipt `{ before, after, command_id, event identities, next_decision }`;
     `next_decision = None` if terminal else next frame fingerprint.
  7. next `Observation` for the offer's actor.
- `Env::semantic_decision_frame / semantic_observation / execute_semantic_command`
  thin wrappers.

## PyO3 exposure (mirrors `projection_json()`)

Three `Env` methods returning JSON, parsed by Python consumers:
- `semantic_decision_frame_json() -> str`
- `semantic_observation_json(viewer: int) -> str`
- `execute_semantic_command_json(command_json: str) -> str` (JSON of `{receipt, observation}`)

## Consumers (one each side; real adapter modules + tests)

- **manabot**: `manabot/semantic/decision_contract.py` —
  `SemanticDecisionContract.from_env(env)` reads the frame; `.apply(env, offer_id)`
  builds a revision-bound `Command`, executes, returns `(receipt, observation)`.
  Test: `tests/semantic/test_decision_contract.py` (drive a real match decision;
  stale/fabricated commands fail closed; receipt revisions advance).
- **Etude**: `etude/semantic_boundary.py` —
  `SemanticExperienceBoundary.observe(env, viewer)` returns the viewer-safe
  composite observation; `.apply(env, command)` returns the receipt. Test:
  `tests/etude/test_semantic_boundary.py` (assert no opponent-private hand in
  viewer_state; apply a command; receipt binds before/after revision).

## Guarantees checked

- Exact legal-action identity: offers from authoritative action space; fingerprint digest.
- Viewer safety: `for_player` projection + explicit opponent-hand assertion.
- Revision/precondition binding: `expected_revision == decision_epoch` enforced before mutation.
- Fail-closed receipts: validate-then-apply with rollback; no receipt on error.

## Non-goals (this slice)

- Migrating `etude/server.py` live play off legacy `Env.step(index)`.
- Deleting `experience::Command` / `structured_offer::OfferSubmission` duplicate constructors.
- `MatchAuthority` aggregate, match-id ownership, canonical Observation history (R2),
  possible-world query kernel (R3/RUL-8).

## Validation

- Rust: `cargo fmt --check`, `cargo clippy --all-targets --all-features -- -D warnings`,
  `cargo test` (debug — CI runs debug).
- Rebuild ext: `uv run --python 3.12 --extra play maturin develop --release --manifest-path managym/Cargo.toml --features python`.
- Python: `uv run pytest tests/semantic/test_decision_contract.py tests/etude/test_semantic_boundary.py`.
