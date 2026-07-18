# Expose a Versioned Belief-Conditioned Strategy Advisor

## Problem

Etude Fantasia can currently show a static two-scenario advice fixture, and
manabot can separately maintain an exact viewer-safe `BeliefState` and search
conditioned fixture worlds. There is no honest provider contract joining those
pieces. The current `/api/advice` identity contains only replay, match, advisor,
and compute strings; its checked artifact contains placeholder engine and
checkpoint hashes; and INT-13's serialized result places sampled-world details
beside the viewer-safe layer. That is not a sufficient boundary for live or
Study advice.

INT-12 will establish the first versioned provider seam. A Game-owned caller
supplies one canonical recorded decision, the requesting viewer, one explicit
belief scenario, and one fully pinned advisor. manabot validates the decision
and belief against managym's authority, computes a complete distribution over
the authoritative semantic offers, and returns a closed viewer-safe result.
The same request can be resolved from a live session's retained replay root or
from Study without changing its bytes. A comparison request carries two belief
scenarios under one shared decision/viewer/advisor context and attributes every
delta to the two normalized belief identities.

This serves the player who wants to ask “how should the play change if my
belief changes?” and the Game developer who needs a stable evidence contract,
without making Etude a search engine or making manabot a replay authority.

## The demo

Run `uv run --extra dev experiments/runners/run_belief_strategy_advisor.py
--verify-fixture`. It regenerates one advice comparison from a canonical
viewer decision, prints the two scenario digests and a non-zero per-action
strategy delta, proves live and Study response bytes have the same SHA-256,
then reports cached and recomputed p50/p95 latency plus realized search cost.

## Approach

### 1. Evolve the existing Game and advice contracts in place

`etude.testing_house_protocol.ViewerIdentity` and
`etude.testing_house_protocol.BeliefScenario` remain the exact canonical Game
types and import locations. INT-12 does not define an advisor-owned scenario or
viewer type. If implementation extracts either definition to break an import
cycle, `testing_house_protocol` must re-export the same class object and all
existing imports and serialized player-authored payloads must remain valid.

Keep `BeliefScenario` as Game-owned metadata: id, author/selector, source
decision, audience, and provenance. Extend its existing provenance in place to
a discriminated union:

- `player_authored` preserves the current serialized fields and semantics;
- `model_inferred` adds belief-model id, exact checkpoint/artifact SHA-256,
  and viewer-history identity while retaining the scenario's Game-owned
  audience and lifecycle.

The scenario does not grow a 10,832-row range vector and does not become a
manabot artifact. Add a separately named `BeliefDistributionPayload` owned by
manabot and a narrow `BeliefDistributionResolver` protocol. Given the exact
canonical `BeliefScenario`, resolved decision/world space, and requesting
`ViewerIdentity`, the resolver returns `space_identity`, `belief_model_id`,
one finite non-negative weight per canonical row, distribution SHA-256, and
the provenance identity it resolved. Player-authored and model-inferred
resolver implementations may store their payloads differently, but both feed
the same `BeliefState.from_probabilities` normalization path.

Existing GAM-6 compatibility is explicit:

- the current player-authored `BeliefProvenance` wire shape continues to parse
  byte-for-byte under the union;
- `ViewerIdentity` and `BeliefScenario` keep their existing import names;
- `BeliefSource.advice_identity` accepts the existing four-string
  `AdviceRequestIdentity` during migration as well as the versioned
  `AdvisorIdentity`;
- checked GAM-6 fixtures are regenerated to the versioned request/identity in
  this PR, while their old payloads remain compatibility fixtures for the
  legacy adapter;
- a legacy payload is never guessed into a full identity: it upgrades only
  through an exact checked manifest mapping, otherwise `/api/advice` returns
  typed `legacy_identity_incomplete` with no evidence.

Version and evolve the existing `etude.advice` seam rather than creating an
`advisor_protocol` family or a second endpoint. `AdviceRequestIdentity` remains
the import-compatible legacy four-string model. New `AdvisorIdentity` is its
pinned superset. Existing `AdviceRequest`, `AdviceResponse`, `AdviceMeta`,
`request_advice`, and shared `GET/POST /api/advice` become the sole versioned
provider surface.

