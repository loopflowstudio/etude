# Build the first exact-range belief-aware player

## Problem

Uniform determinization discards information in an opponent's public actions.
INT-9 must establish whether a likelihood-weighted belief improves a runnable
manabot in one real matchup, not merely produce an attractive posterior plot.

The repository now has the architectural boundary this experiment must use:
managym owns viewer-relative `PossibleWorldSpace`, `WorldQuery`, canonical
viewer Observation/history, deterministic world materialization, semantic
`DecisionFrame`/`Command`, and legality. manabot may assign probability over
that domain, but it must not define a second hand key, hidden pool, query
language, public-action enum, or exact-hand installation path. The existing
INT-9 substrate predates that boundary and is therefore migration input, not
the contract to preserve.

The first executable comparison stays deliberately narrow: symmetric world-w2
`INTERACTIVE_DECK`, a fixed viewer, the exact opponent-hand worlds already
enumerated by managym, likelihood updates only for canonical viewer-observable
semantic commitments supported at this boundary, and matched flat search. It
does not attempt public-belief solving, broad deck generalization, or a second
action representation.

## The demo

Once the required frozen likelihood checkpoint is available and hash-pinned,
run:

```bash
uv run experiments/runners/run_exact_range_player.py \
  --contract experiments/contracts/int-9-exact-range-v1.json \
  --stage smoke --out-dir .runs/int-9-exact-range-v1
```

The command plays belief and compatible-deal-prior arms through normal managym
semantic Commands, then writes a replayable receipt binding every posterior to
one `PossibleWorldSpace` identity and canonical viewer history. It reports
normalization, legality, viewer safety, sampled-world compatibility,
calibration, paired results, latency, throughput, and memory. Until the frozen
checkpoint exists, the same command fails with a typed evidence-wait receipt;
unit tests may use an explicitly test-only likelihood but may not promote it to
smoke or arena evidence.

## User-visible outcome and end-to-end proof

The behavior change is visible to the developer or evaluator running INT-9:
the manabot listens only to the selected viewer's canonical public history,
maintains a replay-identical exact posterior over managym worlds, samples those
worlds for search, and submits a legal semantic `Command`. The paired control
does the same work but samples the current compatible-deal prior. A successful
receipt therefore makes the gameplay difference, posterior explanation, cost,
and complete authority provenance inspectable without revealing the actual
opponent hand.

The concrete proof scenario is the first supported opponent commitment reached
from a fixed-viewer priority root in the symmetric world-w2
`INTERACTIVE_DECK`:

1. managym emits the viewer's canonical `Observation` and current history
   increment, then constructs the revision-bound `PossibleWorldSpace`;
2. manabot initializes `BeliefState` from that space's exact compatible-deal
   weights and records the ordered space and belief identities;
3. the opponent submits a semantic `Command`, and managym returns the accepted
   transition plus the viewer-visible commitment/history identity;
4. the likelihood evaluator materializes canonical world indexes from the
   retained root, evaluates only the provider-supplied semantic commitment,
   and produces the next normalized belief;
5. belief and compatible-prior arms sample paired canonical world indexes and
   call the same flat-search path with the same world, rollout, and seed
   budgets;
6. the selected semantic offer becomes a revision-bound `Command` that
   managym validates and executes; and
7. the public replay reconstructs every identity while the separate audit
   scores the actual canonical world only after the decision.

`uv run pytest tests/belief tests/sim/test_exact_range_runner.py` proves this
path with an explicitly test-only likelihood. With byte-locked artifacts, the
smoke command above is the end-to-end evidence proof and must produce a
verified receipt for both arms. If managym cannot provide the selected
commitment identity, the observable result is instead a typed Rules provider
gap; if the frozen checkpoint is absent, it is `evidence_wait`. Neither result
may be reported as gameplay evidence.

## Source of truth and affected surfaces

The authoritative sources are the existing managym contracts, not the INT-9
Python substrate:

- `managym/src/possible_worlds.rs` owns `PossibleWorldSpace`, ordered canonical
  worlds, exact physical-deal weights, `WorldQuery`, conditioning receipts, and
  materialization;
- `managym/src/decision.rs` owns viewer `Observation`, `DecisionFrame`,
  semantic `Command`, `TransitionReceipt`, and legality;
- `managym/src/python/bindings.rs` and the pure parsing wrappers under
  `managym/` are derived consumer surfaces and may expose those Rust values but
  may not redefine them; and
- the ordered canonical Observation/transition bytes are the history facts.
  manabot may retain them as agent memory and bind the exact sequence in its
  receipt, but it may not infer or rewrite missing history.

