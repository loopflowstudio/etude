# Freeze the first belief-conditioned recommendation flip

## Problem

Etude can already compare two viewer-safe beliefs at one exact decision through
`advice-v1`, but its only retained comparison is too small to demonstrate the
product idea. INT-12 searches 8 traversals over 2 paired worlds at a natural
Pyroclasm-or-pass root. The compatible prior and a Bolt-heavy authored belief
move policy mass by 0.125 without changing the most-visited action.

INT-15 must produce the first frozen result, not another planning abstraction:
hold the decision, viewer, content, planner, evaluator, compute class, and seed
derivation fixed; change only an authoritative typed condition over the
opponent's compatible hands; and show the recommended action changing in a
strategically legible direction. The result benefits players comparing hidden-
information assumptions and developers who need one exact regression fixture
for the whole belief-to-advice path.

A literal `top_action_changed = 1.0` is not sufficient by itself. Kickoff
probes found an apparent flip caused by a 32-32 tie on one side and a 31-33
margin on the other, in the strategically wrong direction. Freezing that would
turn PUCT exploration noise and NumPy's first-index tie break into product
meaning.

The selected result is explicitly post-hoc fixture curation. Kickoff already
probed these three positions at seeds 197, 198, and 199, observed the outcomes,
and chose the four-wide seed-197 case. The retained suite makes that selection
auditable; it does not turn the selected margins or the adjacent-seed outcomes
into preregistered, prospective, or method-stability evidence.

Serving availability is also part of R1, not an incidental byte-comparison
test. On this branch, the exact INT-12 request currently returns
`status: unavailable`, `reason: advisor_artifact_mismatch`: its frozen
`CodeSourceArtifact` names source bundle
`5ac62748027e373da5686bdeb4b4f78bf50c750d7afeb9ba721f62c43133f060`,
while the current `ADVISOR_SOURCE_PATHS` bytes hash to
`f12dce6e0e6ac25add812522eebc96ae63aa1b109c7a072061b2490a7d74158c`.
The existing endpoint test misses this because it compares the endpoint to the
same unavailable adapter response. INT-15 must add a genuinely available,
exact-source-bound fixture and make availability an explicit endpoint
assertion; it must not relabel the historical INT-12 evidence as current.

## The demo

Run:

```bash
uv run python experiments/runners/run_belief_recommendation_flip.py --verify-fixture
```

The command prints one exact `advice-v1` comparison for the same Pyroclasm-or-
pass decision: `Has(Counterspell)` recommends **Pass priority**, while
`Lacks(Counterspell)` recommends **Cast Pyroclasm**. It reports
`top_action_changed: 1.0`, the two top offer IDs and visit margins, typed query
and belief receipts, the pinned 512-traversal/16-world compute identity, exact
response and result hashes, unchanged-root/audit checks, and byte-identical
live/Study/checked-fixture responses. A POST of the exact checked request to
`/api/advice` parses as `status: ok`, `reason: null`, and returns that same
comparison; a one-byte change to any bound advisor source makes the same
request fail closed with `advisor_artifact_mismatch`.

## Approach

### 1. Author a small, fixed position suite on the existing world

Add `experiments/runners/run_belief_recommendation_flip.py`. Keep reusable
search and advice code in `manabot/` and `etude/`; the position definitions and
result orchestration belong in the experiment runner.

The runner owns three immutable candidate specs, all using the existing w2
interactive mirror content and the existing scenario injection surface:

| Candidate | Hero | Opponent | Intended mechanism |
|---|---|---|---|
| `countered-wipe-four-wide-v1` | life 8; Pyroclasm in hand; two ready Mountains | life 12; two ready Islands; two Gray Ogres, Wind Drake, and Raging Goblin | Without Counterspell, wipe the four-creature board; with Counterspell, do not spend the wipe into open UU. |
| `countered-wipe-three-wide-v1` | life 8; same hand and mana | life 12; two ready Islands; two Gray Ogres and Wind Drake | Lower-pressure sensitivity check for the same mechanism. |
| `countered-wipe-buffered-v1` | life 12; same hand and mana | life 12; two ready Islands; two Gray Ogres and Wind Drake | More time to wait, testing whether the direction survives a longer continuation. |

