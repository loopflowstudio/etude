# Search and Learning Architecture: Authorities, Identities, and Laws of Physics

## Problem

The Search Teacher and Student Arena now spans a real rules engine, three search
paths, replayable teacher evidence, supervised shards, model checkpoints,
pairwise evaluation, and Study export. Each piece works, but the system does not
yet have one builder-facing map that answers the questions that matter when the
pieces are changed:

- Which type owns truth at this boundary?
- What identifies a state, action, model, player, or piece of evidence?
- What may mutate, and what invalidates a previously valid handle?
- Which data is legal for the acting viewer to observe?
- What must be pinned for deterministic replay?
- Which compute quantities are comparable, and which only look comparable?

Without that map, a local convenience can silently become a false system
contract. An action index can be treated as a durable action identity. A
viewer-safe observation can be truncated until the played action disappears.
A checkpoint path can stand in for checkpoint bytes. Seat balancing can be
mistaken for paired-deal evaluation. A determinized tree can be described as
information-set-consistent search. Study evidence can be structurally valid
while belonging to the wrong replay.

This review gives builders a top-down map of the current implementation before
making architectural recommendations. It serves the Intelligence objective by
making the runnable teacher → student → arena → Study loop easier to extend
without weakening rules authority, information-set honesty, replayability, or
measurement.

## The demo

This is an explicitly documentation-only slice. A developer opens this document
and traces one real decision from `managym::GameState` through viewer projection,
structured command execution, a determinized PUCT result, a training target, a
student checkpoint, arena registration, and `DecisionEvidence`. At every arrow
they can name the owner, identity, mutation rule, legality check, information
boundary, replay inputs, and performance unit without reverse-engineering the
repository.

## Approach

Adopt a layered architecture with one authority per concern and typed receipts
between layers:

```text
                              Rules authority (managym)
  ContentPack ──> Game/GameState ──> Observation(viewer) + ActionSpace(revision)
                         │                         │
                         │ exact fork              └──> StructuredOfferSet
                         v                                      │
                  SelectedBranchRuntime <── AtomicCommand ─────┘
                         │
                         ├── sample world ──> PUCT + LeafEvaluator ──> SearchResult
                         │                                              │
                         │                                              ├── audit trace
                         │                                              └── teacher target
                         v
                 immutable shard manifest ──> Agent + checkpoint manifest
                                                       │
                                                       v
                         ArenaKey + PlayerRegistration + paired-deal results
                                                       │
                                                       v
                       exact historical context + StudyIdentity + DecisionEvidence
```

The diagram is directional. Higher layers consume receipts from lower
authorities; they do not reconstruct lower-layer meaning. In particular:

- managym owns rules state, legality, viewer projection, exact forks, and the
  final mapping from a structured command to an engine action;
- manabot owns search, belief/world sampling policy, targets, models,
  checkpoints, opponents, evaluation, and search evidence;
- Etude owns the replay address, Study experience, presentation, and research
  consent, while consuming attributable Intelligence evidence.

### Status legend

| Status | Meaning |
| --- | --- |
| **Current** | Present on this branch and usable by builders now. |
| **In flight** | Designed or implemented in another active Task branch; not a contract of this branch. |
| **Desired** | Architectural direction recommended by this review; no implementation is authorized here. |

## Current system map

### 1. Rules state, object identity, and legal actions

| Central type or API | Owner and identity | Mutation and legality | Information and replay | Performance law | Status |
| --- | --- | --- | --- | --- | --- |
| [`ContentPack`](../managym/src/flow/game.rs) | managym; immutable content identity is external to the `Arc` and must include exact pack/version bytes. | Shared by cloned games; rules definitions do not mutate during a game. | Public content is safe, but a pack locator is not a content digest. Exact replay pins content identity. | Clone shares the `Arc`; do not duplicate definitions per branch. | **Current** |
| [`GameState`](../managym/src/flow/game.rs) | managym; the authoritative state consists of zones, objects, turn, stack, combat, events, RNG, and allocators. | Only engine transitions mutate it. Callers never patch state to express an action. | Contains both players' private data and is never a policy observation. Exact cloning preserves the hidden state and RNG. | Full cloning is the selected correctness baseline; optimization must preserve witnesses exactly. | **Current** |
| `EntityId`, `Incarnation`, `ObjectRef` in [`state/game_object.rs`](../managym/src/state/game_object.rs) | managym; physical entity, zone-change generation, and exact rules reference respectively. `CardId` is storage identity and `ObjectId` is presentation identity. | A zone change advances incarnation. A stale `ObjectRef` must fail rather than retarget a new incarnation. | Public projections may expose presentation identity; authority resolution stays in managym. | Integer IDs are cheap, but different ID domains are not substitutable. | **Current** |
| [`Game`](../managym/src/flow/game.rs) | managym; owns `GameState`, current `ActionSpace`, `decision_epoch`, pending choice, trackers, and optional undo. | `publish_action_space` advances the decision epoch. `step(index)` bounds-checks against the current legal list and executes one action. | `Clone` is exact for live state and resets undo. A state digest or witness is evidence about a state, not a restorable snapshot. | `skip_trivial` may execute forced actions without surfacing a decision; decision count is post-collapse. | **Current** |
| [`Action` and `ActionSpace`](../managym/src/agent/action.rs) | managym; `Action` is the authoritative internal operation, while `ActionSpace` is an ordered set scoped to one decision epoch. | An action is legal only because it is a member of the current `ActionSpace`. | The integer position is a revision-local lookup key, not a durable action identity. Replay must bind it to the exact state/revision or use a command receipt. | Legal branching is the current list length, after prompt factorization and trivial-action collapse. | **Current** |
| [`StructuredOfferSet` and `AtomicCommand`](../managym/src/agent/structured_offer.rs) | managym; public offers plus a private decision-epoch binding from offer identity to the exact engine action. | Applying a command rechecks binding and action equality, then commits exactly one action. Caller-supplied candidate values are never authority. | The public half is viewer-facing; the private binding may be retained by exact clones but must not be serialized as evidence. | Search uses one action-aligned offer per current action; richer grouped prompts are a product representation, not a search speedup yet. | **Current** |
| [`ExperienceFrame`, `InteractionOffer`, `Command`](../etude/experience_protocol.py) | Etude wire contract; identity is match + expected revision + prompt + offer + answers. | Etude validates the envelope, then its server-side `DecisionContext` lowers the command to the bound engine action. | A frame is viewer-projected. Protocol `Command` is the replay/product envelope; native `AtomicCommand` is the private engine transaction. | They are complementary layers, but frame/offer construction is currently duplicated in the server and teacher evidence path. | **Current** |

