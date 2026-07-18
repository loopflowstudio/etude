# Train and Play the First Learned Semantic Runtime Policy

## Problem

INT-2 landed the missing runtime boundary: one viewer-safe `ExperienceFrame`,
typed ability programs, actual visible object bindings, and authoritative
`InteractionOffer` values can be joined into a revision-bound
`SemanticDecision`, decoded without fixed action widths, and executed as a
normal structured `Command`. Etude still has no learned manabot on that
boundary. The existing static structural probes cannot answer whether program
structure helps a policy learn or transfer in real play, and Teacher-0's
missing terminal checkpoint bytes must not delay this independent prototype.

INT-11 turns the landed boundary into the smallest honest learned system. It
generates replayable engine decisions for priority, targeting, and combat;
trains matched semantic, identity-only, and structure-shuffled policies across
three seeds; exercises their decoded Commands in managym; and places them in a
bounded world-pinned paired-seat matchup. The result is development evidence
for the Semantic Policy Prototype Project, not arena admission or a general
Magic policy claim.

## The demo

Run:

```bash
uv run experiments/runners/run_semantic_runtime_policy.py \
  --out-dir .runs/int-11-semantic-runtime-policy-v1
```

One command regenerates authoritative examples, trains all three arms at three
seeds, evaluates ordinary and identity/composition holdouts, replays decoded
Commands including a 35-target decision and a 64-line attacker decision, plays
the paired-seat micro-matchup, and writes a versioned manifest plus dataset,
checkpoints, replay rows, and metrics. The terminal summary names legality,
competencies, paired strength, loss, sample efficiency, p50/p95 latency,
throughput, environment SPS, seeds, compute, and every bound identity.

## Approach

### Learned policy

Add a reusable `manabot.semantic.runtime_policy` module. A runtime feature
projection consumes only values already present in `SemanticDecision`:

- normalized public facts from `ExperienceFrame` (turn, phase, life, visible
  zone counts, public battlefield stats, actor/controller/zone roles);
- compiler-emitted typed program tokens for every visible source or candidate;
- authoritative offer verbs, source bindings, choice roles, cardinalities,
  and candidate bindings.

The semantic arm embeds typed tokens with position, program-boundary, and
nesting-depth signals and runs a small two-layer Transformer encoder. Offer and
candidate heads join the resulting object representations to public frame and
offer features. The existing ragged decoder remains the only learned-policy
decoder, and `SemanticDecision.command()` plus `SemanticDecision.step()` remain
the only mutation path. Runtime IDs are joins and replay addresses, never
features.

The identity-only arm replaces typed program tokens with a visible-definition
embedding while keeping the same frame, offer, heads, optimizer, data, and
hidden width. The structure-shuffled arm uses the semantic architecture but a
fixed content-pinned permutation destroys token order and hierarchy while
preserving token multiset, data, capacity, and training budget. Unknown token,
definition, verb, subject, or checkpoint identities fail closed before
scoring.

### Authoritative examples and oracle

Generate examples by reconstructing deterministic managym scenarios, binding
each through `SemanticDecisionAdapter`, and recording the complete viewer-safe
frame, selected offer, final structured Command, source/post state digests,
scenario recipe, and identity hashes. The bounded deterministic oracle is
declared as label provenance, not as a strong teacher:

- priority/targeting: cast admitted Igneous Inspiration when available and
  prefer an opposing subject through its actual candidate binding;
- combat: select legal attackers using public power/toughness plus a declared
  semantic-operation utility, producing both selected and declined candidates;
- pass-only roots: select the authority's pass offer.

Training uses several admitted creature definitions. `Fire Nation Cadets` is
the card-identity holdout. `South Pole Voyager` is the composition holdout: its
gain-life-plus-branch composition is withheld while each primitive occurs in
training definitions. These holdouts admit no unknown opcode. At least one
target root exposes 35 candidates and one combat root represents all 64 subsets
of six attackers.

### Evaluation and artifacts

Evaluate all nine learned checkpoints on the same regenerated rows. Report
offer accuracy, exact candidate agreement, target accuracy, attack precision /
recall, prompt competency, total policy loss, and accepted/illegal Commands,
separately for ordinary, identity-holdout, and composition-holdout splits.
Capture fixed training-curve checkpoints to report examples required to reach
the declared competency threshold rather than treating final loss as sample
efficiency.

Benchmark quiet-host single-decision p50/p95 inference latency, batched example
throughput, and authoritative environment steps per second. Record parameter
counts, epochs, optimizer steps, wall time, process CPU time, peak RSS, host,
Torch/Python versions, and training seeds.