Each candidate starts from a deterministic `GameSession`, applies all scenario
mutations before publishing the decision, refreshes the authoritative priority
offer, and commits one bound command so Game records a canonical `erd1` row and
an exact retained root. Scenario helpers remain measurement-only; they do not
enter live gameplay. The selected fixture is therefore addressed and forked
through the same `LiveAdvisorDecisionResolver` and
`StudyAdvisorDecisionResolver` as INT-12, not through the legacy
`ScenarioWorldSpace` adapter.

`countered-wipe-four-wide-v1` is the chosen fixture candidate. At the fixed
closure budget and seed 197, the kickoff probe measured:

| Condition | Cast visits | Pass visits | Top action | Q(cast) | Q(pass) |
|---|---:|---:|---|---:|---:|
| `Has(Counterspell)` | 223 | 289 | Pass | 0.108 | 0.199 |
| `Lacks(Counterspell)` | 275 | 237 | Cast | 0.265 | 0.224 |

The visit-distribution L1 delta was 0.203. Seeds 198 and 199 also flipped in
the same direction. These observed kickoff outcomes were used to select both
the four-wide position and seed 197, so this is post-hoc fixture curation, not
prospective stability evidence. The result artifact retains all nine
position/seed cells, including every non-flip, and labels the selection as
post-hoc. The checked artifact becomes authoritative only after the exact
runner regenerates the already-selected case through Game and `advice-v1` with
audit enabled; no outcome is upgraded into a method or cross-position claim.

### 2. Derive conditions from managym's typed query authority

Build the compatible-deal prior from the candidate root's canonical
`PossibleWorldSpace`. Construct `WorldQuery.has("Counterspell")` and
`WorldQuery.lacks("Counterspell")`, and obtain a `SupportReceipt` for each from
managym. Restrict the same compatible prior to each support and normalize it
into two `BeliefState` values.

Do not inspect whether the retained actual opponent hand satisfies either
query. Do not use the legacy Python `HasCard` fixture predicate or create a
second hand ontology. The runner must verify that, for each conditioned belief:

- the positive support count equals the authoritative support receipt;
- the pre-normalization exact weight sum equals the receipt's `total_weight`;
- both conditions share one possible-world identity and viewer;
- the canonical query digest, canonical-form digest, support size, exact
  condition mass, normalized belief digest, and provenance identity are frozen;
- query identity is carried by the existing scenario/model/provenance strings
  and evidence manifest, while raw weights and sampled world indexes remain
  private.

The existing advice response's scenario `condition_mass` is the mass of the
already-normalized authored belief and remains 1.0. The authoritative typed
query's mass under the compatible prior is a distinct quantity and belongs in
the INT-15 result artifact beside its `SupportReceipt`; do not relabel one as
the other or change the `advice-v1` schema for this task.

### 3. Retain the post-hoc curation trail, then freeze the selected case

Discovery already happened during kickoff: all three positions were probed at
seeds 197/198/199, and the observed results selected
`countered-wipe-four-wide-v1` at seed 197. Implementation does not replay that
history as a prospective screen. It makes the curation boundary explicit:

1. **Retain the exact curated suite:** rerun and record the fixed 3 × 3 matrix
   at 512 traversals, 16 paired inverse-CDF worlds, seeds 197/198/199,
   `c_puct=1.5`, `max_steps=80`, uniform root priors, and uniform-random
   terminal leaves. Preserve every cell, including non-flips, visit margins,
   Qs, uncertainty, cap hits, and support. Do not add positions, seeds,
   evaluator variants, or budgets, and do not substitute another observed cell
   for the selected fixture.
2. **Freeze the already-selected case:** recompute
   `countered-wipe-four-wide-v1` at seed 197 with the exact same
   planner/evaluator/budget and branch audit on. Run the authority-private
   search twice and compare only `canonical_result_json` for the two
   `ConditionalStrategyResult` values. Then run both live and Study advice
   providers and compare only their canonical serialized public
   `AdviceResponse` bytes before writing the fixture and evidence manifest.

Each invocation records elapsed time and peak RSS in a separate measurement
envelope. Those host/runtime observations are useful cost evidence but are
excluded from `ConditionalStrategyResult` serialization, advice response
bytes, deterministic result/response hashes, and every two-run equality gate.
The retained artifact can contain both envelopes, but verification hashes and
compares only the named deterministic evidence projection.