#### Laws at this layer

1. **There is one rules authority.** Search, models, Study, and clients may
   select among legal offers; none may invent legality or mutate `GameState`
   directly.
2. **Position is not identity.** `action_index == 3` means only “the fourth
   action in this exact action space.” It does not survive a revision, a
   different determinization, or a differently ordered legal list.
3. **Object identity is generational.** An entity after a zone change is not
   the prior `ObjectRef`, even if a card name or presentation ID looks the same.
4. **A command is a transaction against a revision.** The decisive check
   occurs immediately before engine mutation. Old commands fail closed.
5. **Digests have domains.** Engine state digests, representation-neutral
   search witnesses, Etude frame hashes, and artifact digests answer different
   questions and are never interchangeable.

### 2. Viewer-safe observations and model inputs

| Central type or API | Owner and identity | Mutation and legality | Information and replay | Performance law | Status |
| --- | --- | --- | --- | --- | --- |
| [`Observation::for_player`](../managym/src/agent/observation.rs) | managym; one projection for a fixed viewer at one authoritative state. | Immutable projection. If another player acts, public prompt kind may remain visible while candidates and focus are suppressed. | Includes viewer hand, public zones, stack, and viewer-revealed library cards; omits opponent hand. | Projection is cheap enough for live play; recent events and object lists remain ragged before encoding. | **Current** |
| Rust [`ObservationEncoder`](../managym/src/agent/observation_encoder.rs) | managym ABI for fixed tensors; identity is the exact encoder config, enum meanings, dimensions, and caps. | Encoding does not change state. Tensor action rows are legal only when aligned with the source action space. | Viewer safety is inherited from the source observation, not created by the encoder. | Fixed caps are 60 cards, 40 permanents, 32 actions, 2 focus objects, and 32 events in the current ABI. | **Current** |
| Python [`ObservationSpace`](../manabot/env/observation.py) | manabot mirror of the Rust tensor ABI. | Scalar `Env` uses this path; `VectorEnv` uses Rust encoding. Oversized fields may be truncated. | A viewer-safe tensor can still be incomplete. If the selected legal action is truncated, a training row is invalid. | Python and Rust enum/shape duplication creates drift risk and two hot paths. | **Current** |
| [`Agent`](../manabot/model/agent.py) | manabot; model identity is architecture/hypers + exact parameters + observation/action ABI. | Consumes a batch, masks invalid action logits, emits per-action logits and an actor-relative scalar value. | It may consume only viewer-safe tensors. The current value is a scalar over the encoded observation, not a public-belief value. | Attention over the fixed object sequence dominates inference; batching is essential for rollout-heavy use. | **Current** |

#### Laws at this layer

6. **Viewer projection precedes learning.** A policy never receives an
   authority state and promises not to look. managym removes hidden data first.
7. **Viewer-safe does not mean decision-complete.** Fixed-cap truncation is an
   integrity event. A row whose legal surface no longer contains the chosen
   action must be rejected, not relabeled.
8. **The observation ABI is part of the world.** Enum meanings, dimensions,
   caps, ordering, and semantic features bind datasets and checkpoints just as
   content and engine versions do.
9. **Actor-relative values need explicit perspective conversion.** Search
   backups, terminal outcomes, shard targets, and Study values must state whose
   utility they represent.

### 3. Exact forks, determinization, beliefs, and sampled worlds

| Central type or API | Owner and identity | Mutation and legality | Information and replay | Performance law | Status |
| --- | --- | --- | --- | --- | --- |
| [`SearchStateWitness`](../managym/src/search_state.rs) | managym comparison evidence: authority fingerprint, legal surface, acting and fixed-viewer projections, diagnostics. | Immutable evidence. It cannot restore a game. | Lets branch implementations prove semantic equivalence without exposing private state to a policy. | Witness equality is the acceptance gate for faster branch backends. | **Current** |
| [`BranchDriver`](../managym/src/search_state.rs) | managym benchmark contract: exact fork, determinize, reseed, mark/apply/rollback, witness. | Candidate drivers may use clone, undo, or COW, but must preserve the reference semantics. | Full clone is the oracle. Private state remains inside the driver. | Optimization is valid only when throughput/RSS improves with identical witnesses. | **Current benchmark abstraction** |
| [`SelectedBranchRuntime`](../managym/src/python/bindings.rs) via [`BranchSession`](../manabot/sim/search_branch.py) | Selected production search boundary; current ID is `full_clone/current_game_v1`. | Root is guarded; branch mutations flow through structured search offers and emit receipts. | `fork_exact` preserves authority; `determinize` samples hidden state; raw bypasses are rejected. | Full clone is intentionally the selected reference backend. Faster benchmark drivers are not silently substituted. | **Current** |
| [`Game::determinize`](../managym/src/flow/search.rs) | managym mechanism; sample identity is root + acting viewer + seed + algorithm/version. | Mutates only the branch: samples opponent hand from unseen hand/library cards and shuffles libraries while pinning viewer-revealed cards. | Uniformly consistent with current card removal and reveals; it does not condition on public action likelihoods. | World count and per-world allocation are first-class costs. | **Current** |
| Exact range and likelihood-weighted sampling | Intelligence belief layer: explicit support, normalized mass, public-history update model, and sampler identity. | Public actions update mass; chance/reveals update support; sampled authority branches remain private. | Must use only the acting viewer's historical information. Belief quality changes sampling, not the legality engine. | Exact support is 10,832 opening-hand count vectors for the selected small range; checkpoint absence currently fails closed. | **In flight in INT-9; not live on this branch** |