The versioned `AdviceRequest` contains one parsed `ReplayDecisionAddress`, the
exact canonical `ViewerIdentity`, the exact canonical `BeliefScenario`, one
`AdvisorIdentity`, and an optional second `BeliefScenario` for comparison.
Holding decision/viewer/advisor once in the request makes comparison atomic;
the two arms cannot silently change world, compute, or seed identity. The
legacy request shape (`address`, `scenario_id`, four-string identity) is parsed
only by an explicit adapter that resolves the canonical fixture scenario and
full identity or returns the typed-unavailable response through the same
serializer.

`ReplayDecisionAddress` is DecisionAddress v1. It is parsed at model
construction, then checked against the resolved replay row, viewer, revision,
prompt, selected command, presentation cursor, and decision payload digest.
The first version advises only already committed replay decisions, including a
decision retained by a still-live session. It does not invent an address for an
unplayed prompt.

The distribution resolver constructs
`manabot.belief.BeliefState.from_probabilities` over the resolved
`PossibleWorldSpace`. This is the only normalization path. It
rejects the wrong number of rows, negative or non-finite weights, zero total
mass, stale space identity, a provenance/viewer mismatch, or a digest mismatch.
The public result contains a normalization receipt—canonical space identity,
normalized belief digest, positive support, and normalization error—but never
echoes the weight vector or world rows.

`AdvisorIdentity` is closed rather than a bag of labels. It binds:

- world and content manifest/digest;
- semantic Observation, action/DecisionFrame, and possible-world ABI versions
  and hashes;
- information boundary;
- planner and leaf-evaluator identities;
- a discriminated code-source or exact-checkpoint artifact identity;
- compute class and all effective parameters;
- seed-plan id, root seed, and derivation id.

The initial registered advisor is code-only determinized PUCT over the
canonical exact range, with the current random terminal evaluator. A
checkpoint-backed identity is valid wire input, but missing bytes or an
incompatible manifest returns typed unavailable; it is never substituted with
a fixture, untrained model, or nearby checkpoint.

The v1 result is a discriminated `ok | unavailable` envelope. `ok` contains:

- the exact request and normalized belief identities;
- the decision fingerprint and the complete authoritative semantic offer set;
- a typed policy semantic (`puct_visit_distribution/v1`), probabilities, and
  visits for every offer in authority order;
- per-action Q, root value, robustness, and uncertainty as explicit
  `available | unavailable` quantities rather than nullable values or invented
  zeros;
- realized simulations, materialized-world count, tree nodes, cap hits, and
  declared method ids;
- exact source/checkpoint and seed provenance.

`unavailable` contains a closed reason enum, the request identity, and safe
compatibility details. It never includes exception text, filesystem paths,
sampled worlds, actual-query truth, or partial policy evidence. Required policy
mass being unavailable closes the entire response; optional value, robustness,
or uncertainty can be individually unavailable with a typed reason.

### 2. Adapt INT-13's existing result seam to INT-9's canonical belief

`manabot.sim.conditional_search.ConditionalStrategyResult` remains the sole
search-result meaning. INT-12 does not introduce another strategy result,
action vocabulary, or condition semantics. Adapt the existing
`ConditionalWorldPrior`/world materialization input so a named condition can
be backed directly by an INT-9 `BeliefState` over managym's canonical
`PossibleWorldSpace`. The conditional search entry point continues to return
the existing `ConditionalStrategyResult` and `ConditionResult` values.

The adapted existing search seam accepts:

- one authoritative root;
- one or two already normalized `BeliefState` values over the root's exact
  `PossibleWorldSpace`;
- one shared planner/evaluator/compute/seed identity.

Refactor world realization behind a narrow internal protocol. The canonical
implementation calls `PossibleWorldSpace.materialize(index, seed=...)`; the
existing INT-13 fixture adapter can retain its scenario implementation. Tree
steps continue to exact-fork and apply managym semantic Commands through the
selected branch backend. No code outside managym constructs a hidden hand or
changes legality.

