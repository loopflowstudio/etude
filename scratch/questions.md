# RUL-8 — confirmed interpretation, assumptions, and blockers

## Blocker (resolved)

- `lf task acknowledge RUL-8 --directive 1` initially failed (local Loopflow
  store needed migration `0.11.029_ci_incident_repaired_head`, unknown to an
  older lf). `lf doctor` now reports the migration applied, and the retry
  succeeded: `RUL-8 incorporated directive v1`. The kickoff lifecycle is
  closed; implementation may begin from
  `scratch/add-the-first-viewer-relative.md`.

## Confirmed interpretation

- "Selected exact hand-count hypothesis domain" = the opponent's hand as a
  card-**name** multiset of the public hand size, drawn from the opponent's
  unseen pool (Hand ∪ Library). The two-deck slice (UR Lessons vs GW Allies)
  is the selected matchup; the space is built from any live `Game` state and
  proven on the authored match.
- A `PossibleWorld` is one name-multiset; its weight is the exact
  multivariate-hypergeometric count Π C(n_i, k_i) — the number of physical
  `CardId` deals yielding that multiset. Weights are exact integers, **not**
  probabilities. Normalization to a distribution is a manabot concern and is
  out of scope.
- `WorldQuery` grammar is exactly `True`, `Has`, `Lacks`, `Q`, and `Not(Q)`.
  `Q` is an exact-count query; `Not` applies only to a `Q` (per "Not(Q)"),
  not to `Has`/`Lacks`. This is a deliberately minimal grammar, not a general
  boolean algebra.
- "Equivalent queries normalize identically" = a `canonicalize(space)` folds
  tautologies and impossibilities against the pool, so semantically-equivalent
  constructions share one canonical form, one blake3 digest, and one support.
- "Conditioning and explicit empty-support failure" = `condition(query)`
  returns `Err(EmptySupport { query_digest })` when no world matches; a
  separate `support_receipt(query)` always reports the count/weight.
- "Deterministic materialization into exact authority branches" = clone the
  source `Game`, reassign **only** the opponent's hand+library split to match
  the world (lowest `CardId`s to hand; seeded library-order shuffle), reusing
  the `resample_hidden` zone-update pattern. Deterministic per (source, world,
  seed).

## Assumptions

- `managym` owns the domain and materializer (Rust). No Python bindings, UI,
  learned inference, or information-set planning claims in this slice.
- RUL-7 owns the semantic Observation contracts and has no published parent
  PR. This slice uses only `Observation::for_player` (on main) and existing
  fork/zone primitives; no RUL-7 contract is duplicated, so later Study/search
  integration is mechanical.
- The viewer's own library order is **not** part of this hypothesis domain
  (only opponent hand counts). Materialization leaves the viewer's zones
  exactly as the authority has them, which is what makes the
  "preserve source Observation" proof hold.
- Card **name** (String) is the hypothesis key (viewer-meaningful and stable
  across runtime-ID reordering), sorted in a `BTreeMap` for deterministic
  enumeration. Runtime `registry_key` is deliberately not the key.
- Validate with `cargo test` in **debug** (CI runs debug; `debug_assert!`
  compiles out of release). Rebuild the cp312 extension after Rust changes.

## Validation evidence (implementation)

- `cargo test --manifest-path managym/Cargo.toml` (full debug suite) passes;
  `cargo clippy --all-targets --all-features -- -D warnings` and
  `cargo fmt --check` are clean.
- `possible_worlds` inline unit tests (7) and the
  `managym/tests/possible_worlds_tests.rs` integration proof (5) pass on the
  authored UR-Lessons-vs-GW-Allies match, covering exact weights, brute-force
  cross-check, canonicalization folding, empty-support failure, and the three
  viewer-safety proofs (Observation preservation, no opponent-hand identity
  leakage, distinct worlds → identical viewer projection).
- The cp312 extension is rebuilt; `uv run pytest tests/ -q` is 558 passed, 2
  failed. The 2 failures
  (`tests/sim/test_visit_iteration_runner.py::test_int4_contract_binds_runtime_and_both_execution_profiles`,
  `tests/sim/test_visit_teacher_production.py::test_production_contract_binds_frozen_iteration_and_runtime`)
  are **pre-existing on origin/main**, not caused by this slice:
  - `production_source_sha256` hashes only Python sources + a JSON schema; it
    drifted on main when `d7fe8ac "visit teacher: integrate selected branch
    driver"` touched `mcts.py`, `flat_mc.py`, and `teacher1_evidence.py` after
    the contracts were frozen (`int-4-visit-teacher-iteration-v1.json` at
    `9b0f51f`; `int-4-visit-teacher-production-v1.json` at `37ccf4c`).
  - `engine_source_sha256` likewise already mismatched on clean main for the
    same reason.
  - Verified by stashing this slice's Rust changes, rebuilding the cp312
    extension from clean origin/main, and re-running the 2 tests: both still
    fail with the same expected-vs-actual mismatches. This slice touches no
    Python, no experiment contract, and no `engine_source_sha256`/`production_source_sha256`
    input in a way that changes pass/fail (the contracts were already stale).
  - Updating those frozen INT-4 pre-registration contracts is the experiment
    owner's job and is out of scope for this managym slice.