#### Laws at this layer

10. **A branch is authoritative state, not a decoded observation.** The only
    exact fork starts from `Game`; observations and witnesses are projections.
11. **The root is immutable during search.** Each simulation mutates a child
    branch. Direct root mutation or an unreceipted action invalidates replay.
12. **Sampling is an Intelligence policy over a Rules mechanism.** managym
    knows how to materialize a legal hidden world. manabot decides the
    viewer-historical distribution from which worlds are drawn.
13. **Belief accuracy and information-set consistency are different axes.** A
    perfect posterior can improve sampled worlds while separate perfect-
    information trees still condition future choices on hidden facts.
14. **Audit-private is not viewer-public.** True hands, world seeds, and branch
    witnesses may be retained for replay and calibration but cannot appear in
    policy inputs or viewer-facing Study evidence.

### 4. PUCT, leaf evaluation, and search results

| Central type or API | Owner and identity | Mutation and legality | Information and replay | Performance law | Status |
| --- | --- | --- | --- | --- | --- |
| [`determinized_puct`](../manabot/sim/mcts.py) | manabot; algorithm identity includes world sampler, world count, total simulations, exploration constant, leaf evaluator, seeds, and source/model identity. | Builds an independent tree per sampled world, applies only legal structured offers, and aggregates root visits and Q values. | Each world tree has perfect information after determinization. The aggregate is viewer-rooted but not an information-set tree. | Simulation budget is total adaptive traversals across all worlds, with at least one per world. | **Current** |
| `UniformRandomLeafEvaluator` | manabot; terminal rollout policy + seed + maximum horizon. | Mutates only the simulation branch to terminal. | Has no hidden-data leak beyond the already determinized branch, but continuation behavior is strategically weak. | Unbiased for its rollout policy and expensive per leaf. | **Current** |
| `AgentLeafEvaluator` | manabot; checkpoint bytes + model/ABI + device + evaluation semantics. | Reads branch observation and returns priors/value without engine mutation. | Must project for the acting player at the leaf. Current implementation is CPU-only. | Faster leaves can buy traversals but model error and inference latency become dominant. | **Current** |
| [`PuctResult`](../manabot/sim/mcts.py) | manabot immutable search output: root visit counts, Q values, root value, per-world matrices, sampled seeds, branch receipt. | No mutation; selected action is derived from visits with declared tie-breaking. | Public evidence may expose aggregated alternatives and robustness; raw worlds and seeds stay audit-private. | Visits, worlds, and realized latency are all reported; no single scalar describes cost. | **Current** |