The compute identity is
`puct-512-traversal-16-world-counterspell-flip-v1`: 512 total traversals per
scenario, 16 sampled worlds per scenario, `c_puct=1.5`, `max_steps=80`, the
selected full-clone branch driver, and root seed 197 under the existing paired
inverse-CDF derivation. This is exactly 64 times INT-12's traversal budget and
eight times its world count. Latency and RSS are recorded in the measurement
envelope but are neither gates nor deterministic identity fields.

### 4. Make a flip a server-verified fact, not an argmax accident

Compute the summary from each scenario's complete visit distribution after the
provider projection. Resolve the winning offer IDs through the authoritative
`AdviceResponse.offers` labels so the public result says `Cast Pyroclasm` and
`Pass priority`, not the searcher's generic `PriorityCastSpell` and
`PriorityPassPriority` labels. Do not extend `advice-v1`: the response already
serves both aligned distributions and their action deltas, and the checked
result manifest can deterministically derive the top-action summary.

The positive fixture gate requires all of the following:

- `top_action_changed == 1.0`;
- `Has(Counterspell)` has unique top offer **Pass priority**;
- `Lacks(Counterspell)` has unique top offer **Cast Pyroclasm**;
- each winning visit margin is at least 32 of 512 traversals (6.25%);
- each scenario's highest-Q offer agrees with its most-visited offer;
- the combined cap-hit rate is at most 35%;
- every offer has realized visits and complete viewer-safe Q/robustness/
  uncertainty evidence;
- the authoritative root digest is unchanged, branch reconciliation succeeds,
  and the branch receipt records zero indexed or unmeasured fallbacks;
- two audited recomputations have byte-identical canonical
  `ConditionalStrategyResult` serialization; their timing/RSS measurement
  envelopes may differ and are not compared;
- fresh live and Study responses have byte-identical canonical public
  `AdviceResponse` serialization and equal the checked fixture response;
- the exact endpoint request returns HTTP 200 and an `AdviceResponse` with
  `status == "ok"`, `reason is None`, and a present strategy/comparison before
  its canonical bytes are compared with the checked response;
- the endpoint returns those exact checked bytes for the exact request and
  fails closed for decision, viewer, condition, compute, seed, source, or
  belief drift.

The margin, Q-agreement, and cap-rate checks are fixture-quality gates only.
They are not prospective stability criteria or general claims about
statistical significance, optimal play, the planner, or the evaluator. They
prevent measured kickoff failure modes from entering one curated regression
fixture without inventing a method-level threshold.

### 5. Close the validation gap before freezing

`conditional_determinized_puct_beliefs` correctly returns an arbitrary nonempty
set of explicit belief scenarios, but `validate_result` still hard-codes the
legacy INT-13 five-condition shape. An audited two-belief kickoff run completed
search and then failed validation with `expected 5 conditions, got 2`.

Refactor validation so the legacy path still defaults to its exact five
condition IDs, while the explicit-belief path passes its ordered expected IDs.
Call the validator inside `AdviceProvider` before viewer-safe projection. The
shared checks must continue to cover simulations, action alignment, world
coverage, planner identity, unique condition IDs, deltas, and branch receipts.
This is an integrity repair to the existing instrument, not a new planner or
result shape.

### 6. Preserve old evidence and add one source-bound fixture registry entry

Keep `protocol/fixtures/advice-belief-conditioned-v1.json` byte-for-byte as the
INT-12 result. Its current file SHA-256 is
`4a3fbeaa8461e00a785e961b9819508d2c1065ae98f058cc50a3783db0945e8d`;
pin that digest in a regression test. Do not rewrite its embedded advisor
artifact to the current source hash. An exact request for that historical
fixture therefore continues to return the honest
`advisor_artifact_mismatch` while its named source bytes are unavailable.

Replace the anonymous tuple inside `request_versioned_fixture_advice` with the
smallest additive registry: one frozen entry type pairing a fixture loader with
the exact source paths used to verify that fixture's advisor artifact. Register
the unchanged INT-12 fixture, the unchanged checkpoint fixture, and the new
INT-15 fixture separately. Exact request matching selects one registry entry;
only that entry's artifact may authorize its response. Identity-near or
request-near inputs continue through the existing typed mismatch logic and
never fall back to another fixture, current source bytes, or a regenerated
response.

The INT-15 `CodeSourceArtifact` binds the final checked bytes of precisely these
ten repository-relative paths, the existing `ADVISOR_SOURCE_PATHS` contract:

- `etude/advice.py`;
- `etude/advice_identity.py`;
- `etude/study_branch.py`;
- `manabot/belief/range.py`;
- `manabot/sim/conditional_search.py`;
- `manabot/sim/mcts.py`;
- `manabot/sim/search_branch.py`;
- `manabot/sim/search_runtime.py`;
- `managym/decision.py`;
- `managym/possible_worlds.py`.

Finish all serving and validation code before generating the INT-15 fixture,
then compute and embed `source_bundle_sha256(ADVISOR_SOURCE_PATHS)` as the
fixture's exact advisor source identity. The checked artifact records the
resulting digest; verify mode and every exact request recompute it from disk via
`RegisteredAdvisor.verify_artifact()`. This is intentionally exact-head
serving, not a promise that future advisor code can serve historical cached
results: any later change to a bound path makes the INT-15 positive endpoint
test fail with `advisor_artifact_mismatch` until a new additive evidence
fixture, with a new identity, is deliberately authored.

Add:

- `protocol/fixtures/advice-belief-conditioned-flip-v1.json` — the exact public
  `VersionedAdviceFixture` request/response;
- `experiments/data/int-15-belief-recommendation-flip-v1.json` — candidate
  suite, typed query receipts, private-safe aggregate search results, selection
  decision, compute/seed/source identities, audit summary, hashes, and claim
  boundary;
- a cached loader for the new fixture and inclusion in the additive
  `request_versioned_fixture_advice` registry;
- focused tests beside the existing belief-advisor and conditional-search
  tests.

The endpoint regression must load the checked INT-15 response as its expected
payload, POST the checked request, parse the actual response independently,
and assert `status == "ok"`, `reason is None`, and the expected flip before
asserting canonical byte equality. It must not derive the sole expectation by
calling `request_versioned_fixture_advice` and comparing two potentially
unavailable responses. A companion artifact-drift test substitutes one changed
bound source byte and requires the exact INT-15 request to return
`advisor_artifact_mismatch`; the checked response is never served before
verification. The existing INT-12 endpoint coverage becomes an explicit
historical-unavailability assertion rather than a false-positive availability
test.

The verify mode performs no writes. `--update-fixture` is the only mode allowed
to replace the INT-15 fixture and result artifact; it first completes all gates
in memory, then writes canonical JSON. It never modifies the INT-12 or
checkpoint-backed fixtures.

