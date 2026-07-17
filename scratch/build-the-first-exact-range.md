# Build the first exact-range belief-aware player

## Problem

Uniform determinization throws away the information in an opponent's public
actions. INT-9 must establish whether that information improves a runnable
manabot, rather than merely producing a plausible posterior plot. The player
must therefore maintain its range from exactly the facts available to one
fixed viewer, sample only history-compatible worlds, choose an authoritative
legal action, and be compared with a uniform-range player that differs only in
the sampling distribution used by search.

The selected boundary is the symmetric world-w2 `INTERACTIVE_DECK` matchup
already frozen by the Teacher-1 contract: 60 cards, ten definitions, and
10,832 possible opening-hand count vectors. The first player is not a
public-belief solver. It remains determinization search and may still suffer
strategy fusion; the claim is only whether better state inference improves
actual play at the same search budget.

## The demo

Run:

```bash
uv run experiments/runners/run_exact_range_player.py \
  --contract experiments/contracts/int-9-exact-range-v1.json \
  --stage smoke --out-dir .runs/int-9-exact-range-v1
```

The smoke stage plays the belief and uniform arms through normal managym
commands, then writes a replayable receipt showing zero illegal commands or
viewer leaks, an exactly normalized range, belief samples compatible with the
viewer's complete history, paired game results, calibration/log loss,
effective range size, p50/p95 latency, rollout throughput, and peak memory. It
is the developer demo and integrity gate, not the final arena evidence.

## Approach

### 1. Freeze one executable comparison

`experiments/contracts/int-9-exact-range-v1.json` pins world `w2`, the symmetric
`INTERACTIVE_DECK`, content/action/observation/protocol hashes, the action
likelihood checkpoint and SHA-256, epsilon, search worlds and rollouts, deal
blocks, arena opponents, competency runs, host, caps, and all evidence paths.
It contains separate bounded `smoke` and registered `arena` stages. The arena
stage and its decision thresholds are frozen before any arena result is read.
The real likelihood checkpoint must be recovered byte-for-byte and hash
matched; a reconstructed or convenient substitute is not evidence. Toy
likelihoods exist only in unit tests.
The primary pair is:

- `belief`: exact posterior samples after public-action and chance updates;
- `uniform`: the same tracker, likelihood inference, canonicalization,
  determinization function, random seeds, rollouts, and command path, but its
  search sampler uses the exact combinatorial range implied only by the current
  public zones and hand size.

Both arms still compute the diagnostic posterior. This deliberately spends the
same likelihood cost in the control and makes the search sampling distribution
the only implementation difference. The uniform samples are compatible with
the current public snapshot but intentionally forget earlier public actions;
only the belief arm claims compatibility with the complete viewer history.

### 2. Represent the exact range over indistinguishable copies

Add `manabot/belief/range.py`. A `HandKey` is a tuple of counts in the contract's
sorted `CardDefId` order. A range is a sorted sparse support plus float64 log
mass. Physical copies with the same definition are exchangeable to the selected
rules and action model, so one hand key carries the exact multiplicity

```text
product(comb(unseen_count[i], hand_count[i]))
```

rather than treating all count vectors as equally likely. Normalization uses
log-sum-exp; impossible support is a hard error. No pruning, top-k, particles,
or silent support cap is allowed. Memory and support size are recorded at every
update.

At every public commitment, action evidence is applied to the pre-commitment
range before the resulting zone/chance transitions. The exact transitions are:

- hidden draw of definition `c`:
  `P(h + e_c) += P(h) * (unseen[c] - h[c]) / library_size`;
- a publicly identified card leaving the opponent hand: condition on
  `h[c] > 0`, then map `h -> h - e_c`;
- a publicly identified card returning to the opponent hand: map
  `h -> h + e_c`;
- opponent public action `a`: multiply by
  `sigma_tilde(a|h) = (1-epsilon)*sigma(a|h) +
  epsilon/|legal_canonical_actions(h)|` when `a` is legal, while an illegal
  action remains a logical zero.

Unknown draws and known zone moves are derived from consecutive fixed-viewer
snapshots plus a viewer-safe public event projection. The current matchup
contains no hidden discard, scry, shuffle, or hidden decklist operation;
encountering one fails closed as an unsupported transition instead of guessing.

### 3. Make public action identity independent of engine prompts

Add `manabot/belief/history.py`. `PublicAction` contains only facts available
after one semantic commitment: verb, source `CardDefId` when revealed, public
target object render IDs, and attacker/blocker declarations. A
`PublicActionBundle` also records the viewer-safe before/after snapshots. Any
raw prompt transcript stays in the private authority audit and is neither a
bundle field nor tracker input.