The current teacher is real adaptive MCTS, but its architecture is
**determinized PUCT**, not ISMCTS, public-belief search, or CFR. It searches a
different perfect-information tree for each sampled world and aggregates only
at the root. That is useful and measurable, but it permits strategy fusion.
[ISMCTS](https://eprints.whiterose.ac.uk/id/eprint/75048/) instead searches trees
whose nodes represent information sets, and
[EPIMC](https://arxiv.org/abs/2408.02380) addresses strategy fusion by
postponing perfect-information resolution. Neither property should be inferred
from root aggregation.

#### Laws at this layer

15. **Budget names are semantic.** Teacher-0 flat MC counts playouts per legal
    root action. Teacher-1 PUCT counts total adaptive traversals across all
    determinizations. Compare them by pinned realized latency and report both
    raw budgets; equal integer `sims` is not matched compute.
16. **Tree statistics have perspective.** Node selection, opponent turns,
    terminal payoff, leaf value, Q values, root value, and exported evidence
    all declare the root/actor perspective and convert exactly once.
17. **A search result is a vector, not an action.** Visits, Q values, value,
    world robustness, uncertainty, and the finally played command remain
    distinct. No field substitutes for another when unavailable.
18. **Determinized PUCT cannot make an information-set claim.** Strong play
    from this teacher is evidence for the running system at its information
    boundary, not evidence that strategy fusion has been solved.

### 5. Teacher trajectories, shards, targets, and checkpoints

| Central type or API | Owner and identity | Mutation and legality | Information and replay | Performance law | Status |
| --- | --- | --- | --- | --- | --- |
| [`record_teacher_trajectories`](../manabot/sim/teacher1_evidence.py) | manabot audit artifact; identity includes game/deal seeds, every viewer frame and command, search spec, world seeds/statistics, source, and final outcome. | Append-only logical trace. Each played command must be legal in the recorded frame. | Full replay may retain private audit data; exported frames are actor-safe. | Exact search replay intentionally reruns expensive selected roots. | **Current** |
| [`replay_teacher_trajectories`](../manabot/sim/teacher1_evidence.py) | manabot verifier of the exact recorded context. | Rebuilds frames/commands, reruns selected searches, checks arrays/metadata, then replays the game. | Exact equality assumes the pinned runtime, sources, seeds, and deterministic floating behavior. | `verify` is allowed to be expensive but must never launch new unrecorded generation. | **Current** |
| [`generate_selfplay_shard`](../manabot/sim/distill.py) | manabot training data; row identity is dataset run + shard + game + decision. | Writes observation tensors, chosen action, legal mask/count, seat/outcome, and optional score/visit/root-value targets atomically. | Rows are viewer-safe and reject a played action missing from the encoded legal surface. | NPZ is compact; generation cost is dominated by teacher decisions. | **Current** |
| [`load_shards`](../manabot/sim/distill.py) | manabot convenience loader. | Concatenates compatible-looking arrays; optional fields survive only if all shards contain them. | It does not itself verify file digests or a shared provenance contract. | Convenient for local iteration; too permissive to be the integrity boundary of a promoted run. | **Current** |
| [`train_search_supervised`](../manabot/sim/search_supervised.py) | manabot; training run identity includes exact shard manifest, split, target kinds, hypers, seed, source/runtime, and outputs. | Supports `score_softmax`, `visit_distribution`, or `chosen_action` policy targets and terminal-outcome or root-value supervision. | Game-level splitting prevents decision leakage across train/validation. | Shared encoder policy/value learning is practical; seeds are the experimental unit for method claims. | **Current** |
| `save_bc_checkpoint` / `load_checkpoint_agent` | manabot; desired identity is digest of exact bytes plus schema, architecture, ABI, world, training run, and source. | Checkpoint is immutable after publication; loader reconstructs model from saved hypers. | The current loader trusts a path and does not independently enforce world or digest identity. | Loading is cheap; inference performance depends on model layout/device/batching, not file size. | **Current mechanism; identity supplied by outer manifests** |
| Production manifest and cumulative cap ledger | manabot run envelope; immutable stage/job digests bind inputs, outputs, caps, and gate decisions. | Append-only ledger is checked before every stage and child launch. Tamper or missing exact checkpoint bytes fails closed. | This is the reviewed production integrity boundary; it is stronger than the core NPZ/checkpoint loaders. | Integrity checks must not become generation work and are negligible beside labels/training. | **Current in INT-4 production runner** |

#### Laws at this layer

19. **Targets are typed and non-interchangeable.** A chosen action, score
    softmax, root visit distribution, terminal outcome, and search root value
    represent different supervision. A missing target is not zero.
20. **A file path is a locator, not identity.** Dataset and checkpoint identity
    comes from exact bytes plus the manifest that binds world, source, schema,
    seeds, and generation/training configuration.
21. **Atomic write is not immutability.** Temp + fsync + replace prevents torn
    files. Promotion additionally requires content digests, append-only
    manifests/ledger, and rejection of later mutation.
22. **Replay and training artifacts have different shapes.** Audit trajectories
    preserve the exact command/search narrative. Shards optimize batched
    learning. They share identities and receipts; neither should be overloaded
    to replace the other.
23. **Verification never generates.** A replay/verify mode may recompute from
    recorded seeds, but it must fail closed rather than fill missing shards,
    checkpoints, worlds, or evidence.

### 6. Players, evaluation, ratings, and promotion

| Central type or API | Owner and identity | Mutation and legality | Information and replay | Performance law | Status |
| --- | --- | --- | --- | --- | --- |
| [`MatchupPlayer.act`](../manabot/sim/flat_mc.py) | manabot runtime behavior; current player specs are loose dictionaries for random/search/model variants. | Returns an action for the current legal surface. Legality remains an engine check. | Each player must receive only its seat's observation/history. | Different player kinds expose inconsistent latency/search detail today. | **Current** |
| [`play_games`](../manabot/sim/flat_mc.py) | manabot pairwise evaluator. | Alternates hero seat and uses a fresh deal seed per game. | Deal-diverse and seat-balanced, but opposite-seat games do not share the same deal. | Wilson intervals quantify game noise for one fixed pair; they are not cross-training-seed uncertainty. | **Current** |
| `ArenaKey`, `PlayerRegistration`, paired-deal matrix, rating, promotion gate | manabot versioned skill arena. Arena identity is world + content suite + viewer boundary + arena version + anchor cohort + compute class. | Registrations and results are immutable; candidates challenge a frozen anchor cohort on both seats of each exact deal. | Command traces replay legality. Ratings and promotion never cross arena identity. | Proposed Bradley-Terry order-effect model plus deal-block bootstrap; full payoff matrix and residuals remain visible. | **In flight in INT-6; not live on this branch** |

The current harness can answer a pairwise seat-balanced question. It cannot yet
answer “which agent is admitted in this world?” Pairwise games use different
deals on opposite seats, no typed player identity pins model/search bytes, and
there is no closed cohort, payoff matrix, population rating, or promotion
decision.

The in-flight arena design resolves those problems with paired deals and a
closed `ArenaKey`. Its rating estimator still needs defensive treatment of
separated or sparse matchup graphs: unregularized Bradley–Terry estimates may
not exist for dominance-separated data. The design therefore uses a declared
Gaussian MAP prior, retains the raw matrix, and bootstraps paired deal blocks.
This follows the known [Bradley–Terry existence
condition](https://arxiv.org/abs/math/0412232) rather than treating a finite
rating as guaranteed.

#### Laws at this layer

24. **Seat balance is not deal pairing.** Alternate seats removes aggregate
    seat allocation bias. Paired evaluation holds the deal fixed and swaps
    seats, enabling lower-variance pair effects and deal-block uncertainty.
25. **A player is a registered artifact, not a nickname.** Identity binds
    algorithm, checkpoint bytes, world/ABI, information boundary, compute
    class, and deterministic seed policy.
26. **Ratings are arena-local coordinates.** They never cross world, content,
    viewer boundary, cohort, rating-model, or compute-class changes.
27. **A rating does not replace the matrix.** Promotion reads population
    rating, paired uncertainty, legality, competencies, latency/throughput,
    calibration, and matchup residuals together.
28. **Training seed is the method-level experimental unit.** More evaluation
    games narrow one checkpoint's game noise; they do not create independent
    evidence about a learning method.

### 7. Study evidence

| Central type or API | Owner and identity | Mutation and legality | Information and replay | Performance law | Status |
| --- | --- | --- | --- | --- | --- |
| [`StudyIdentity`](../etude/study_protocol.py) | Etude/Study evidence context: content, engine, observation/action/model/checkpoint, search spec/budget, state and historical viewer identities. | Immutable. Any changed component denotes different evidence. | Must select the exact historical viewer and replay; identity mismatch is typed unavailability. | Hashing/validation is negligible; recovering exact historical runtime bytes may be the real cost or blocker. | **Current contract** |
| [`DecisionEvidence`](../etude/study_protocol.py) | Intelligence quantities attached to one Study decision: played command, policy mass, visits, value, robustness, uncertainty, and unavailable fields. | Immutable and alternative-complete for the bound offer set. Quantities remain separate. | Rejects opponent-hand leakage and binds frame, pack, viewer, prompt, offer, command, and address. | Compact viewer-facing summary; raw sampled worlds remain outside the artifact. | **Current contract and one exact fixture path** |
| [`build_study_artifact`](../manabot/sim/study_evidence.py) | manabot adapter from running policy/search outputs into the Study schema. | Selects a recorded root and validates the finished artifact. | Caller currently supplies identity fields; exact arbitrary historical replay export still depends on the matching checkpoint/runtime seam. | Standard error and sampled-world robustness are derived from recorded search, not invented client-side. | **Current adapter with fail-closed context limits** |

#### Laws at this layer

29. **Study evidence belongs to one evidence context.** Pack, engine,
    observation/action/model/checkpoint, state, viewer, Study identity,
    decision/prompt/offer, budget, sampled-world set, and seed identities all
    match the selected replay.
30. **A checked fixture is not a universal substitute.** Evidence from another
    replay may prove the adapter works; it cannot answer the historical
    position currently selected by a person.
31. **Unavailable is data.** Missing checkpoint bytes, policy mass, visits,
    value, robustness, or uncertainty remains explicitly unavailable. Values
    are never copied from a nearby run or inferred from another quantity.
32. **Study does not become a second rules or replay engine.** It navigates and
    presents authoritative frames, commands, and Intelligence evidence.

## End-to-end problem traces

### Problem A: Execute a legal decision

**Current.** `Game` publishes an `ActionSpace` and increments its decision
epoch. managym projects an actor observation and structured offers. A policy or
search component chooses an offer/action. `AtomicCommand` resolves through the
private offer binding, while Etude's wire `Command` binds match/revision/prompt/
offer before lowering to the engine. The engine revalidates at mutation time.

**Duplicate or missing contract.** Native search, teacher evidence, and Etude
server code each adapt engine actions into frames/offers/commands. Several use
the current action index as an offer ID. That is safe only inside the exact
revision and should not be mistaken for canonical action identity across
hypothetical hands or histories.

**Desired.** One managym-owned projection/binding API produces the public
decision contract and private lowering receipt for all consumers. Keep the
native `AtomicCommand` and protocol `Command` layers, but delete independent
meaning reconstruction from adapters.

### Problem B: Represent what the actor knows

**Current.** `Observation::for_player` is the information boundary. Rust and
Python encoders turn it into fixed arrays. Dataset generation checks the chosen
action remains inside the encoded legal surface.

**Duplicate or missing contract.** Rust and Python duplicate enum tables,
dimensions, caps, and encoding behavior; scalar and vector environments use
different implementations. Truncation metadata is not uniformly represented
as a hard integrity result.

**Desired.** Rust is the sole observation ABI implementation. Python receives
arrays plus a typed encoding receipt containing ABI digest, source revision,
original/encoded counts, and truncation status. Training and search reject any
legality-affecting truncation.

### Problem C: Search hidden-information worlds

**Current.** `SelectedBranchRuntime` exact-forks the authoritative root,
uniformly determinizes hidden cards from the actor-consistent unseen pool, and
runs independent PUCT trees. Structured offers keep every branch action legal;
`PuctResult` retains aggregate and per-world evidence.

**In flight.** INT-9 adds exact range support, public-action likelihood updates,
and likelihood-weighted determinizations through the same command/search path.
It improves beliefs but does not make the tree information-set-consistent. No
frozen w2 checkpoint exists locally, so its registered gameplay comparison
correctly remains unavailable.

**Desired.** Define a `WorldSampler` contract with explicit viewer-history,
support/model identity, normalization receipt, seed schedule, and sampled-world
digest. PUCT consumes it without knowing whether sampling is uniform, exact-
range, or learned. A later ISMCTS/public-belief/CFR treatment is a different
`Planner`, not a boolean mode on determinized PUCT. Naive public-policy
reductions are not automatically game-equivalent; the
[Sokota et al. conditions](https://proceedings.mlr.press/v202/sokota23a.html)
must be addressed if that architecture is built.

### Problem D: Turn search into supervision

**Current.** Teacher trajectories preserve replay detail. Shards preserve
batched tensors and typed optional targets. Supervised training chooses visit,
score, or chosen-action policy targets and terminal or root-value targets.

**Duplicate or missing contract.** Core loaders accept paths and compatible
shapes; the stronger identity/tamper guarantees live in the production runner.
This makes the safe path a convention above a permissive library API.

**Desired.** Promote a shared `ArtifactManifest` value type used by trajectory,
shard, checkpoint, arena, and Study adapters. Every loader accepts a manifest
plus expected context, verifies bytes before decoding, and returns a typed
receipt. Local scratch helpers may opt into an explicit `unverified` namespace;
production code cannot.

### Problem E: Train and load a practical student

**Current.** The `Agent` learns masked action logits and an actor-relative
scalar value from game-grouped train/validation splits. Checkpoints include
model state and hypers; orchestration binds outer provenance.

**Duplicate or missing contract.** Checkpoint schema does not intrinsically
bind world/content/observation/action/model identities, and the loader trusts a
path. The scalar value model also cannot be relabeled as a range-conditioned
counterfactual value without changing its mathematical type.

**Desired.** `CheckpointManifest` binds exact weights, model schema, ABI/world,
training dataset/run, target semantics, seed, source/runtime, and measured
inference profile. `LoadedPlayer` refuses a mismatched arena or leaf-evaluator
context. Future range values use a new model/output type, not an extra flag on
the scalar head.

### Problem F: Compare and promote agents

**Current.** `play_games` is useful for deal-diverse, seat-balanced pairwise
iteration and Wilson game-noise intervals.

**In flight.** INT-6 designs a closed, world-pinned arena with typed players,
same-deal seat swaps, full matrices, population ratings, deal-block bootstrap,
competency and systems gates, and immutable promotion receipts.

**Desired.** Land the INT-6 boundary essentially as designed. Keep a lightweight
pairwise harness for development, but label its output `pairwise_evaluation`,
never `arena_admission`. Rating model, prior, anchor cohort, compute class, and
promotion thresholds are part of `ArenaKey`. A candidate cannot promote if any
identity is unresolved or its exact checkpoint bytes are missing.

### Problem G: Explain a historical decision in Study

**Current.** The running policy/search evidence can be transformed into a
strict `DecisionEvidence` object and validated against a historical frame and
command. Private world details remain audit-only.

**Duplicate or missing contract.** The adapter can accept caller-supplied
identity strings, while the durable law requires exact bytes and the exact
historical runtime export seam. A successful artifact for one replay is not a
general historical export capability.

**Desired.** The historical replay service provides a signed/hashed
`DecisionEvidenceRequest` containing the exact context. Intelligence either
resolves every artifact and runs/loads the bound system or returns typed
unavailability per field. Study never selects a “close enough” fixture.

## De-risking

| Question | Finding | Impact on design |
| --- | --- | --- |
| Is engine state already an exact branch substrate? | Yes. `Game::clone`, selected full-clone runtime, structured branch actions, and representation-neutral witnesses provide a correctness reference. | Do not make a faster backend prerequisite. Optimize behind witness equivalence only when measured cost requires it. |
| Is an action index a canonical action identifier? | No. It is scoped to one ordered action list and decision epoch. Cross-hand belief updates require content-based identity, while live execution requires the private binding. | Preserve revision-scoped commands now; introduce canonical public action identity only for cross-state/history matching, never as a replacement for engine legality. |
| Is current PUCT information-set-consistent? | No. It builds one perfect-information tree per determination and combines root statistics. Prior work distinguishes this from information-set trees, and determinization permits strategy fusion. | Name it determinized PUCT everywhere. Treat belief sampling and information-set planning as separate interfaces and measurements. |
| Does exact range sampling solve strategy fusion? | No. Better posteriors improve which worlds are searched; they do not stop future choices inside each world from using hidden information. | INT-9 is a `WorldSampler` improvement. Do not use its calibration result as a planning-equilibrium claim. |
| Can the policy ABI silently lose legality? | Yes. Fixed-cap encoding can truncate action/object rows, and Python/Rust duplicate the encoder. Dataset generation catches the selected-action case but the contract is not universal. | Make encoding receipts and legality-affecting truncation a hard failure; converge on one Rust ABI implementation. |
| Are current shards/checkpoints intrinsically immutable and self-identifying? | No. Atomic NPZ writes avoid tears, while digest binding, cumulative caps, and fail-closed identity live in the production envelope. Loaders remain permissive. | Promote the manifest/receipt pattern into shared artifact loading instead of relying on runner conventions. |
| Can the existing INT-4 smoke contract be reused as the next current-system demo? | No. On 2026-07-17 its expected engine source digest is `60fb…e85`, while the current branch reports `6acc…e03`; its expected extension digest is `7267…b7c`, while the locally rebuilt pinned extension reports `18d0…504`. The runner rejects the mismatch as designed. | Never edit or bless the frozen contract in place. A new build registers a new contract against the current world/runtime before generation and keeps prior evidence immutable. |
| Is current evaluation a versioned skill arena? | No. It alternates seats on different deals and reports pairwise Wilson intervals; it has no closed cohort, typed registration, rating, or promotion contract. | Keep it as a development harness. Use the INT-6 paired-deal, world-pinned arena as the admission boundary. |
| Can Bradley–Terry always return a finite honest rating? | No. Dominance separation and disconnected comparison graphs can make unregularized MLE nonexistent or unidentifiable. | Pin a regularized estimator/prior in `ArenaKey`, expose the matrix/connectivity/residuals, and bootstrap paired deal blocks. |
| Does structurally valid Study evidence answer any historical replay? | No. Evidence is specific to exact pack/engine/model/checkpoint/state/viewer/search/world/seed context. Missing frozen bytes currently block production evidence. | Make exact-context resolution a first-class request and return typed unavailability. Never substitute another replay's fixture. |
| Is a naive public-belief/public-policy reduction automatically licensed in two-player zero-sum play? | No. Published counterexamples and regularization conditions show that reduced-game equilibria need not map to original-game equilibria. | A future public-belief solver must state and test its representation/regularization conditions; this review does not bless a generic reduction. |

## Alternatives considered

| Approach | Tradeoff | Why not |
| --- | --- | --- |
| Document only the desired end state | Clean and aspirational, with little legacy detail. | Builders would still misread current paths and in-flight work as the desired contract. The task explicitly requires a current-system map first. |
| Create one universal `Decision`/`Artifact`/`Player` schema across all layers | Fewer type names and apparently simple serialization. | It collapses distinct authority domains: native transaction vs wire command, audit trace vs training shard, scalar value vs counterfactual value, pairwise player vs admitted registration. Type separation is safety here. |
| Make experiment runners the permanent architecture | Already contains the strongest provenance and cap checks. | Runners compose a registered experiment; they should not become the reusable domain model or force every local iteration through production orchestration. Extract shared manifest/receipt types instead. |
| Promote action positions to global canonical IDs | Avoids adapter maps and simplifies logs. | The same position changes meaning across revisions and worlds. Canonical semantic action IDs help beliefs/history matching, but only a revision-bound private map can authorize execution. |
| Replace full clone with undo/COW before further search work | Potential throughput/RSS gain. | Full clone already fits the correctness boundary and current bottleneck includes inference. The wave explicitly accepts full clone when it meets measured budget. Optimize only after profiling the running planner. |
| Use win rate plus Elo as the promotion rule | Familiar and easy to summarize. | Opponent choice, seat/deal effects, sparse matrices, world drift, competency failures, and compute mismatch can all produce a persuasive but false scalar. |

## Key decisions

### 1. Keep four identity planes separate

- **Rules identity:** pack, engine/source, game state, object generations,
  decision epoch, legal action space.
- **Information identity:** historical viewer, observation ABI, public history,
  world-sampler/belief model, sampled-world set.
- **Learning identity:** dataset bytes/manifest, target semantics, training seed,
  model schema, checkpoint bytes.
- **Evaluation/evidence identity:** player registration, arena key/cohort/compute,
  replay address, Study identity.

An end-to-end manifest composes these planes. It does not replace their local
types with a giant string fingerprint.

### 2. Standardize receipts, not implementations

Do not force Etude, search, training, and the arena through one runtime object.
Require each boundary to emit a small immutable receipt that binds inputs,
outputs, identity, and validation result. This preserves specialized hot paths
while making provenance composable.

The minimum receipt family is:

- `DecisionReceipt`: state/revision/viewer/offer/played-command/legal result;
- `BranchReceipt`: root witness, driver, sample seed/world digest, transition;
- `SearchReceipt`: planner/evaluator/budget, alternatives, action, latency/cost;
- `ArtifactReceipt`: manifest digest, byte digest, schema/context validation;
- `MatchReceipt`: arena/player/deal/seat/commands/outcome/systems measurements;
- `EvidenceReceipt`: exact Study request identity and available/unavailable
  quantities.

### 3. Make current truth visible before consolidation

The first implementation following this review should not start with a
repository-wide abstraction rewrite. It should add characterization tests and
typed receipts around the selected path, then delete duplicate adapters one at
a time. A consolidating change is done only when the old constructor is gone;
dual “temporary” truth paths are not an acceptable steady state.

### 4. Preserve planner plurality behind one narrow boundary

`Planner.plan(root, viewer_history, world_sampler, leaf_evaluator, budget) ->
SearchResult` is the desired conceptual seam. Determinized flat MC,
determinized PUCT, ISMCTS, and a future public-belief/CFR solver may implement
it, but the output declares algorithm-specific semantics. The interface does
not pretend their visits, values, or budgets are automatically comparable.

### 5. Treat the arena as the only promotion authority

Development harnesses may iterate quickly. Only a closed, versioned arena may
admit a candidate. No experiment report, one-off pairwise win rate, or Study
artifact silently changes the admitted cohort.

### 6. Put the durable map in architecture docs after review

This scratch document is the review surface. Once the human accepts the
boundaries, move the edited current map and laws to
`docs/architecture/search-learning.md`, then link it from `manabot/README.md`
and the docs index. Keep experiment results in `experiments/`, strategic
direction in `wave/intelligence/`, and session assumptions in `scratch/`.

## Architecture decision receipt

| Field | Decision |
| --- | --- |
| Receipt ID | `INT-10-architecture-v1` |
| Date | 2026-07-17 |
| Status | Proposed for human review; no implementation authorized by this kickoff |
| Decision | Keep managym as the single state/legality/viewer/fork authority; keep manabot as the planner/learning/evaluation authority; keep Etude as the historical replay and Study authority. Connect them with small typed receipts and exact artifact manifests, not shared mutable objects or reconstructed meaning. |
| Selected seams | Revision-bound decision/command; viewer projection plus encoding receipt; authoritative branch plus world sampler; planner plus typed result; trajectory and shard as distinct artifacts; checkpoint manifest; arena-local player registration; exact-context Study request/evidence. |
| Planner posture | Current planner is named `determinized PUCT`. Belief-weighted worlds are a sampler change. Information-set search is a different planner family and must earn its own claims. |
| Evaluation posture | Current `play_games` remains a non-admission pairwise harness. The in-flight INT-6 `ArenaKey` boundary becomes the sole admission authority only after it lands and is reviewed. |
| Artifact posture | Frozen contracts and evidence are immutable. A runtime mismatch creates a new contract/version; missing exact bytes create typed unavailability. Neither is repaired by changing a digest or substituting another artifact. |
| Consolidation order | Characterize the selected path, add receipts, migrate one consumer at a time, and delete each duplicate constructor before starting the next. Do not create a permanent compatibility layer. |
| Durable documentation | After human acceptance, promote the edited map to `docs/architecture/search-learning.md` and link it from `manabot/README.md` and the docs index. |
| Explicit non-decisions | No branch-backend replacement, new planner, model architecture, arena implementation, Study UI, portfolio expansion, production run, or checkpoint recovery in INT-10. |

### Next computable end-to-end build

Build the **authority-receipted self-starting visit iteration**. It is the
smallest real loop that exercises the chosen architecture without the missing
Teacher-0 checkpoint bytes:

1. Register a new immutable smoke contract for the current w2 engine,
   observation/action ABI, content, selected matchup, runner source, and hard
   cap. Do not modify the frozen INT-4 contract.
2. Start with the current uniform-prior/random-leaf determinized PUCT teacher;
   it needs no learned checkpoint. Every played decision originates from one
   managym-produced `DecisionReceipt` binding root witness, historical viewer,
   revision, legal offers, structured command, and post-state receipt.
3. Record exact teacher trajectories and derive visit/chosen-action plus
   policy-only/policy-value shards. Each shard row carries the digest of its
   source decision receipt; replay proves the root and sampled search statistics
   before training begins.
4. Train new students only from artifacts produced inside this run. Their
   checkpoint manifests bind shard bytes, target semantics, initialization,
   training seed, ABI/world, source/runtime, and measured inference profile.
5. Play teacher, new visit-policy student, new visit-policy/value student, and
   student-plus-search through the current pairwise harness. Label the output
   `engineering_smoke_non_admission`; do not duplicate or anticipate INT-6's
   rating/promotion implementation.
6. Export one Study artifact from an exact decision in the same run. The Study
   artifact references the same decision, search, dataset, and checkpoint
   receipt chain and exposes no private sampled-world payload.
7. Run verify-only mode with generation disabled. It checks exact trajectory
   replay, artifact bytes, receipt links, legality, viewer equivalence, and
   Study validation and fails if any input is absent or changed.

The proposed developer demo is one new contract and one command, following the
existing runner shape:

```bash
uv run experiments/runners/run_visit_teacher_iteration.py \
  --contract experiments/contracts/int-10-authority-receipt-smoke-v1.json \
  --profile smoke --out-dir .runs/int-10-authority-receipt-smoke-v1
```

The command is illustrative until the architecture review is accepted: the new
contract and receipt wiring do not exist in this kickoff. The build is complete
when the command produces a linked teacher → shards → students → non-admission
matchups → Study bundle, and the same command with `--verify` performs no
generation and reports zero legality, replay, viewer, digest, or receipt-link
mismatches. This is an end-to-end working system, not a prerequisite kata. It
leaves the unavailable frozen incumbents for a later registered admission run.

## Wild success

Six months after adoption, a builder can add a new graph policy, belief sampler,
or information-set planner without modifying managym legality or Etude Study.
They register exact identities, implement one narrow interface, and immediately
receive replay, matched arena, and Study compatibility. When a result moves,
the matrix makes the reason legible: policy strength, competency, belief
calibration, leaf cost, or information consistency—not a mystery “agent
version.” The surprising win is that research moves faster because integrity
checks become reusable data types instead of bespoke production ceremonies.

## Wild failure

The map becomes decorative while code grows new parallel adapters. “Temporary”
dict specs turn into public APIs, paths continue to stand in for immutable
artifacts, and a faster branch driver bypasses structured commands. Ratings are
quoted outside their arena, uniform belief calibration is mistaken for an
information-set solution, and Study eventually displays confident evidence
from a different replay. The root cause would be treating this as a naming
exercise rather than deleting duplicate authorities as each boundary is
implemented.

## Scope

- In scope: a builder-facing map of current managym authority and identity;
  viewer-safe observations and encoding; branch drivers, determinization, and
  belief sampling; PUCT and leaf evaluators; search evidence and teacher
  targets; immutable shard/checkpoint provenance; policy/value students;
  players, paired evaluation, ratings, and promotion; Study evidence; duplicate
  abstractions; missing contracts; and the recommended durable doc location.
- In scope: explicit separation of current code, INT-6/INT-9 in-flight work,
  and desired architecture.
- Out of scope: implementing any architectural change before human review;
  changing Rules or Game authority; running production generation; claiming
  missing checkpoints or evidence; adding cards/mechanics; deck construction,
  drafting, Commander, multiplayer/general-sum play, or hidden decklists.

## Done when

This kickoff is complete when:

1. every central boundary in the directive is represented in the system map;
2. each boundary states ownership, identity, mutation, legality, information
   hiding, replay/determinism, and performance laws;
3. all seven major problems have a current trace, gap, and desired disposition;
4. current, in-flight, and desired behavior cannot be confused;
5. exact replay/checkpoint unavailability and the determinized-PUCT information
   limit are explicit fail-closed boundaries;
6. a builder can validate the live substrate with:

   ```bash
   uv run --extra dev pytest tests/sim/test_mcts.py tests/sim/test_distill.py \
     tests/sim/test_teacher1_evidence.py tests/sim/test_study_evidence.py -q
   cargo test --manifest-path managym/Cargo.toml --test search_state_contract \
     --test search_tests
   ```

The design advances the Intelligence measures that search teachers/students be
compared at explicit compute budgets with legality, competency, strength,
calibration, latency, throughput, cost, and uncertainty; that admitted agents
enter a world-pinned paired arena; and that evidence replay through Study
without hidden-information leakage or invented meaning.

## Measure

This documentation slice does not claim a performance improvement. The
implementation it specifies must preserve or add the following measurements at
the boundary that owns them:

| Boundary | Required measurements |
| --- | --- |
| Observation/encoding | original and encoded ragged counts, truncation status, ABI digest, encoded decisions/sec |
| Branching/world sampling | fork/determinize/apply p50/p95, branches/sec, peak RSS, witness equivalence, support/calibration error |
| Search | declared semantic budget, realized worlds/traversals/playouts, p50/p95 decision latency, rollout throughput, root legality/replay mismatches |
| Teacher/shards | labels/sec, decisions and games, byte size, target coverage, label cost, manifest and content digests |
| Student | training wall time/cost, validation policy/value metrics by target type, inference p50/p95 and throughput, independent training seeds |
| Arena | full paired-deal matrix, order effect, arena-local rating and uncertainty, connectivity/residuals, legality, competencies, calibration, systems cost, promotion decision |
| Study | exact-context resolution, field availability, alternative coverage, information leak checks, replay/evidence mismatches |