Play one bounded `w2` semantic-combat matchup as paired deal blocks with player
registrations swapped between seats. Each match begins from an exact recipe,
runs normal structured Commands to a terminal or declared cap, and retains the
Command/state-digest trace. Compare every arm pair and retain the full payoff
matrix. The current branch contains no callable approved INT-6 registration or
rating contract at this integration boundary. Keep the local result behind a
narrow arena adapter and emit a versioned `development_paired_arena_v1`
receipt that is explicitly non-promotional and makes no INT-6 compatibility
claim. This design does not infer a future rebase or contract migration; either
would require new evidence and a separately reviewed integration decision.

Artifacts use canonical JSON and SHA-256 identities. A top-level manifest pins
world, content pack, semantic IR/schema, experience protocol, structured-offer
contract, runner/workload, dataset, model architecture, checkpoints, seeds,
replays, and measured compute. Loading or replay verifies hashes before
decoding.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|-----------------|
| Is the real runtime boundary already executable? | Yes. INT-2's `SemanticDecisionAdapter` joins viewer-safe frames, immutable semantic programs, runtime objects, and Rust-owned offers. Existing tests execute a 35-target cast and one of 64 attacker subsets through `step_structured`. | Build the learner above this adapter; do not create a second observation, legality, offer, or command authority. |
| Can Lightning Bolt be the semantic target fixture? | No. The current semantic IR deliberately excludes Lightning Bolt, and the adapter correctly fails it as an unadmitted definition. Igneous Inspiration is admitted and publishes the same uncapped creature-or-player target shape. | Use Igneous Inspiration for targeting and preserve the unknown-primitive failure test. Do not broaden card coverage. |
| Is another static structural encoder viable? | No. Bag pooling is structurally blind and the first relational-pooling family failed trainability/cost gates. The project memory explicitly calls for a plausible end-to-end Transformer/tree-aware prototype. | Use a small positional/depth-aware Transformer and measure it in the running policy. Preserve existing katas unchanged. |
| Does normal structured execution cover the required slice? | Yes for pass, admitted single-target casts, and complete attacker declarations—the exact priority, targeting, and combat slice requested. Broader legacy action kinds are outside the narrow INT-2 adapter. | Make the micro-matchup terminal inside the admitted combat slice; do not widen Rules or claim full selected-deck coverage. |
| Can runtime object IDs leak identity or hidden truth? | The adapter already separates `RuntimeSubject` addresses from model fields and projects the opponent hand as hidden count only. It exposes visible definition rows separately from opaque runtime entity IDs. | Never embed entity/incarnation/candidate/offer IDs. Add viewer-equivalence and feature-invariance tests; retain IDs only in replay/Command artifacts. |
| Are Teacher-0 bytes required for labels or a control? | No. The task permits a deterministic engine oracle, and INT-2 plus the selected semantic pack generate exact supervised decisions locally. | Name oracle labels honestly and use matched identity/structure controls. Make no Teacher-0, search-strength, or promotion claim. |
| Is the INT-6 arena contract available? | No callable approved `ArenaKey`, `PlayerRegistration`, or rating contract exists in the current branch at this integration boundary. Neither the supplied Task/Project context nor the current `wave/intelligence/MEMORY.md` establishes one. | Emit clearly non-admission paired evidence behind a local adapter. Do not infer a future rebase, migration, rating, or INT-6 compatibility claim. |
| Can results be replayed without serializing private engine state? | Deterministic scenario recipes plus exact engine/content identities reconstruct each root; current `state_digest()` witnesses the root and post-command authority, while the frame/offer/Command retain the viewer contract. | Store recipes, both digests, viewer-safe payloads, and Commands, then replay every retained row before accepting the manifest. |
| Will a small CPU run satisfy the statistical unit? | Three independent training seeds are inexpensive for a 31-definition, bounded-example Transformer. Evaluation games within a seed quantify paired match behavior but do not become extra training replicates. | Train seeds 1101/1102/1103 for every arm, report per-seed rows and cross-seed summaries, and cap the default run for a laptop CPU. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Extend the killed relational pooler into gameplay | Reuses old code but retains the demonstrated capacity and CPU failure modes. | Project memory explicitly kills this family; doing so would turn INT-11 into another static-proof detour. |
| Train the existing fixed-width PPO `Agent` and attach semantics out of band | Reuses mature training/arena code but cannot consume uncapped authoritative offers or bind typed programs to runtime choices. | It would leave the core INT-11 contract unexercised and make structured decoding an adapter afterthought. |
| Wait for Teacher-0 and INT-6 | Would provide stronger labels and admission machinery. | Their missing bytes/contracts are named non-dependencies. Waiting would prevent the commissioned runnable prototype and confound semantic-policy learning with search/arena work. |
| Generalize `SemanticDecisionAdapter` to every selected-deck prompt | Moves toward full game coverage but requires new action/choice semantics across Rules and Etude protocol boundaries. | The requested smallest learned slice is already complete for priority, target, and combat. Broader action coverage belongs to provider work, not INT-11. |
| Treat replay rows as a second engine truth | Makes loading easy but duplicates mutable authority and risks drift. | Recipes reconstruct managym authority; rows witness viewer input and exact Commands but never define legality. |