The current Rust `PossibleWorldSpace` is not yet exposed to Python. Pursuit
must add one thin, read-only PyO3/Python surface for the existing canonical
space: its versioned source/space identity, ordered world rows and exact
weights, query receipts, and source-bound materialization by canonical world
index. This binding is the only allowed cross-language adapter. Enumeration,
copy multiplicity, query evaluation, identity serialization, and world
installation stay in managym; there is no Python fallback. Search-root
preservation and acting-opponent legality refresh are explicit modes of that
one canonical materializer, not calls to `determinize_to_hand`.

manabot derives only probability and experiment behavior:

- `manabot/belief/range.py` and its exported `ExactHandRange`/`HandKey` contract
  are replaced by a bounded `BeliefState` over the bound world's ordered rows;
- `manabot/belief/tracker.py`, `likelihood.py`, `player.py`, and `audit.py`
  consume canonical spaces, Observations/transitions, commitment identities,
  and world indexes; the detached `HiddenPoolSnapshot`, Python `PublicAction`,
  raw-prompt grouping, and direct-hand installation paths are removed;
- `manabot/sim/flat_mc.py` keeps the belief/prior player registrations on one
  factory and search path, but their durable identity is the semantic Command
  path rather than positional actions;
- `experiments/runners/run_exact_range_player.py` remains the one CLI and
  receipt writer; `experiments/contracts/int-9-exact-range-v1.json` must be
  schema-bumped before it produces evidence, replacing the stale
  `public_action_alphabet` and direct-hand determinization identities with the
  canonical world-space, semantic-history, BeliefState, and materializer
  identities; and
- `tests/belief/`, `tests/sim/test_exact_range_runner.py`, and managym's Rust
  possible-world/semantic-decision tests are the affected verification
  consumers. They are rewritten around canonical rows and identities rather
  than preserved as compatibility tests for the superseded ontology.

Etude, Study, the existing semantic Command wire DTO, unrelated manabot player
kinds, and frozen evidence remain unchanged consumers. INT-9 adds no Etude
executor or presentation surface and does not reinterpret an older receipt as
evidence under the new contract schema.

## Approach

### 1. Freeze one bounded comparison and one honest wait state

`experiments/contracts/int-9-exact-range-v1.json` pins world w2, the symmetric
deck, content and semantic contract identities, epsilon, world and rollout
budgets, paired deals, caps, evidence paths, and the required frozen artifacts.
The primary pair is:

- `belief`: samples the normalized likelihood-weighted `BeliefState`;
- `compatible-prior`: runs the same observation, likelihood, sampling, search,
  seeds, and Command path, but samples managym's normalized
  compatible-physical-deal measure for the current space.

Both arms perform the same likelihood work so sampling distribution is the
only intended algorithmic difference. The control intentionally forgets prior
action evidence when selecting worlds, while the diagnostic belief continues
to be computed for matched end-to-end cost.

The real world-w2 policy/value checkpoint and arena opponents are not present
in the repository. Their absence is an explicit `evidence_wait`, not a license
to reconstruct, retrain, or substitute an artifact. Implementation and fixture
verification may proceed; smoke and arena claims may not.

### 2. Express exact range as a bounded manabot `BeliefState`

Replace `ExactHandRange`/`HandKey` as the public probability contract with a
bounded adapter whose rows are exactly the ordered worlds supplied by one
managym `PossibleWorldSpace`:

```python
@dataclass(frozen=True)
class BeliefState:
    space: managym.PossibleWorldSpace
    model_id: str
    log_probabilities: NDArray[np.float64]
```

The adapter stores no independent card-definition order or hidden-pool schema.
It validates that the probability vector has one finite, normalized value per
canonical world and binds its digest to the managym space identity, model
identity, and float64 log probabilities. It provides only probability-layer
operations: normalization, likelihood conditioning, sampling canonical world
indexes, calibration, effective range size, and memory accounting.

The compatible prior is built by normalizing the exact physical-deal weights
already attached to managym worlds. No Python recomputation of bounded
compositions or copy multiplicity is authoritative. Empty support, identity
mismatch, non-finite mass, and silent pruning are hard failures.

When public history advances, managym constructs the next canonical space from
the next canonical viewer Observation/history. The adapter transports belief
mass only by joining old and new canonical world rows through their managym
semantic hand-count projections:

- hidden draw of name `c`:
  `P(h + e_c) += P(h) * (pool[c] - h[c]) / library_size`;
- publicly known card leaving hand: condition on the canonical world containing
  it, then join to the next-space world with one fewer copy;