This distinction is required because the legacy action ABI splits one public
choice across private engine prompts. Targeted casting chooses a source and
then a target; attacker and blocker declarations are assembled one object at a
time. The tracker must not treat those raw indices or intermediate candidates
as public observations. The trusted match adapter accumulates them, emits one
bundle only when the choice has become public, and proves that the emitted
bundle is identical for authority roots that differ only in viewer-hidden
state. Passes and already-atomic public moves emit immediately.

managym owns the selected-matchup projection from accepted action fragments to
stable `CardDefId`/`ObjectRenderId` semantics; Python owns only bundle assembly
and Bayesian updates. This avoids deriving public identity from labels or
physical `CardId`s.

For likelihood, every raw prompt path that yields the same `PublicAction`
collapses to one key and its probability mass is summed. The evaluator
enumerates matching legal paths from the public action; it does not receive the
authority path that happened in the retained game. This handles both
physical duplicate copies and latent split-prompt paths without turning
revision-local offer IDs into Bayesian evidence. The selected move still
travels through the existing protocol-v1 `ExperienceFrame` and `Command`
builders; the command executor validates match, revision, prompt, and offer
before calling normal authoritative `Env.step`. Public actions are evidence
records only and never mutate game state.

### 4. Pin a real likelihood model and sweep counterfactual hands

Add `manabot/belief/likelihood.py`. `FrozenPolicyLikelihood` loads the contract's
immutable w2 policy/value checkpoint. At each opponent public commitment it:

1. constructs a counterfactual authoritative clone for every supported hand;
2. enumerates the legal engine prompt paths in that clone whose viewer-safe
   projection equals the observed public action;
3. batches the existing observation encoder and frozen policy forward passes;
4. multiplies probabilities along each path and sums all paths that produce the
   observed `PublicAction`;
5. returns the joint public-action probability and the number of legal public
   actions for the epsilon mixture.

The actual hidden hand is never passed to this interface. A trusted engine
clone is overwritten with the requested hand key before the opponent
observation is constructed. Inference streams support-sized batches rather
than materializing all counterfactual observations, but every hypothesis is
evaluated: batching is not pruning. The checkpoint, temperature, deterministic
preprocessing, epsilon, and batch size are contract fields.

Count-vector state is admitted only if two invariance tests pass: choosing
different physical copies for one hand key must produce the same grouped
public-action probabilities, and viewer-equivalent authority roots must
produce the same range update. Otherwise physical copies are not exchangeable
for this likelihood and the run fails rather than claiming an exact posterior.