## Key decisions

- The prototype is learned and end to end, but intentionally narrow. The
  winning demo is an ordinary structured `Command` changing a real engine and
  a terminal paired matchup—not a held-out classifier score alone.
- Program sequence, hierarchy, field role, and binding are represented with
  positional/depth-aware attention. The model is small enough that inference
  cost is measured rather than assumed.
- Identity-only and fixed structure-shuffled controls share data, public facts,
  decoder, heads, and optimization budget. Their differing feature boundary is
  recorded in every checkpoint manifest.
- The identity and composition holdouts are definition-level splits from
  engine-generated decisions. The composition uses known operations only;
  unknown opcodes never become an accidental out-of-vocabulary test.
- Legality is binary authority evidence: a decoded submission either becomes
  an accepted normal Command or the run fails. Accuracy cannot compensate for
  an illegal decode.
- The likely successful behavior is modest but real: a developer can retrain a
  manabot from exact engine positions and watch it choose an uncapped target or
  lethal attacker set through the product's semantic boundary. Wild success is
  that semantic holdout behavior survives where identity does not at acceptable
  latency. Wild failure is a semantically decorated memorizer whose strength,
  transfer, or cost matches the controls; the matched arms and paired runtime
  evidence make that failure useful rather than hide it.
- The arena receipt is deliberately local and non-promotional because no
  callable approved INT-6 contract exists at this integration boundary. No
  result from this task is called Elo, admitted, INT-6-compatible, or
  superhuman, and this design does not pre-authorize a future rebase.

### Evidence versus assumptions

Review provenance: this section was produced by a headless parent Project
review using the current INT-11 Task directive, the Semantic Policy Prototype
Project definition and KRs, `wave/intelligence/GOAL.md` and
`wave/intelligence/MEMORY.md`, and current-main tests and code. No human
reviewer confirmed the design or its assumptions.

The design treats the following as established evidence:

- INT-2's checked runtime join already projects viewer-safe
  `ExperienceFrame` facts, typed program rows, visible runtime bindings, and
  authoritative `InteractionOffer` values. Its focused tests execute a
  35-candidate target Command and one of 64 complete attacker declarations,
  and reject fabricated IDs, stale revisions, and unadmitted definitions
  before mutation (`tests/semantic/test_semantic_policy.py`).
- The existing ragged decoder has no fixed candidate width and lowers only
  authority-minted offer, role, and candidate IDs
  (`manabot/sim/structured_policy.py`,
  `tests/sim/test_structured_policy.py`).
- Viewer-safe semantic projection is already bound to the immutable content
  manifest and is invariant to opponent-private determinization under the
  semantic-only identity mode
  (`tests/semantic/test_learning_projection_env.py`).
- The checked semantic IR contains all primitives used by the proposed
  identity and composition holdouts. Lightning Bolt is absent and therefore
  must continue to fail admission; Igneous Inspiration is the admitted
  creature-or-player targeting fixture
  (`content/semantic/v1/generated/two_deck.ir.json`).
- The current branch contains no callable approved INT-6 registration or
  rating types at this integration boundary. The supplied
  `wave/intelligence/MEMORY.md` does not establish such a contract, so the
  design makes no compatibility or future-rebase claim.

The following remain explicit experimental assumptions rather than facts:

- a two-layer positional/depth-aware Transformer can learn this bounded oracle
  within the declared CPU and sample budget;
- withholding Fire Nation Cadets and the gain-life-plus-branch composition on
  South Pole Voyager meaningfully probes identity and compositional transfer;
- semantic structure will improve holdout loss or exact agreement over the
  identity-only and structure-shuffled controls;
- cached immutable definition representations are the appropriate serving
  latency boundary; and
- the bounded `w2` terminal combat matchup and admitted Igneous Inspiration
  targeting fixture are suitable end-to-end representatives for the requested
  priority, targeting, combat, transfer, and paired-strength evidence rather
  than merely legal demonstrations;