- publicly known card returning to hand: join to the next-space world with one
  additional copy;
- canonical semantic action: multiply each row by the frozen policy likelihood
  plus the pinned legal-action epsilon mixture.

These formulas live in manabot because they are probability updates. The
hypotheses, card-name semantics, compatible measure, support ordering, and
world materialization remain managym-owned.

### 3. Consume canonical Observation/history and semantic Commands

The tracker consumes the fixed viewer's canonical managym Observation/history
increment and the accepted semantic `Command`/transition receipt. It does not
infer history from positional action indices, raw prompt counts, labels, or
ad-hoc before/after snapshots.

At one opponent commitment, the belief tracker retains the pre-commitment
root, waits until managym publishes the viewer-visible history increment, and
then records the canonical semantic commitment identity from the authoritative
DecisionFrame/Command path. The likelihood evaluator sums mass only across
authoritative offers whose managym-provided viewer-visible semantic identity
matches that commitment. INT-9 never groups or reconstructs positional actions
or private prompt paths. Revision-local offer IDs and physical object IDs never
become Bayesian evidence.

INT-9 admits only the selected-matchup semantic commitments for which managym
can supply a complete viewer-observable identity. If a commitment cannot be
expressed through the canonical authority, the run fails closed and reports a
typed Rules provider gap. INT-9 will not fill that gap with a Python
`PublicAction` enum, an Etude command envelope, or a legacy prompt transcript.

The replay receipt contains Observation/history identities, space identities,
semantic Command/transition identities, belief digests, and transition counts.
The actual hidden hand remains available only to the separate authority audit
used after decisions for calibration.

### 4. Evaluate likelihood over canonical worlds

`FrozenPolicyLikelihood` loads only the hash-pinned world-w2 checkpoint. At
each admitted opponent commitment it:

1. verifies that the retained root's `PossibleWorldSpace` identity equals the
   `BeliefState` space identity;
2. asks that space to materialize each supported canonical world into an
   isolated branch;
3. obtains the hypothetical opponent's normal model Observation and canonical
   semantic decision frame;
4. batches the existing encoder and frozen policy forward pass;
5. sums probability across authoritative offers matching the observed
   canonical semantic commitment; and
6. returns one likelihood and canonical legal-action count per belief row.

The evaluator never accepts the authority's true hand and never calls a
parallel `determinize_to_hand` API. Search and likelihood both materialize
worlds through `PossibleWorldSpace`; likelihood materialization refreshes the
acting opponent's authoritative priority legality, while search materialization
preserves the fixed viewer's root decision. Unsupported hand-dependent prompt
kinds fail closed.

Physical copies are already quotiented by the canonical space. Admission still
tests that distinct physical materializations of one canonical world yield the
same grouped likelihood and that viewer-equivalent roots yield the same belief
update. A failure invalidates the bounded model rather than expanding its
ontology.

### 5. Search one shared materialization path

The exact-range player samples canonical world indexes from either the
posterior or compatible prior and passes those indexes, their space identity,
and paired seeds to one native flat-MC entry point. The engine materializes each
world through the retained `PossibleWorldSpace`; every root action shares the
same world and rollout streams. Both arms use identical budgets and tie rules.

The player selects from the current canonical `DecisionFrame` and emits a
revision-bound semantic `Command`. managym validates and executes the Command;
the belief layer never mutates authority or claims legality. Positional action
indices may remain an internal accelerator only where managym binds them to the
semantic frame; they are absent from durable evidence and likelihood identity.

### 6. Keep evidence proportional to the first comparison

Known-truth replay scores the posterior without feeding truth back to the
player:

- exact canonical-world negative log likelihood and rank;
- per-card inclusion Brier score and ECE;
- top-world mass and effective range size;
- sampled unique-world count and collision rate; and
- space/belief identity continuity across every update.

The bounded smoke compares belief versus compatible prior through normal play
and reports integrity plus mechanism evidence. The preregistered arena remains
the admission claim: paired belief-minus-prior play, population rating,
uncertainty, competencies, raw playouts, and systems cost. Posterior quality
explains gameplay; it does not substitute for gameplay.

