# Finish the Belief-to-Strategy Comparison with Conditional Evidence

## Problem

GAM-6 already ships one reusable Etude decision-advice component in live play
and Study, a checked `StudyArtifact` fixture, two viewer-safe belief scenarios,
explicit policy/value/uncertainty deltas, and fail-closed identity validation.
The remaining evidence-integrity gap is in the fixture generator: its two
strategies came from disjoint flat-MC seed families at the same root. That made
the visible delta reproducible, but did not prove that changing only the belief
changed strategy.

The owning Project resolved decision `cd_7505e6cc681a4a0590007e43752196de`
in favor of regenerating the existing fixture from the merged conditional
determinized-PUCT substrate. This serial slice changes evidence production,
not the player surface. GAM-7 still owns the live provider.

## The demo

Run:

```bash
uv run --extra dev python scripts/generate_advice_fixture.py
uv run pytest tests/etude/test_advice.py -q
cd frontend && npm test -- --run advice
```

The generator produces the same two-landmark `StudyArtifact` at the same
`erd1` address twice byte-identically. Both landmarks name one advisor, compute
class, paired seed plan, action vocabulary, and producer. Selecting “Opponent
curving out” shows policy 43.75% / 56.25%; selecting “Opponent holding
interaction” shows 50% / 50%, with value and per-world uncertainty deltas.

## Approach

Keep the current fixture-first request and rendering path intact:

- `GET /api/advice` still returns one pinned address, two scenario summaries,
  and the identity callers must echo.
- `POST /api/advice` still accepts the same address/scenario/identity shape and
  fails closed on mismatch.
- `DecisionAdvice.svelte` remains the only live/Study surface.

Change only how `scripts/generate_advice_fixture.py` produces the checked
evidence:

1. Restore the existing canonical replay decision and fork its retained
   managym root.
2. Define four exact ten-card GW Allies hidden hands. Two are creature-dense;
   two are interaction-heavy and contain `Allies at Last`. Every world keeps
   the opponent hand count, viewer observation, and root action count exact.
3. Build one uniform conditional world prior and one query plan. Search all
   conditions with determinized PUCT using seed 197, 16 simulations per
   condition, at most four worlds, and a 200-step rollout cap.
4. Select the complementary `has:Allies at Last` and
   `not(has:Allies at Last)` results as the two displayed beliefs. Both have
   support two and use the same root, paired seed schedule, budget, and offers.
5. Project real visits to policy mass, q-values to expected match points,
   per-world q-values to favorable-world counts and per-action standard error,
   then content-address each evidence core.
6. Run the entire conditional search twice and refuse to write unless the two
   serialized results are byte-identical. Refuse to write if viewer facts or
   offers drift, policies do not differ, both policies are uniform, or producer
   identity differs.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Can conditional PUCT consume the existing Study root without a second replay or state path? | Yes. `StudyBranch._env` is the exact cloned managym decision root expected by `conditional_determinized_puct`. | Keep the existing DecisionAddress and action vocabulary. |
| Can hidden-hand hypotheses preserve the viewer surface? | Yes. Four ten-card worlds produce byte-identical `observation_for_player(0).toJSON()` and the same two actions as the root. | Generator rejects any world that changes viewer facts or offers. |
| Can both beliefs use the same seed schedule and compute class? | Yes. One conditional run searches the query and its complement with seed 197 and the same per-condition simulation budget. | Both scenario records carry `paired-seed-197`; both evidence rows share one producer. |
| Does a bounded budget yield an honest visible delta? | Yes. At 16 simulations, curve policy is 7/16 vs 9/16 and interaction policy is 8/16 vs 8/16. Values and per-world standard errors also differ. | Keep the measured bounded budget. Do not tune the seed to make both policies non-uniform. |
| Is the local extension missing newer semantic-observation bindings? | Yes, while its stable `observation_for_player(...).toJSON()` API is available. | Use the available complete viewer observation for the compatibility proof; no Rust rebuild or source change. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Keep the two disjoint flat-MC seed families | Smallest diff, but the delta can be explained by seed variance. | It does not support the UI’s conditional-strategy claim. |
| Connect the live conditional provider now | Would remove fixture latency and staleness. | GAM-7 explicitly owns live provider integration and its typed failures. |
| Import the standalone INT-13 fixture directly | Already conditional, but it belongs to another root with three different actions. | Evidence would not bind to this DecisionAddress or shared action vocabulary. |
| Increase simulations until both policies look non-uniform | Produces a visually stronger chart at higher regeneration cost. | It risks tuning evidence for presentation. The bounded paired result already proves a real delta. |

## Key decisions

- The conditioned exact worlds are generator-private. The checked fixture and
  API expose only viewer-safe belief descriptions and aggregate evidence.
- `conditional-determinized-puct-v1` is one fixture advisor identity; this does
  not claim equilibrium play, ISMCTS, strength, or production latency.
- `DecisionEvidence` uncertainty and robustness come from real per-world
  q-values. The old scalar broadcast and favorable-world proxy are removed.
- The old `seed_family` scenario metadata becomes `condition_id` plus one
  shared `seed_plan`; neither field changes the public advice request.
- One uniform conditioned policy is accepted because it is the measured result,
  not a failure. The two scenario policies must differ and at least one must be
  non-uniform.

## Scope

- In scope: conditional fixture generation at the existing decision, honest
  evidence mapping, fixed cross-scenario identity/provenance, regenerated
  fixture, focused Python/TypeScript/e2e expectations, and truthful docs copy.
- Out of scope: runtime search, provider/checkpoint loading, new belief inputs,
  UI redesign, new protocol schemas, alternate decisions, Retry/return changes,
  chat, range tooling, watcher roles, or Avatar content expansion.

## Done when

- The generator runs twice deterministically and writes a schema-valid,
  viewer-safe `StudyArtifact` at the existing `erd1` address.
- Both scenarios share advisor `conditional-determinized-puct-v1`, compute
  `2w-16s-paired-seed-197`, seed plan `paired-seed-197`, action ids, generated
  time, and producer `conditional-determinized-puct:v1:paired-seed-197`.
- Scenario policies and at least one value or uncertainty differ, and every
  displayed delta is antisymmetric.
- Python adapter/endpoint, frontend unit/check/build, Etude regression, and
  advice Playwright tests pass.

## Measure

This is an integrity result, not a strength target. The gate is binary:

- zero viewer-observation or action-vocabulary drift across all four worlds;
- zero seed-plan, advisor, compute, or producer drift across scenarios;
- non-zero conditional policy delta at one pinned decision;
- byte-identical regeneration across two complete runs.