- the terminal paired combat micro-matchup is sensitive enough to distinguish
  the learned arms rather than merely confirm legal execution; and
- the default 600-second CPU wall-clock budget per checkpoint is sufficient to
  train all declared arms to an interpretable outcome without changing the
  pre-registered optimizer or sample budget.

The one-command run measures these assumptions. It must report a refutation as
an outcome; it must not rewrite an assumption as evidence after seeing results.

### Integrity gates versus experimental outcomes

Integrity gates decide whether the run is valid at all:

- exactly three declared training seeds execute for every arm on matched data,
  optimizer budget, hidden width, and decoder;
- held-out definitions never occur in training, the composition holdout uses
  only opcodes present in training, and every unknown primitive fails before
  scoring;
- at least one authoritative target decision has more than 32 candidates and
  one combat decision represents more than 32 complete legal branches;
- every decoded submission becomes a normal accepted structured `Command`,
  with zero illegal decodes or fail-open fallback;
- every retained recipe regenerates the same viewer feature digest and source
  state digest, and every retained Command regenerates its post-state digest;
- opponent-private truth never enters model features; and
- manifests bind exact world, content, engine, observation, offer, model,
  dataset, checkpoint, seed, replay, and compute identities.

Experimental outcomes answer whether the prototype is useful:

- policy loss, exact offer/candidate agreement, and prompt competencies;
- identity- and composition-holdout transfer versus matched controls;
- examples required to reach the declared 90% competency target;
- p50/p95 latency, batch throughput, environment SPS, wall/CPU time, and peak
  memory; and
- the complete paired payoff matrix and paired strength summary.

The 90% agreement target, semantic-over-identity transfer prediction, 2x p95
latency bound, and any paired-strength separation are outcome thresholds, not
integrity gates. Missing them produces an honest `revise` or `kill/redesign`
result for the Semantic Policy parent Project; it does not invalidate an
otherwise reproducible zero-illegality experiment or license post-result gate
changes. Conversely, a strong outcome cannot compensate for an integrity
failure.

## Scope

- In scope: one small Transformer policy, matched identity and
  structure-shuffled controls, public runtime facts, typed program structure,
  authoritative offers, existing ragged decode, normal structured Commands,
  priority/target/combat examples, >32 choices, three seeds, identity and
  known-operation composition holdouts, paired-seat bounded play, replay,
  legality, competencies, loss, sample efficiency, strength, latency,
  throughput, SPS, seeds, compute, versioned manifests, tests, and one command.
- Out of scope: Rules changes, new card/opcode coverage, Lightning Bolt
  admission, full selected-deck prompt coverage, Teacher-0 substitution,
  search/PUCT integration, value learning, self-play populations, INT-6 rating
  or promotion, Study or UI work, natural-language semantics, hidden opponent
  truth, static kata additions, Commander/format breadth, and superhuman claims.

## Done when

- `uv run pytest tests/semantic/test_runtime_policy.py -q` proves viewer-safe
  feature projection, matched model arms, fail-closed unknowns, 35-target and
  64-attacker decoding, normal Command execution, checkpoint verification, and
  deterministic replay.
- `uv run experiments/runners/run_semantic_runtime_policy.py --out-dir
  .runs/int-11-semantic-runtime-policy-v1` regenerates data; trains semantic,
  identity-only, and structure-shuffled arms at seeds 1101/1102/1103; evaluates
  ordinary plus both holdouts; plays the paired matchup; verifies every replay;
  and exits zero only with no illegal Commands.
- The top-level manifest and concise checked result bind all authoritative and
  learned artifact identities and report legality, prompt competencies, paired
  strength/payoff matrix, policy loss, sample efficiency, p50/p95 inference,
  batch throughput, environment SPS, seed-level rows, parameter/compute cost,
  and the non-admission limitation.
- Existing focused boundaries continue to pass:
  `uv run pytest tests/semantic/test_semantic_policy.py
  tests/sim/test_structured_policy.py -q`.

## Measure

The default run records a pre-result contract rather than a success guarantee.
Required integrity is zero illegal decodes, exact root/post digest replay, no
viewer-private feature variance, and at least one evaluated decision with more
than 32 legal choices. The first practical competency target is at least 90%
aggregate offer/candidate agreement for each semantic seed and exact lethal
attacker selection in the bounded matchup. Semantic transfer is considered a
promising result only if its mean identity/composition holdout loss or exact
agreement improves over identity-only without more than 2x its p95 latency;
otherwise the result nominates the measured failure for the parent Project's
next review. Strength is reported as the complete paired payoff matrix, never
as an admission rating.