No extra ablation, microgame, public-belief solver, new Project, or second PR is
started by kickoff. If the registered result is ambiguous, the existing
contract may emit a follow-up preregistration, but it does not execute new
backlog work.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Is there already an authoritative hidden-world domain? | Current main provides viewer-relative `PossibleWorldSpace`, exact compatible-deal weights, `WorldQuery`, and deterministic materialization. | Delete the detached hand-range/materializer contract; bind `BeliefState` rows to canonical worlds. |
| Is a separate Python action alphabet safe? | No. Offer IDs are revision-local, raw prompt structure may be private, and semantic Command authority now lives in managym. | Use canonical DecisionFrame/Command/history projections; unsupported public identity is a provider gap, not a `PublicAction` enum. |
| Is the opening range tractable? | The selected symmetric deck has 10,832 canonical seven-card worlds; growth remains measurable but exact at this boundary. | Keep the adapter bounded and unpruned; record support and memory at every update. |
| Can likelihood and search share materialization? | Yes. Both need the same canonical world; only whether the installed opponent currently owns priority changes legality refresh behavior. | Use one `PossibleWorldSpace` materialization contract with explicit search-root preservation and likelihood-root refresh tests. |
| Can the tracker use true hidden state accidentally? | Canonical viewer Observation/history hides the opponent hand; the actual world is needed only for post-hoc scoring. | Keep truth access in the audit module and prove the acting path has no authority-hand parameter. |
| Are frozen model artifacts available? | A repository-wide filesystem search found no admissible checkpoint; the contract already marks required artifacts unresolved. | Record `evidence_wait`, fail closed, and prohibit convenient substitutes. |
| Does better belief eliminate strategy fusion? | No. This remains determinization search and may still be information-set inconsistent. | Limit the claim to whether likelihood-weighted state inference improves matched play. |
| What keeps the control causal? | Same likelihood work, materializer, worlds/rollouts, seeds, command path, and timing surfaces isolate the sampling distribution. | Compare posterior sampling with current-space compatible-prior sampling only. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Preserve `ExactHandRange`, `HandKey`, and `determinize_to_hand` behind adapters | Smallest code diff, but creates two hidden-world identities and two materialization contracts. | It violates the accepted Rules/Intelligence boundary and makes later conditional search unable to share beliefs safely. |
| Keep a Python `PublicAction` alphabet over legacy prompts | Easy likelihood grouping for the current prototype. | It duplicates semantic Command authority and risks making private prompt structure durable evidence. |
| Particle filter over physical cards | Bounded memory and simple online updates. | It abandons exact normalization at a tractable boundary and models distinctions managym intentionally quotients away. |
| Marginal per-card probabilities | Cheap and compact. | It cannot represent mutually exclusive canonical worlds or materialize an exact correlated opponent hand. |
| Public-belief MCTS/CFR now | Could address strategy fusion. | It changes planner and inference simultaneously, broadens scope, and destroys the first causal comparison. |
| Reconstruct or retrain the missing checkpoint | Would unblock a runnable smoke quickly. | It is not the frozen opponent model named by the contract and cannot produce admissible evidence. |

## Key decisions

1. **managym owns facts and worlds.** `PossibleWorldSpace`, `WorldQuery`,
   Observation/history, materialization, DecisionFrame, Command, and legality
   are canonical and are not mirrored in INT-9.
2. **manabot owns probability only.** `BeliefState` is a normalized vector over
   one canonical space, with a model identity and no independent hand schema.
3. **The comparison stays symmetric w2 and flat-search bounded.** This is the
   smallest end-to-end test of whether action likelihoods improve play.
4. **Public evidence is semantic and viewer-safe.** No raw prompts, labels,
   positional offers, or physical private identities enter the belief update.
5. **One materializer serves likelihood and search.** Root-action preservation
   and acting-opponent legality refresh are explicit modes of canonical
   materialization, not separate determinization APIs.
6. **Unavailable checkpoints create an evidence wait.** Test fixtures can prove
   mechanics; they cannot satisfy smoke, arena, or admission gates.
7. **Strength remains primary.** Calibration and effective range size explain
   a paired gameplay result; they do not become the result.
8. **Deferred lifecycle review belongs to the Belief-Aware Play Project
   Session.** Kickoff and gate do not wait on or assign the absent user.

### Wild success

The belief player gains against frozen learned/search opponents at matched
compute, especially in held-interaction competencies, while its true canonical
world log loss improves as public actions accumulate. A developer can replay
one decision from canonical history through the same space, sampled world,
semantic Command, and receipt without translation layers.

### Wild failure

The frozen policy is a mismatched opponent model, or exact support expansion
makes likelihood inference dominate end-to-end cost. The posterior becomes
confident without improving play, or the comparison cannot leave
`evidence_wait` because the frozen artifacts are irrecoverable. The pinned
matrix, calibration, surprise, support/memory curve, and explicit wait state
make each failure visible without substituting a new ontology or claim.

## Absent, error, and operational boundaries