Negative closure is available only if exact provider regeneration finds no
qualifying flip anywhere in this exact curated 3-position × 3-seed suite at
512/16 with uniform-random leaves. Retain that complete matrix as
`status: measured_no_flip_in_exact_curated_suite_uniform_random_leaves`. This
means only that these nine already-curated cells did not produce a fixture at
this budget; it is not evidence that other positions, seeds, budgets, or the
planner generally cannot flip. The artifact names the strongest observed
deltas and cap rates, remains exactly rerunnable, and makes no leaf-evaluator,
stability, or strength claim. No new seeds, planner, evaluator, checkpoint,
belief head, content, or schema change is introduced to rescue the result.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Is the existing INT-12 root merely under-budgeted? | At 64 traversals/8 worlds it produced a seed-sensitive apparent flip. At the required 512/16 budget, seed 198 converged to pass/pass: Has visits 236/276 and Lacks 249/263. | Do not scale the natural root alone. Author sharper, shorter positions and retain it only as a calibration control. |
| Can a literal flip be noise? | Yes. Seed 197 at 64/8 tied Has 32/32 and split Lacks 31/33 in the wrong direction; seed 198 flipped correctly; seed 199 did not flip. | Require named direction, unique tops, visit margins, and Q/top agreement. |
| Is the canonical condition support healthy? | The natural INT-12 root has 473,178 worlds; Counterspell-present mass is 0.665 with 298,978 supporting worlds. Empty or vanishing support is not the blocker. | Fail closed on support receipts, but focus curation on continuation signal and cost. |
| Does position curation help without a new evaluator? | The selected life-8/four-attacker root has 10,604 compatible worlds and flips in the intended direction at 512/16 for seeds 197, 198, and 199. Seed 197 margins are 66 visits for Has and 38 for Lacks. These outcomes were observed before selection. | Freeze the post-hoc selected case and retain the entire 3 × 3 curation trail, including non-flips; do not present it as prospective stability evidence. |
| Are terminal leaves actually terminal enough? | INT-12's checked 8-traversal response capped 14/16 playouts; the natural 512/16 probe capped 792/1024. The selected late-game root measured about 24-31% caps across the kickoff-probed seeds. | Freeze cap hits and gate the positive fixture at 35%; prefer the already-curated late-game root rather than increasing `max_steps` and changing the compute class. |
| Is the full budget practical? | With audit off, the selected root runs 512 traversals × 2 scenarios in under one second on the kickoff host. Audit is much slower because it validates every simulated command, but latency is irrelevant to a frozen fixture. | Record elapsed time and peak RSS separately for each run; exclude them from canonical result/response identity and equality. |
| Does paired sampling or the adjacent-seed result prove stability? | No. Common random numbers can reduce comparison variance, but not pointwise estimator variance; moreover seeds 197/198/199 were already observed when seed 197 was selected. | Pair the exact comparisons and retain all observed cells, but make no prospective, cross-seed, or method-stability claim. Margin/Q/cap checks qualify only the selected fixture. |
| Which bytes must reproduce? | `ConditionalStrategyResult` and public `AdviceResponse` are deterministic evidence. Elapsed time and peak RSS vary with host load and process history. | Compare canonical result JSON and canonical response bytes only. Record timing/RSS in a separate unhashed measurement envelope. |
| What is the policy whose argmax matters? | The shipped advice semantic is `puct_visit_distribution/v1`; AlphaZero likewise exposes root visit counts as the search policy. | Define top action from visit mass, while requiring Q agreement only as a fixture-quality sanity check. |
| Can typed conditions use the authoritative world grammar? | `managym.possible_worlds.WorldQuery` and `PossibleWorldSpace.support()` already return canonical query/support receipts. The current advice runner instead hand-filters authored weights. | Construct Has/Lacks through authoritative query receipts and verify the normalized masks against them. |
| Does existing result validation cover advice comparisons? | No. `validate_result` requires exactly five legacy INT-13 conditions and rejects the two-belief result after search completes. | Parameterize expected condition IDs and invoke validation in the provider before projection. |
| Can the fixture replay through both product paths? | Game records a canonical row and retained root before command execution; INT-12 already resolves that capability through separate live and Study adapters and serves exact checked bytes from `/api/advice`. | Build each authored candidate inside `GameSession`; do not freeze a direct `Env` or legacy scenario-world result. |
| Does current endpoint coverage prove the fixture is served? | No. The exact INT-12 request returns `unavailable/advisor_artifact_mismatch` because its embedded source hash is `5ac62748...` and current bound sources hash to `f12dce6e...`; the endpoint test passes by comparing that unavailable response with itself. | Add a per-fixture source-bound registry, keep INT-12 frozen and explicitly unavailable, and require the new endpoint response to parse as `ok` before checking bytes. |
| Can a checked response bypass source validation? | Current `RegisteredAdvisor.verify_artifact()` already fails closed before serving matched fixture bytes, but one global anonymous fixture tuple obscures which registration authorizes which evidence. | Pair every loader with its verification source paths, select the exact entry first, and add a one-byte source-drift regression for INT-15. |