World sampling uses paired uniform draws derived once from the request seed
plan and inverse-CDF sampling against each scenario's normalized range. Thus
the random source is paired while each scenario honestly selects worlds under
its own distribution. Sampling identities and realized counts are public;
indexes, hands, per-world states, and RNG tapes remain internal audit values.

Keep INT-13's audit-private per-world values, selected indexes, RNG/branch
receipts, and existing `serialize_result` audit payload internally. Add or
tighten a viewer-safe projector alongside `ConditionalStrategyResult`; the
advisor consumes only that projector, never the `authority_private` sibling.
The projector preserves the existing condition policy/Q/root-value/uncertainty
meaning and attaches it to the already-authoritative semantic offers from the
resolved DecisionFrame. It does not define a new action identity: it validates
that INT-13's aligned slots cover the DecisionFrame's offers in authority order
and uses the existing advice/Study alternative binding.

The viewer-safe projection must not reuse INT-13's internal default `0.5` for
an unvisited action as evidence. A Q value is real and available only when the
action has visits. Root value is available only from realized simulations.
Between-world standard error is real and available only with at least two
covered worlds; otherwise uncertainty is explicitly typed unavailable.
Robustness reports co-best coverage only over worlds where the action was
evaluated. These availability rules refine the transport projection without
changing `ConditionalStrategyResult`'s search meaning.

### 3. Keep Game and managym authority explicit

Define an internal `ResolvedAdvisorDecision` containing the validated replay
row, viewer-safe frame, authoritative semantic DecisionFrame, canonical world
space, and an opaque retained managym root. It is not serializable.

INT-12 defines only the narrow `AdvisorDecisionResolver` capability consumed by
the provider. Existing Game code supplies that capability from its canonical
replay/retained-root lifecycle, and the existing Study path supplies it through
`ReplayDecisionAddress` plus `StudyForkProvider`. Both yield isolated root
clones after Game checks the same source digest and viewer. INT-12 does not add
root storage or replay lookup authority. manabot receives the resolved context
by dependency injection and never reads `GameSession._study_roots`,
reconstructs a replay, or looks up a table participant itself.

For this PR, the checked fixture resolver and the two adapter paths are enough
to prove the seam end to end. Arbitrary persisted recordings without retained
authority roots remain typed `decision_root_unavailable`. No nearby root or
static evidence is substituted.

### 4. Deterministic public bytes, separate operational evidence

Add `advisor-canonical-json-v1`: UTF-8, lexicographically sorted object keys,
compact separators, finite JSON numbers only, and no generated timestamp.
This follows the invariant-representation purpose of
[RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html) while naming the
project's exact serializer rather than claiming full cross-language JCS
compliance. Hashes are over the canonical request and response cores. The
response digest field is computed over the response core that excludes the
digest itself.

Route kind, wall-clock timing, cache status, process id, and generation time
are deliberately absent from the response. Live and Study adapters return the
provider's canonical bytes directly rather than asking FastAPI to re-encode a
dict. Domain failures remain a deterministic `unavailable` envelope; malformed
wire input remains normal transport validation.

Version the existing advice schema as `protocol/advice-v1.schema.json` and add
`protocol/fixtures/advice-belief-conditioned-v1.json`. The fixture
contains one comparison request and its public response only. A separate
measurement receipt under `experiments/data/` may retain hardware, timestamps,
latencies, and aggregate cost, but no sampled hands or actual hidden-query
truth. The fixture generator writes only with an explicit maintenance flag;
normal verification regenerates in memory and fails on any byte difference.

### 5. Prove information-boundary and compatibility failures

The contract tests create two managym authorities with different private
opponent truth but byte-identical viewer Observation/DecisionFrame projections.
Given the same explicit belief, advisor, compute, and seed identities, both
must produce byte-identical public responses. A closed response schema and
typed projection make sampled-world values structurally unrepresentable;
additional recursive forbidden-field checks guard fixture regressions.