Every boundary fails closed with an attributable status:

- a missing, unlocked, or digest-mismatched likelihood/opponent checkpoint
  produces `evidence_wait` before play and never selects a fallback model;
- an absent viewer-visible semantic commitment identity produces a typed
  `rules_provider_gap`, never an inferred Python action or prompt transcript;
- empty world/query support, a space/source/history identity mismatch,
  non-finite or non-normalized mass, an out-of-range world index, and a
  materialization mismatch are hard integrity failures with no state mutation;
- a stale or illegal semantic `Command` remains a managym rejection and counts
  as an integrity failure rather than being retried positionally;
- a test-only likelihood can prove mechanics but is permanently ineligible for
  smoke, arena, rating, or admission evidence; and
- exceeding a registered time, artifact, worker, memory, or rollout cap stops
  the stage and records the cap failure without pruning belief support.

The selected boundary is exact and unpruned. Its opening support is 10,832
canonical worlds; the contract cap is 512 MiB for the belief representation,
4 GiB for artifacts, 16 wall-hours, 64 core-hours, and four workers. Both arms
use the same configured worlds per legal root action, rollouts per world,
materializer, likelihood work, semantic Command path, and deterministic seed
plan. Their configured budget ratios must remain exactly 1.0, while raw
playouts are reported rather than forced equal after divergent play. The
belief arm's end-to-end p95 may be at most 1.1 times the matched control gate.
All runtime artifact resolution is local and byte-locked before play; no
network fetch, retraining, recovery subprocess, or silent resume occurs inside
the evidence command.

## Scope

- In scope: canonical managym opponent-hand spaces; a bounded normalized
  manabot `BeliefState`; exact compatible-deal prior; selected viewer-safe
  likelihood updates; canonical world materialization; shared flat search;
  semantic Commands; paired belief/prior comparison; replay, calibration,
  legality, leakage, latency, throughput, memory, and explicit artifact wait.
- Out of scope: a new hand/query/action schema; legacy prompt history; direct
  exact-hand installation as a consumer contract; public-belief search; CFR;
  continual resolving; mixed-strategy training; broad cards/decks; hidden
  decklists; deck building; Study UI; new Projects, backlog activation, or a
  second PR.

## Done when

The focused checks pass:

```bash
uv run pytest tests/belief tests/sim/test_exact_range_runner.py
cd managym && cargo fmt --check
cd managym && cargo clippy --all-targets --all-features -- -D warnings
cd managym && cargo test
```

They prove:

- every `BeliefState` is normalized and identity-bound to one canonical space;
- the prior exactly normalizes managym's compatible-deal weights;
- belief transport matches brute-force tiny-space oracles without an
  independent hand enumeration;
- likelihood and search materialize only canonical world indexes;
- search roots preserve their semantic frame and opponent likelihood roots
  rebuild authoritative legality;
- no private hand, physical ID, raw prompt, or positional action becomes
  belief evidence;
- belief and control use the same materializer, search entry point, configured
  worlds/rollouts, and semantic Command path; and
- a missing checkpoint produces the registered evidence-wait result rather
  than a fallback model.

With the frozen artifacts present, smoke must produce a verified integrity
receipt. Task completion additionally runs the frozen arena stage:

```bash
uv run experiments/runners/run_exact_range_player.py \
  --contract experiments/contracts/int-9-exact-range-v1.json \
  --stage arena --out-dir .runs/int-9-exact-range-v1
```

If artifacts remain unavailable, implementation can be mechanically green but
the Task remains explicitly waiting for evidence; it does not claim completion.

This advances the Intelligence measures that a complete player acts in real
managym positions, matched search comparisons report legality, calibration,
latency, throughput, uncertainty, and cost, and evidence remains attributable
to one world-pinned semantic authority.

## Measure

Before arena execution, lock the fixed synthetic and replay corpus for:

- belief normalization and canonical space-identity continuity;
- posterior versus compatible-prior world-sampling frequencies;
- viewer-history, semantic-action, materialized-world, and belief digests;
- likelihood observations/second and updates/second by support size;
- canonical materialization and rollout throughput;
- p50/p95 likelihood, search-only, Command, and end-to-end latency; and
- peak RSS, serialized belief bytes, and canonical support count.

The arena reports paired belief-minus-prior play and block-bootstrap interval,
population rating difference and uncertainty, full matchup cells, per-seat
splits, competencies, integrity counts, raw playouts, and systems cost. A
positive belief claim requires the preregistered gameplay thresholds at matched
compute with every integrity gate green; posterior metrics only explain the
outcome.