Relevant primary references: [AlphaZero](https://arxiv.org/abs/1712.01815)
for root visit-count policy and [Lee et al. 2020](https://proceedings.mlr.press/v124/lee20a.html)
for common-random-number variance reduction in rollout comparisons. Neither
reference upgrades this fixture into an equilibrium, optimality, or strength
claim.

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Freeze the first observed 64-traversal flip | Smallest change and already yields `top_action_changed=1.0`. | It was tie-driven, seed-unstable, and strategically reversed. It would reward metric gaming. |
| Run only the existing natural INT-12 position at 512/16 | No authored scenario machinery and exact replay already works. | The full-budget probe remained pass/pass and 77% of playouts capped; more of the same search did not sharpen it. |
| Use the legacy four-world `ScenarioWorldSpace` fixture | Very fast and already exposes `HasCard`/`NotQuery`. | It bypasses the canonical managym possible-world identity, typed query receipts, exact-range beliefs, and the production `advice-v1` belief path. |
| Add a stronger checkpoint/value leaf evaluator now | Likely lowers caps and could sharpen more positions. | Explicitly outside directive v1. Only a no-flip result across the exact curated 3 × 3 uniform-random suite can motivate that separate experiment, without claiming a general evaluator deficit. |
| Add an explicit `top_action_changed` field to `advice-v1` | Convenient for clients. | The complete aligned distributions already make the summary deterministic. A protocol change is unnecessary for R1 and would enlarge the compatibility surface. |
| Replace the INT-12 checked fixture with the flip | Leaves the endpoint with one belief fixture. | INT-12 is frozen evidence and must remain independently reproducible. Add an exact-match fixture instead. |
| Refresh INT-12's embedded source hash so its endpoint becomes available again | Would make the old endpoint test appear positive with little code. | It silently rewrites historical evidence under code that did not produce it. Keep its file SHA-256 and artifact identity frozen; current-code serving belongs to the new additive INT-15 fixture. |
| Serve a checked response without revalidating current source bytes | Historical fixtures would remain available across refactors. | It weakens the fail-closed contract and permits evidence from different advisor code to masquerade as current. Exact-head availability is the honest smallest R1 repair. |
| Search additional seeds or substitute another observed cell | Could produce a wider-margin or easier fixture. | It would extend the already post-hoc curation and hide the actual selection trail. Freeze the disclosed four-wide seed-197 case and retain all nine already-probed cells without further search. |

## Key decisions

- The selected case is the life-8, four-attacker Pyroclasm decision under
  authoritative `Has(Counterspell)` versus `Lacks(Counterspell)`.
- The final compute class is 512 traversals and 16 paired worlds per scenario,
  with seed 197, `c_puct=1.5`, `max_steps=80`, and uniform-random terminal
  leaves. No budget tuning follows the result.
- Kickoff already observed the three positions at seeds 197, 198, and 199 and
  post-hoc selected four-wide seed 197. The result retains all nine cells and
  makes no preregistered, prospective, or cross-seed stability claim.
- The product result is a visit-policy argmax flip with a named strategic
  direction. Q agreement and visit margins protect the fixture from PUCT
  exploration/tie artifacts; margin, Q, and cap checks are fixture-quality
  only, not general admission or method criteria.
- Typed query authority and condition mass come from managym support receipts;
  probability and normalization remain manabot-owned.
- Actual hidden truth, raw sampled worlds, weights, world Q arrays, RNG tapes,
  and branch receipts do not enter the public response.
- The new fixture and registry entry are additive. INT-12 remains at file
  SHA-256 `4a3fbeaa...`, its drifted source artifact is not refreshed, and the
  checkpoint evidence remains unchanged.
- INT-15 binds the exact final bytes of the ten existing
  `ADVISOR_SOURCE_PATHS`. Availability is verified on every request; source
  drift is a typed failure and requires a new evidence identity, not an
  adapter relaxation.
- Positive endpoint proof means parsed `status == "ok"` plus checked canonical
  bytes. Comparing the endpoint with another adapter call is insufficient.
- No `advice-v1` schema, planner, evaluator, content, position, budget, or
  additional-seed search is introduced; INT-15 freezes and serves the disclosed
  curated result on the existing instrument.
- Two-run identity covers only canonical `ConditionalStrategyResult` and
  public `AdviceResponse` bytes. Elapsed time and peak RSS live in a separate
  recorded measurement envelope excluded from deterministic hashes and
  equality.
- The claim boundary is narrow: this proves belief-sensitive, reproducible,
  viewer-safe advice on one post-hoc curated w2 fixture. It does not prove
  stronger play, calibrated beliefs, stability across seeds or positions,
  information-set consistency, equilibrium behavior, or a service latency.

Wild success is that this becomes the canonical Etude product moment: a player
can toggle one intelligible hidden-card assumption and see the recommendation
change, while every number remains attributable and replayable. Developers can
then reuse the fixture to catch regressions in typed query identity, belief
normalization, planning, Game/Study parity, and endpoint serving.

Wild failure is a beautiful but meaningless demo: a favorable seed turns a
tie into a flip; the actual hidden hand leaks into condition construction; the
public artifact exposes sampled worlds; or a cap-dominated random rollout is
described as strategically strong. The fixed suite, authoritative receipts,
direction/margin/cap gates, private projection, and narrow claim boundary are
the protections against that outcome.

## Scope

- In scope:
  - three fixed authored positions using the current interactive mirror;
  - authoritative Has/Lacks Counterspell support receipts and conditioned
    compatible-prior beliefs;
  - the 512-traversal/16-world paired uniform-random dPUCT run;
  - shared result-validation repair for explicit belief scenario counts;
  - one additive, exact `advice-v1` flip fixture and one retained result
    artifact;
  - the minimal per-fixture registry/source-validation repair needed to serve
    that exact current-code result honestly;
  - live/Study/endpoint byte parity, source/identity binding, viewer safety,
    and negative-result closure.
- Out of scope:
  - any `advice-v1` schema change;
  - any new or changed planner, evaluator, ISMCTS, CFR, or public-belief
    solving;
  - any further position, seed, budget, or content search beyond the disclosed
    curated 3 × 3 suite;
  - checkpoint priors, learned/value leaf evaluation, or training;
  - a supervised belief head or live belief tracking;
  - a wider content pool or new rules mechanics;
  - arena admission, ratings, strength, calibration, or service-SLO claims;
  - UI work or changes to Game's player-authored belief surface.

## Done when

The implementation advances Intelligence R1: “A frozen, exactly replayable
fixture shows changing only the typed condition ... flipping the advised top
action under paired seeds at a declared offline budget, served through the
`advice-v1` comparison.”

Positive closure requires:

```bash
uv run python experiments/runners/run_belief_recommendation_flip.py --verify-fixture
uv run pytest tests/etude/test_belief_recommendation_flip.py tests/etude/test_belief_advisor.py tests/sim/test_conditional_search.py -q
uv run ruff check experiments/runners/run_belief_recommendation_flip.py etude/advice.py manabot/sim/conditional_search.py tests/etude/test_belief_recommendation_flip.py tests/etude/test_belief_advisor.py tests/sim/test_conditional_search.py
cargo test --manifest-path managym/Cargo.toml
```

The verify command must exit zero without writes and print the exact positive
gate evidence. The focused tests must prove fixture/schema round-trip,
authoritative query receipts, provider validation, direction/margins,
privacy, root preservation, byte identity for canonical
`ConditionalStrategyResult` and public `AdviceResponse` only, exclusion of
elapsed time and peak RSS from deterministic hashes/equality, live/Study
parity, endpoint `status == "ok"` before byte equality, the unchanged INT-12
file digest and honest historical `advisor_artifact_mismatch`, one-byte INT-15
source-drift rejection, and all other typed fail-closed perturbations. The
retained artifact must disclose the post-hoc selection and all nine curated
cells. Debug Rust remains green because CI runs debug assertions even though
INT-15 should not change Rust.

Negative closure applies only if no cell in the exact retained 3 × 3 curated
suite passes the positive flip gate at 512/16. Done then means the same commands
verify `measured_no_flip_in_exact_curated_suite_uniform_random_leaves`; it says
nothing about untested positions, seeds, budgets, planners, or evaluators.

## Measure

Baseline:

- INT-12: 8 traversals, 2 worlds, max policy delta 0.125, no argmax flip,
  14/16 cap hits in the checked response.
- Existing natural Pyroclasm root at 512/16, seed 198: Has 236/276 and Lacks
  249/263 (pass/pass), visit L1 0.0508, 792/1024 cap hits.

Deterministic evidence, included in result/response hashes and equality:

- `top_action_changed` and the two named top offers;
- per-scenario visit distributions, winner margins, Q values, robustness,
  uncertainty, root values, and cap hits;
- condition support, exact prior mass, query/canonical digests, and normalized
  belief digests;
- realized simulations, sampled/unique worlds, tree nodes, and audit counters;
- root/result/request/response/source/fixture SHA-256 identities;
- equality across two canonical `ConditionalStrategyResult` serializations and
  across live/Study/endpoint public `AdviceResponse` bytes;
- parsed endpoint availability (`status: ok`, `reason: null`) for the exact
  INT-15 request, alongside fail-closed source drift and the frozen INT-12 file
  digest.

Recorded measurement envelope, excluded from all deterministic hashes and
equality gates:

- elapsed time for each suite and audited-fixture invocation;
- peak RSS for each invocation;
- the measurement host/profile identity needed to interpret those values.

“Better” for this task means a strategically directed, exactly replayable flip
for the disclosed post-hoc curated fixture on the current instrument. It does
not mean stability across seeds or positions, a higher win rate, or a stronger
manabot; R4 owns that measurement.