Compatibility tests independently perturb every identity domain: decision
digest, viewer seat/table, world, content, Observation ABI, action ABI,
possible-world space, source/checkpoint bytes, compute parameters, seed plan,
and belief distribution. Every perturbation must yield the expected typed
unavailable code with no evidence.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Is there already a canonical decision address? | `ReplayDecisionAddress` (`erd1`) binds replay, match, ordinal, viewer, revision, prompt, selected offer/command, presentation cursor, and decision digest. Canonical replay intentionally does not address an unplayed prompt. | Reuse and parse `erd1`; v1 advises committed live-session or historical decisions only. Do not create another address grammar. |
| Can Intelligence safely resolve an arbitrary Study root today? | `GameSession` retains exact roots privately and `StudyForkProvider` validates/forks them, but `HistoricalStudyEvidenceRequest` carries no root handle. Prior work identified direct `_study_roots` access as an authority violation. | Add a narrow Game-owned resolver that injects an opaque isolated root. Missing retained authority is typed unavailable, never reconstructed. |
| Is the exact belief substrate ready? | INT-9's `BeliefState` binds one managym `PossibleWorldSpace`, validates normalization, has a deterministic digest, and samples by seed. A live probe on seed 197 produced 10,832 worlds with normalization error `1.33e-15`. | Use `BeliefState.from_probabilities` unchanged. The advisor owns no hand enumeration or normalization implementation. |
| Can the provider safely accept query strings and condition them itself? | managym's Python `WorldQuery.support` returns authoritative aggregate mass/support and a digest, but not the member indexes needed to reweight an arbitrary belief. Evaluating query membership again in manabot would duplicate Rules semantics. | The canonical Game `BeliefScenario` remains metadata; an injected `BeliefDistributionResolver` supplies exact authored/model-inferred weights. Query text/digests may be provenance, never a strategy feature or local predicate. |
| Are learned likelihood/model bytes available? | INT-9 remains `evidence_wait` because the exact frozen likelihood checkpoint is not retained. Hashes are not byte artifacts. | Ship a code-only search advisor and the full checkpoint identity/unavailable path. Do not block the provider or substitute a checkpoint. |
| Can INT-13's checked fixture be reused? | The focused suite passed 51 tests, but its deterministic fixture test now fails because the checked engine source SHA (`6acc…`) differs from current main (`5120…`). The fixture's scenario world adapter also predates the canonical INT-9 world path. | Generate a new INT-12 fixture from current canonical authority. Treat the old fixture as regression input only, not provider evidence. |
| Is INT-13's serialized result player-safe as a whole? | It contains an `authority_private` sibling with per-world Q/root values and branch receipts. The existing Etude adapter relies on a static fixture and four-string identity. | Preserve the full audit serialization internally and make the evolved advice seam consume only INT-13's viewer-safe projection. |
| Are durable actions already semantic? | managym exposes a revision-bound DecisionFrame fingerprint and semantic offers, while INT-13 aligns result arrays to root slots. | Validate every aligned slot against the existing authoritative DecisionFrame/advice/Study alternative binding; do not introduce a second action identity. |
| Are the product request types already present? | `testing_house_protocol.py` already defines `ViewerIdentity`, `BeliefSource`, `BeliefProvenance`, and `BeliefScenario`; `etude.advice` already defines the shared request/response and `/api/advice` seam. | Evolve those exact types in place, preserve imports and legacy parsing, and reject any parallel advisor protocol or endpoint family. |
| Does the current uncertainty mapping distinguish missing evidence? | INT-13 fills unvisited Q entries with `0.5`, while Study's current schema requires every quantity. That can turn missing samples into apparent evidence. | Add typed per-quantity availability and never broadcast/fill unavailable values. |
| Can deterministic bytes include runtime metadata? | Current Study evidence includes `generated_at`; timings and cache state necessarily vary. RFC 8785 exists because hashing requires invariant representation. | Exclude time and operational measurements from public response identity; return one project-versioned canonical encoding from both paths. |
| Is recomputation plausibly interactive? | A direct canonical INT-9 probe at 4 sampled worlds × 1 rollout (20 action playouts total) completed in 17.78 ms on the current Apple M4 Max, with three 200-step cap hits. This is engineering evidence, not a service SLO. | Measure a pinned advisor profile rather than promising latency. Report cap hits and realized compute beside p50/p95; keep cache-hit and fresh recomputation distributions separate. |
| How do typed failures cross HTTP? | The current endpoint already returns domain `unavailable` as a successful typed body rather than leaking exceptions. [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) is useful for transport failures but does not replace domain availability. | Preserve a discriminated domain envelope. Use normal HTTP validation/problem responses only for malformed transport. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Extend the current static `/api/advice` fixture and four-string identity | Smallest diff and already rendered by Game. | It cannot bind world/ABI/belief/checkpoint/seed identity, has placeholder artifact hashes, does not normalize a canonical range, and cannot recompute honestly. |
| Return `conditional_search.serialize_result` directly | Preserves all INT-13 diagnostics with almost no adapter code. | It deliberately co-locates sampled-world audit material with the viewer layer and uses positional action labels. One serializer mistake would cross the privacy boundary. |
| Accept managym `WorldQuery` and evaluate membership in Python | Gives compact player requests such as `Has(Bolt)`. | The current authority API does not return member indexes. Reimplementing membership would create the parallel hand/query semantics this task forbids. A later Rules-owned membership projection can be consumed without changing the advisor result. |
| Let manabot resolve replays and retained roots itself | Makes the provider look self-contained. | It takes Game authority, reaches private root storage, and risks live/Study selecting different facts. Root resolution must remain injected by Game. |
| Build only a generic contract and postpone a runnable advisor | Avoids choosing a current search implementation. | It would not advance the wave requirement that every primary Project produce a runnable agent/search path, nor expose the real cost and missing-value behavior. |
| Add a new `advisor_protocol` request/result family beside `/api/advice` | Allows a clean-slate schema with fewer migrations. | It would make two product contracts authoritative for the same decision. INT-12 must version and bridge the existing advice seam instead. |