Policy-based inference estimates state reach by multiplying the likelihoods of
the observed opponent actions. This is the same mechanism reported to improve
both inference and determinized play in Skat
([Rebstock et al.](https://arxiv.org/abs/1905.10911)). The design retains its
known risk: a mismatched opponent model can make the resulting player more
exploitable, so calibration and the full opponent matrix are reported rather
than treating the posterior as truth.

### 5. Add exact-hand determinization to the engine

Extend `Game::determinize` with an exact-hand sibling and expose it through
`managym.Env`:

```rust
pub fn determinize_to_hand(
    &mut self,
    perspective: PlayerId,
    hand: &[(CardDefId, usize)],
    seed: u64,
) -> Result<(), AgentError>
```

It validates the requested count sum against the current opponent hand size,
validates every count against the opponent's hand-plus-library pool, chooses
physical copies deterministically under the supplied seed, installs exactly
that hand, shuffles the remainder and the viewer's library, and re-pins any
currently revealed library cards. The Python binding accepts sorted
`(card_def_id, count)` pairs. Public state and the fixed-viewer projection must
remain unchanged. At a search root, where the perspective owns the decision,
the legal action space must also remain byte-identical. At an opponent
likelihood root, priority legality must instead be authoritatively recomputed
from the installed counterfactual hand so stale actions from the true hand can
never enter the model. Other hand-dependent prompt kinds fail closed unless
explicitly covered.

Add a flat-MC entry point accepting already sampled hand keys and their seeds.
For every hand, all root actions share the same world and rollout random
streams, matching the current common-random-number behavior. It returns scores,
total playouts, cap hits, and the ordered installed-hand digests. Uniform and
belief players call this one entry point; neither arm may resample inside a
different search implementation.

### 6. Run one stateful player through the existing match loop

Add `manabot/belief/player.py`. `ExactRangePlayer` owns one tracker for the
opponent of its seat. Extend the matchup-player protocol with `start_game` and
`observe_public_transition` lifecycle hooks. The trusted match adapter is
invoked after every accepted command, but the player hook receives a bundle
only after a viewer-observable commitment. It contains only:

- the player's fixed-viewer observation before and after the commitment;
- the public action and actor;
- public definition/zone changes and hidden-zone count deltas.

On the player's turn, it samples hand keys from either the posterior or uniform
control using common seeds, calls exact-hand flat MC, picks deterministic
argmax with the existing tie rule, wraps the selected offer in a protocol
`Command`, and lets the command executor apply it. Reset starts a new exact
range; swapping seats starts a tracker for the other opponent. Belief-update,
search-only, command, and end-to-end time are recorded separately.

### 7. Treat posterior quality as mechanism evidence, not the claim

Known-truth replay games retain the authority witness in a separate audit
bundle. Replaying the tracker consumes only the viewer projection and public
action bundles, then scores:

- exact hand-composition negative log likelihood before each opponent action;
- per-card inclusion reliability, Brier score, and ECE;
- top-hand mass and true-hand rank;
- `effective_range_size = 1 / sum(p_h**2)` and its support-normalized value;
- sampled unique-world count and collision rate.

The receipt calls this effective range size, not evaluation ESS: it measures
the number of materially weighted exact-hand hypotheses, not independent games
or rollout samples.

### 8. Arena and ablations

Arena `w2-interactive-belief-v1` includes a direct belief-versus-uniform cell
and fits the repo's specified batch Bradley-Terry model, expressed on the Elo
scale with the random anchor fixed at zero. Every cell uses paired deal seeds
and reports block-bootstrap uncertainty, the full payoff matrix, residuals,
and native cost.

Both primary arms face random, deterministic policy-only checkpoints, flat
search, the frozen likelihood checkpoint itself, and any independent frozen
learned checkpoint named by the registered contract. Fixed scripted
competencies run for both arms on the same seeds and appear beside the arena
matrix. Optional Teacher-1 cells may be additive, but cannot replace a required
opponent slot. The contract fails closed rather than substituting a missing
artifact.

Three diagnostic arms use the same command/search path:

- `neutral-action`: logical legality and public card movement only;
- `reset-on-draw`: applies action likelihoods but resets to the exact
  combinatorial public range after each hidden draw, measuring whether action
  information survives chance events;
- `uniform`: full posterior still computed for diagnostics, but search samples
  the public-only combinatorial range.

Gameplay determines the result. Diagnostics explain it. If the paired belief
versus uniform interval is ambiguous, the runner writes—but does not execute—a
preregistration for the smallest information-by-continuation scenario slice.
No microgame is added in this task unless that slice remains ambiguous.

The registered decision rule is explicit: claim improved play only when the
paired bootstrap lower bound for belief-minus-uniform play and the rating
difference lower bound are both above zero, every integrity gate passes, raw
playout counts are reported, and the preregistered compute-class tolerances
hold. Worlds per action and rollouts per world match exactly; realized total
playouts need not match after the arms choose different actions and visit
different numbers of legal roots.
Otherwise report non-improvement or ambiguity without promoting posterior
quality into a gameplay claim.

### 9. Implement in evidence-bearing slices

This design is larger than one commit and may require serial INT-9 PRs, but it
does not need another diagnostic Task before a player exists:

1. exact range algebra, brute-force oracles, exact-hand determinization, and
   conditional action-space refresh;
2. public-action bundling, grouped frozen-policy likelihood, and viewer-safe
   replay/calibration;
3. the stateful Command-emitting player, matched uniform arm, and smoke
   receipt;
4. registered ablations, competencies, arena, uncertainty, and the mechanism
   decision.

An earlier substrate slice is useful engineering evidence but does not complete
INT-9. Completion requires the registered arena receipt or an explicit measured
failure of that registered run; a fixture model, a posterior plot, or smoke
alone cannot satisfy the Task.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|-----------------|
| Is the opening exact range tractable? | The selected deck has 10,832 seven-card count vectors, while 386,206,920 physical seven-card subsets collapse into them. Support grows with hand size (22,191 at eight; 75,820 at ten), so silent pruning would invalidate exactness. | Store count vectors with combinatorial mass, use float64 log weights, record support/memory, and fail closed rather than prune. |
| Can current determinization install a posterior sample? | `Game::determinize` only uniformly reshuffles the opponent hand-plus-library pool. Scenario injection is explicitly non-authoritative test machinery. | Add a validated exact-hand determinization primitive and make both arms use it. |
| Can one installed-hand action space serve search and likelihood? | No. Search changes only the non-acting opponent hand, so root actions must stay fixed; likelihood changes the acting player's hand, so priority actions must be rebuilt. | Preserve actions at search roots, refresh them at opponent priority roots, and test that no true-hand `CardId` survives. |
| Can the tracker get the opponent's model view without reading its hand? | `Env.observation_for_player` already produces fixed-viewer projections, and a clone can expose the hypothetical opponent hand after exact determinization. | Counterfactual likelihoods come from public root + hand key; the actual hidden hand remains audit-only. |
| Are action indices stable Bayesian identities? | No. Offer/action positions are revision-local, duplicate card copies may produce equivalent actions, and cast/combat choices span several prompts. | Group complete prompt paths by one viewer-observable public action and sum their probability mass. |
| Does the engine already provide complete structured Commands? | The experimental atomic structured bridge covers pass, one-target casts, and attacker sets only. The production protocol/Teacher-1 evidence already wraps every positional legal action in a revision-bound `Command` and replays it authoritatively. | Reuse the complete protocol command envelope and a validating command executor; do not broaden the experimental atomic decoder. |
| Can fixed-viewer snapshot differences recover chance events? | In the selected matchup, hidden hand-count deltas plus public definition-count deltas distinguish hidden draws, known hand-to-public moves, and Man-o'-War returns. There are no hidden discards, scry, or shuffles. | Implement a selected-matchup transition extractor with fail-closed unsupported cases and event-order tests. |
| Can raw opponent commands be used as history? | No. Targeted casts and combat declarations expose private intermediate choices before the semantic action becomes public. | The trusted adapter groups prompts and emits only a public bundle proven invariant across viewer-equivalent roots. |
| Does better inference make determinized search information-set-consistent? | No. Determinization remains exposed to strategy fusion; EPIMC explicitly targets that separate failure mode ([Arjonilla et al.](https://arxiv.org/abs/2408.02380)). | Limit the claim to matched-compute play improvement and exclude public-belief solving. |
| Can this design claim a public-belief equilibrium? | No. Naive public-policy reductions in two-player zero-sum games need additional regularization/representation conditions ([Sokota et al.](https://proceedings.mlr.press/v202/sokota23a.html)). | Do not add solving, continual resolving, or equilibrium language to INT-9. |
| What prevents the model from annihilating the true hand? | Behavioral zeroes from a misspecified checkpoint are not logical impossibilities. | Use a pinned epsilon mixture for legal actions; keep illegal actions at exact zero and report surprise/log loss. |
| What makes the strength comparison causal? | Equal sims integers alone can conceal different code paths and overhead. | Both primary arms compute the same posterior and call the same sampled-hand search with paired seeds; report search-only and end-to-end latency separately. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Particle filter over physical worlds | Bounded memory and easy online sampling, but only approximate, subject to particle death, and unable to meet the exact-normalization KR. | Exact count-vector support is tractable at the selected boundary and gives a stronger correctness oracle. |
| Marginal independent card probabilities | Very cheap and scalable, but cannot represent mutually exclusive hand compositions and misprices multi-card evidence. | It repeats the independence weakness documented by policy-inference prior work and cannot install an exact hand. |
| Handcrafted action propensities | Fast and easy to pin, but duplicates engine legality and makes calibration depend on arbitrary coefficients. | The existing frozen w2 policy can supply a genuine normalized likelihood over authoritative legal actions. |
| Weight rollout returns after uniform sampling | Avoids exact-hand installation changes, but high-variance importance weights can collapse and make the effective search budget differ between arms. | Sample directly from the exact posterior, keep every rollout equally weighted, and report effective range size separately. |
| Public-belief MCTS/CFR now | Can address strategy fusion, but changes both beliefs and the planner and requires mixed-strategy/value machinery. | It destroys the causal first comparison and is explicitly outside INT-9. |
| New exact microgame first | Supplies ground truth but does not build a selected-matchup player. | The wave requires integrated play first; diagnostics are conditional on an ambiguous gameplay result. |

## Key decisions

1. **The matchup is symmetric w2 `INTERACTIVE_DECK`.** It reuses current
   teacher/search artifacts and keeps deck strength out of the comparison.
2. **Exact means normalized count-vector posterior with combinatorial copy
   mass.** No pruning or particle approximation is permitted.
3. **Viewer-safe public history is the only tracker input.** Authority truth is
   retained only for post-hoc scoring.
4. **Public semantic actions, not raw prompts, are observations.** Joint path
   probability sums positional duplicates and split-prompt paths, then uses a
   pinned legal-public-action epsilon floor.
5. **The action model is a byte-identical frozen w2 policy checkpoint.** A
   missing or hash-mismatched checkpoint blocks evidence rather than inviting
   substitution.
6. **Posterior sampling changes; search does not.** Uniform and belief arms use
   the same exact-hand flat-MC implementation, budgets, likelihood work, and
   seeds.
7. **Commands remain authoritative.** The player selects an offer; protocol
   `Command` validation and managym execution decide legality.
8. **Strength is primary.** Calibration, log loss, effective range size, and
   surprise explain a win-rate/rating result but cannot substitute for it.
9. **The result is not an equilibrium or anti-fusion claim.** Public-belief
   solving is the later project KR, not this task.

### Wild success

The belief player gains rating specifically against frozen learned/search
opponents while remaining neutral against random, its true-hand log loss and
card reliability improve as actions accumulate, and competencies involving
held interaction move without a latency-class regression. A replay lets a
developer inspect exactly which public pass or cast shifted the range and then
reproduce the sampled worlds and command.

### Wild failure

The checkpoint is a poor opponent model, confidently deletes the true hand
without the epsilon floor, and belief search overfits one opponent while losing
arena rating. Or exact support expansion makes every update slower than search,
so the control wins only because it ignores inference cost. The pinned full
matrix, log loss, surprise, support/memory curve, native/end-to-end latency, and
uniform same-path control make both failures explicit rather than allowing a
posterior visualization to masquerade as progress.

## Scope

- In scope: symmetric w2 interactive matchup; exact opponent hand ranges;
  combinatorial prior; canonical opponent-action Bayes updates; hidden draws;
  known public-to-hand and hand-to-public moves; pinned checkpoint likelihood;
  exact-hand determinization; likelihood-weighted flat search; matched uniform
  control; protocol Commands; replay, legality, leakage, calibration,
  effective range size, competencies, arena, latency, throughput, and memory
  evidence.
- Out of scope: public-belief search, CFR, continual resolving, mixed-strategy
  training, range-conditioned values, opponent adaptation, hidden decklists,
  broad card/deck generalization, UR Lessons versus GW Allies, deck building,
  microgames before an ambiguous integrated result, and any Study UI changes.

## Done when

The following focused checks pass:

```bash
uv run pytest tests/belief tests/sim/test_exact_range_player.py
cd managym && cargo fmt --check
cd managym && cargo clippy --all-targets --all-features -- -D warnings
cd managym && cargo test
```

The smoke command above must produce a verified integrity receipt. Task
completion additionally runs the frozen arena stage:

```bash
uv run experiments/runners/run_exact_range_player.py \
  --contract experiments/contracts/int-9-exact-range-v1.json \
  --stage arena --out-dir .runs/int-9-exact-range-v1
```

Together the verified receipts contain:

- exact normalization after every update and correct brute-force results on
  tiny decks;
- exact-hand samples matching requested count vectors, preserving every
  fixed-viewer public field, preserving search-root actions, and rebuilding
  opponent priority actions without stale true-hand identities;
- zero illegal Commands, replay mismatches, unsupported selected-matchup
  transitions, raw-prompt exposures, or viewer-equivalence differences;
- equal grouped-action likelihoods across physical-copy assignments for every
  audited hand key;
- both primary arms executing identical configured worlds and rollouts per
  legal root action through the same native search entry point under paired
  seeds, with realized total playouts reported;
- reported calibration/log loss, effective range size, competencies, p50/p95
  latency, rollout throughput, peak RSS/range bytes, and full matchup rows.

This advances the wave measures that a complete player act in real managym
positions, that search/player comparisons report legality, calibration,
latency, throughput, uncertainty and cost, and that belief-aware play enter a
world-pinned arena through normal Commands.

## Measure

Before the arena, run the contract's fixed synthetic and replay corpus to lock:

- normalization error and true-hand retention;
- posterior versus uniform exact-hand sampling frequencies;
- viewer-equivalence state/action/range digests;
- likelihood batch observations/second and updates/second by support size;
- exact-hand determinization and rollout throughput;
- p50/p95 likelihood-update, search-only, command, and end-to-end latency;
- peak RSS, serialized range bytes, and support count.

The arena reports belief-minus-uniform paired win difference and its paired
deal block-bootstrap interval, Bradley-Terry/Elo difference and uncertainty,
every matchup cell, per-seat splits, competencies, legality/replay/leakage
counts, and all cost metrics. A positive belief claim requires the
pre-registered rating/win criterion at the same search budget without an
integrity failure or compute-class regression; posterior metrics only explain
the outcome.