## Key decisions

1. **The v1 proof is one real, complete vertical slice.** It includes strict
   request/result types, canonical exact-range search, Game-owned root adapters,
   a checked public fixture/schema, typed failure coverage, privacy invariance,
   and cost evidence in one PR.
2. **Canonical belief scenarios remain Game-owned metadata, not
   distributions.** The exact existing `BeliefScenario` carries
   authorship/model provenance, audience, and source. A separately named
   `BeliefDistributionPayload` supplies the canonical weights to manabot
   through `BeliefDistributionResolver`; manabot owns normalization and
   planning, while managym owns the rows.
3. **Comparison is atomic.** One comparison request holds context once and
   computes both scenarios with paired seed material. Deltas are explicitly
   `left_minus_right` and bind both normalized belief digests.
4. **The initial advisor is honest about its method.** It is determinized PUCT
   with random terminal leaves, not ISMCTS, public-belief solving, an
   equilibrium, or a strength claim.
5. **Unavailable is data.** Missing checkpoint bytes, stale authority, empty or
   malformed belief support, ABI drift, and missing statistical coverage are
   named states. No fallback changes the method.
6. **INT-13 has one result and two projections.** `ConditionalStrategyResult`
   remains the search result; its audit projection stays internal and the
   evolved advice seam consumes only its viewer-safe projection. There is no
   flag that can expose private worlds in a player response.
7. **The response is timeless.** Exact identities and realized compute belong
   in it; timestamps, hardware, latency, and cache state belong in a separate
   measurement receipt.

Wild success looks like Game consuming the checked schema without a custom
adapter, then later swapping the fixture resolver for a retained live root and
getting the same bytes. Players compare authored and model-inferred ranges,
and every displayed delta can be traced to exact belief/advisor identities.

Wild failure is a nominally deterministic API whose evidence changes with
route, cache, engine truth, or missing artifacts; or a convenient serializer
that leaks sampled hands. The closed public model, injected authority resolver,
paired comparison, identity perturbation matrix, and private-truth invariance
test are the removal criteria for those failure modes.

## Scope

- In scope: versioning and bridging the existing `etude.advice` request,
  identity, response, metadata, and `/api/advice` seam; exact reuse of the
  existing `ViewerIdentity` and `BeliefScenario`; discriminated authored/model
  provenance plus a separately named distribution resolver; canonical
  DecisionAddress reuse; INT-9 `BeliefState` normalization and world
  materialization; the existing INT-13 `ConditionalStrategyResult` and its
  viewer-safe projection; authoritative semantic-offer coverage; typed
  value/uncertainty availability; atomic scenario comparison and attributable
  deltas; code/checkpoint artifact resolution; injected Game live/Study root
  capability; deterministic canonical bytes; one checked Game-consumable
  fixture; privacy/compatibility tests; p50/p95 and recomputation-cost evidence.
- Out of scope: UI, chat, narration, natural-language query construction,
  reveal/causal claims, current-unplayed-prompt addresses, arbitrary recording
  reconstruction, new Rules query semantics, new hidden-hand/action ontology,
  checkpoint training or recovery, arena admission, rating, strength claims,
  ISMCTS/CFR/public-belief solving, and changes to match legality or replay
  authority.

## Done when

- `uv run --extra dev experiments/runners/run_belief_strategy_advisor.py
  --verify-fixture` regenerates the canonical request/response, matches the
  checked fixture byte for byte, observes a non-zero scenario delta, and emits
  a measurement receipt with cached and recomputed p50/p95 plus realized cost.
- The provider returns every legal semantic offer exactly once; probabilities
  are finite, non-negative, and sum to one; the exact offer-id set and order
  equal the authoritative DecisionFrame; all evidence rows bind that same
  frame and AdvisorIdentity, with no advisor-owned action ids.
- Per-action Q, root value, robustness, and uncertainty are populated only
  from statistically supported realized evidence under their registered
  availability rules. Every unsupported quantity carries the registered
  typed-unavailable quantity state; no quantity is filled with `0.5`, zero,
  an aggregate broadcast, or fixture data.
- The live-session and Study adapters pass the same versioned `AdviceRequest`
  to the same provider and the same canonical response serializer; the exact
  returned bytes and SHA-256 are identical for both paths.
- Existing GAM-6 `ViewerIdentity`, `BeliefScenario`, and player-authored
  provenance fixtures still parse at their established imports. The migrated
  fixture succeeds under `AdvisorIdentity`; an unmapped legacy four-string
  identity returns `legacy_identity_incomplete` with no evidence through the
  same `AdviceResponse` serializer.
- Two viewer-equivalent authority roots with different opponent-private truth
  produce identical response bytes; the public schema cannot represent sampled
  worlds, hands, RNG tapes, or actual-query truth.
- Each identity/artifact/range perturbation returns its registered typed
  unavailable reason with no policy evidence.
- Focused Python tests pass through `uv run --extra dev pytest ...`; if the
  implementation changes Rust, debug `cargo test` passes before landing.

This directly advances the wave measures that “the same identity is
reproducible through live play and Study,” that advice is pinned to viewer-safe
belief/advisor/compute/seed/evidence bytes, and that missing or mismatched
artifacts return typed unavailability. It also advances the Project KR for one
canonical historical decision with byte-identical attributable baseline and
conditional evidence, without exposing sampled worlds or actual-query truth.

## Measure

The runner records two distributions after warmup on a pinned root and advisor
profile:

- **cached provider latency:** validate request, verify fixture/artifact digest,
  and return canonical bytes;
- **fresh recomputation latency:** resolve/validate root, normalize the range,
  materialize worlds, search both scenarios, project, validate, and serialize.

Use at least 20 warmups and 128 measured calls per path. Report p50/p95 wall
latency, response bytes, source/checkpoint verification time, normalization
time, materialization/search time, simulations, sampled and unique worlds,
tree nodes, cap hits, transitions if available, peak RSS, and the recomputation
to cache-hit ratio. Pin hardware/runtime fingerprints in the measurement
receipt. These numbers characterize the declared engineering profile; they are
not an admission, strength, or general service-SLO claim.
