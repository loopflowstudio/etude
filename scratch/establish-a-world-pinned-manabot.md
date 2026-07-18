# Establish a World-Pinned Manabot Skill Arena

## Problem

Etude Fantasia has runnable random, learned, flat-search, and visit-search
players, but it does not yet have a stable hill-climbing instrument. Existing
experiment runners answer local questions with pairwise win rates. They do not
place every runnable player on one world-pinned scale, preserve a complete
payoff matrix, or distinguish a replayable rating from an eligible promotion
decision.

INT-6 establishes that instrument for Intelligence. A developer training a new
manabot should be able to register immutable bytes, run one command, and place
it against a stable reference scale without silently changing the world,
matchup, opponents, deal distribution, viewer boundary, or statistical rule.
If the cohort contains an exact same-compute incumbent, the same artifact can
make a promotion decision; if it does not, the result remains a useful rating
with a typed not-eligible disposition. A reviewer should be able to verify
either result without generating another game.

Arena v1 deliberately measures play skill in one symmetric selected matchup:
the w2 `INTERACTIVE_DECK` mirror already used by the Search Teacher and Student
Arena. It is not a format rating, equilibrium claim, or training league.

## The demo

The developer runs:

```bash
# One-time arena-v1 code-only anchor freeze.
uv run experiments/runners/run_skill_arena.py freeze-anchors \
  --contract experiments/contracts/int-6-skill-arena-v1.json \
  --out-dir .runs/int-6-skill-arena-v1/anchors

# Repeatable candidate entry against the frozen anchor evidence.
uv run experiments/runners/run_skill_arena.py challenge \
  --contract experiments/contracts/int-6-skill-arena-v1.json \
  --anchor-artifact .runs/int-6-skill-arena-v1/anchors/manifest.json \
  --candidate experiments/candidates/<candidate>.json \
  --candidate-checkpoint /absolute/path/to/candidate.pt \
  --out-dir .runs/int-6-skill-arena-v1/<candidate>
```

The command finishes with a report headed by the candidate's Elo coordinate and
promotion disposition. Against the base code-only cohort the disposition is
`rated_not_promotion_eligible`, with typed reason
`exact_incumbent_unavailable`; this is a valid production rating, not a smoke
result or integrity failure. The report also shows the full observed/expected
payoff matrix, largest Bradley-Terry residuals, S1-S5 competencies, legality and
exact replay receipts, matched-root p50/p95 latency, throughput, and isolated
RSS. Every game row links to an immutable Command trace. Re-running the same
command with `--verify` reads and replays the frozen artifacts without playing a
new game or invoking a player.

Candidate entry never reruns anchor-versus-anchor games: it binds the frozen
anchor artifact by digest, evaluates the challenger against each anchor on the
same registered deal blocks, and refits the combined population model. If a
future cohort contract registers exact learned incumbent bytes, the challenge
also profiles incumbent and challenger contemporaneously and applies the same
promotion rule. Adding that anchor creates a new cohort identity and anchor
artifact; it never appends to or rewrites the base v1 cohort.

## Approach

### 1. Make arena identity a closed contract

Add `experiments/contracts/int-6-skill-arena-v1.json`. Validation happens before
the output directory is created. The contract closes:

- `arena_version`: `manabot-skill-arena-v1`;
- world: `w2`, including observation/action ABI hashes;
- content suite: `w2-interactive-mirror-v1`, including the exact two identical
  deck mappings, retained ContentPack manifest hash, matchup hash, experience
  protocol hash, engine source hash, and native extension hash;
- information boundary: `acting-viewer-history-only-v1`;
- host/runtime class: Python 3.12, CPU inference, one Torch thread per player,
  exact package versions and machine identity;
- the exact five-player code-only anchor registry, each implementation source
  digest, and the registry's canonical SHA-256;
- rating-model version, optimizer/convergence contract, order-effect
  parameterization, Gaussian-prior digest, and Elo conversion;
- for candidate runs, the immutable anchor artifact digest whose registry,
  schedule, Commands, descriptive anchor profiles, competencies, and
  anchor-only matrix are reused;
- deal-block seeds, player-seed derivation, profile-root selection, competency
  seeds, bootstrap seed, and bootstrap count;
- typed promotion eligibility, fixed-compute envelope, approved thresholds,
  integrity gates, resource caps, and artifact schema versions;
- the source digest of the additive `manabot.arena` package and runner.

Ratings belong to the complete arena key:

```text
(world, content_suite, viewer_boundary, arena_version, rating_model_version,
 rating_prior_sha256, anchor_cohort_sha256, evaluation_compute_envelope_id)
```

The report never joins ratings from another key or from an anchor artifact with
a different contract or schedule. Player compute class remains an explicit
dimension on every rating and match row. Heterogeneous runnable configurations
share the closed cohort fit so the matrix exposes the skill/cost frontier, but
the rating is not compute-normalized: only a challenger and incumbent in the
exact same player compute class may form a promotion delta or replace one
another, and no longitudinal comparison may silently change either the player
class or the evaluation compute envelope.

The runner has two profiles:

- `smoke`: two deal blocks, two competency runs, and 100 bootstrap replicates;
  it proves plumbing and always returns `engineering_smoke_non_promotion`.
- `production`: 24 deal blocks (48 games per matchup), 100 paired competency
  runs per S1-S5 scenario,
  and 2,000 bootstrap replicates. It issues a rating disposition and may issue
  a promotion only when the cohort contains an exact eligible incumbent.

The base production contract has no learned checkpoint dependency and can be
frozen from current source. Tests may additionally create fixture checkpoint
registrations under a separate fixture contract, but that evidence class has a
distinct identity and can return only `engineering_smoke_non_promotion`.
Fixture bytes can never satisfy production registration or promotion.

Production uses four outcome workers, 16 cumulative wall hours, 64 cumulative
core hours, and 4 GiB of artifacts. Failed and resumed attempts remain charged
through the append-only hash-chained resource ledger.

### 2. Freeze anchors; content-address the challenger

Create a validated `PlayerRegistration` discriminated union in
`manabot/arena/models.py`. A registration contains:

- stable `player_id`, display name, role (`anchor`, `incumbent`, or
  `challenger`), and runner kind;
- model/checkpoint SHA-256, byte size, parameter count, training seed and
  immutable training/artifact IDs when applicable;
- algorithm/source SHA-256 for code-only players;
- information boundary and exact compute-class ID;
- all inference/search parameters, deterministic/stochastic mode, device and
  player seed derivation;
- world, observation ABI, semantic input spec when present, and content
  compatibility declaration.

Paths are locators, never identities. The runner rejects aliases such as
`latest`, missing bytes, wrong hashes, wrong checkpoint arms, incompatible
observation shapes, unregistered fields, duplicate IDs, and source drift.

The frozen arena-v1 anchor cohort is:

1. `random-v1`: uniform among current legal offers;
2. `scripted-greedy-v1`: deterministic viewer-safe script—play the first legal
   land, otherwise the first legal spell, otherwise pass; attack when offered,
   block the first legal attacker, take the first legal target/required choice,
   and decline optional choices only when no affirmative legal choice exists;
3. `flat-mc-4-v1`;
4. `flat-mc-16-v1`;
5. `flat-mc-64-v1`.

These registrations map to current executable seams, not future artifacts:

- `random-v1` delegates to
  `manabot.sim.flat_mc.RandomMatchupPlayer`, seeded from the arena seed plan and
  checked against the current legal offers before every transition;
- `flat-mc-{4,16,64}-v1` delegate to
  `manabot.sim.flat_mc.FlatMCPlayer` through `make_player({"kind": "search"})`
  with the complete budget parameters stored in each registration;
- `scripted-greedy-v1` is the only new base player implementation. It conforms
  to the existing `MatchupPlayer.act(env, obs) -> int` seam and selects only
  from the acting viewer's raw action-space metadata. Its deterministic rules
  are the ordered rules above, and its source digest is part of the registry.

Flat-MC budgets mean simulations per legal root action, with four rollouts per
world, a 2,000-step playout cap, uniform viewer-safe determinization, common
worlds across actions, and argmax tie-breaking. Those meanings are stored, not
inferred from a name.

Checkpoint challengers use `policy-cpu-batch1-v1`: deterministic policy-only
inference, CPU, one Torch thread, batch size one, no decision-time search, p95
matched-root latency no more than 10 ms, and isolated peak RSS delta no more
than 1 GiB. Flat-search anchors have separate compute classes. A checkpoint
challenger can enter and receive a rating without any learned anchor. If its
declared incumbent is absent, has unavailable bytes, or has another compute
class, promotion eligibility is typed unavailable rather than inferred from a
proxy.

The anchor list and anchor behavior never come from a training scheduler.
`manabot.arena` does not import `net_opponent`, accept opponent weights, or
perform adaptive scheduling. A new challenger changes its run manifest, not
the anchor cohort or arena version. `freeze-anchors` runs every unordered
anchor pair once and publishes a content-addressed anchor artifact.
`challenge` runs only challenger-versus-anchor cells; it must use the exact
registered deal blocks so the combined uncertainty calculation preserves the
common-random-number design. Changing any anchor, seed, or anchor evidence byte
requires a new cohort contract and digest or a byte-identical regenerated
artifact. A future learned anchor may bind the base artifact as an immutable
submatrix, but it creates a new `anchor_cohort_sha256`, refits ratings under the
new key, and never mutates base v1 evidence.

### 3. Add a stable arena player seam without disturbing frozen experiments

Add `manabot/arena/players.py` with an `ArenaPlayer` protocol and an additive
factory. It wraps current `manabot.sim.flat_mc.make_player` kinds and adds the
full-game scripted anchor locally. It does not edit `flat_mc.py`,
`teacher1_evidence.py`, or `manabot.verify.competency`; the arena composes their
current seams additively and carries its own source digest.

Every player receives only the acting viewer's encoded observation or raw
viewer-oriented observation. Search players may use the authoritative engine
only through the already viewer-safe determinization surface. The registration
records which of these boundaries applies. Arena instrumentation wraps
`act(...)`; it does not let a player inspect the opponent controller, result
ledger, or future deal seed.

Current main does not yet expose the target `MatchAuthority` /
managym-semantic-`Command` API from `docs/ARCHITECTURE.md`. Arena v1 therefore
reuses the already tested bridge exactly as it exists:
`manabot.sim.teacher1_evidence.build_viewer_frame` produces a validated
`ExperienceFrame`, and `etude.experience_protocol.Command` binds its prompt,
revision and offered action. The arena wraps that version-pinned bridge; it does
not invent a third offer or Command meaning. When managym's target semantic
Command lands, the adapter can migrate behind the arena seam, and any change to
durable command meaning forks the arena identity rather than rewriting v1
evidence.

The remaining executable seams are equally direct. `Match` and
`Match.swapped()` provide the two orientations; `Env.reset(seed=deal_seed,
options={"match": swapped})` can reset both legs on the same deal rather than
`play_games`' current successive seeds. `SCENARIOS` and `run_scenario_once` in
`manabot.verify.competency` accept any object with the same `act(...)` method,
so the arena can persist per-seed S1-S5 rows without changing scenario
authority. No base-cohort path loads a checkpoint or imports a training runner.

Errors are fatal. The arena never falls back to pass or action zero after an
illegal output, because a fallback would turn an integrity failure into fake
strength evidence.

### 4. Pair both seats on the same deal and keep all Commands

Add `manabot/arena/match.py` and `manabot/arena/replay.py`. The one-time anchor
freeze runs every unordered anchor pair. A candidate run adds every
challenger-versus-anchor pair. For each scheduled pair and registered deal seed,
run exactly two games:

```text
leg 0: player A in seat 0, player B in seat 1
leg 1: player B in seat 0, player A in seat 1
```

Both legs reset the identical symmetric matchup with the identical engine deal
seed. Each player is reconstructed for each leg. Its RNG seed is derived from
`(arena key, unordered player pair, deal seed, player_id)` and is therefore
stable across the seat swap, while being independent of filesystem paths and
worker order. The same registered deal-seed list is used for every pair, so a
global deal block contains both seat legs for the complete combined matrix.
Candidate runs may not substitute, omit, or append deal seeds.

Every `matches.jsonl` row repeats its closed evidence identity rather than
depending on a mutable join: world, content suite and digest, viewer boundary,
arena version and cohort digest; both player/model IDs and byte/source hashes;
both compute classes and the opponent seen by each player; deal-block, deal,
leg, per-player RNG and schedule-seed identities; trace and game digests; and
the result, termination, legality, replay, latency, throughput and cell-memory
receipts. Player/rating rows carry the same arena key, registration/model,
compute class, opponent-cohort and complete deal-seed-set identities.

At every surfaced decision the runner:

1. constructs the existing viewer-safe `ExperienceFrame`;
2. times the player's `act(...)` call;
3. proves the action is one of the current offers;
4. materializes and validates the existing protocol-v1 prompt-bound `Command`;
5. records actor, revision, action-space kind, pre-state digest, viewer frame
   SHA-256, Command, chosen offer, decision latency and truncation flags;
6. applies exactly the Command's `offer_id` and records the post-state digest.

The compact trace stores frame hashes rather than duplicating every large
projection. One deterministic gzip JSONL shard per unordered player cell holds
all of that cell's game headers, decisions, outcomes, and resource receipts.
Each game row records both the shard SHA-256 and its own canonical game-trace
SHA-256.

After generation, a replay stage instantiates `managym` plus the exact
version-pinned frame/Command bridge, resets the exact deal/orientation, rebuilds
every viewer frame, validates every Command binding and offer, applies the
recorded offer through the authoritative managym transition, and compares every
state digest and outcome. It never invokes the original players or search.
This proves Command replay independently from policy determinism and makes
verification cheap without claiming the transitional protocol object is the
future managym authority.

These are Intelligence-owned authority-private evidence traces, not fabricated
Game canonical replays: arena v1 does not invent presentation tracks or expose
mixed-view artifacts to clients. The existing Study adapter can consume a
selected viewer-safe decision later.

### 5. Fit one batch model and keep its failures visible

Add `manabot/arena/rating.py` using NumPy only. Fit all game outcomes jointly
with the Bradley-Terry order-effect model:

```text
logit P(A beats B) = beta[A] - beta[B] + seat0_bonus * seat_sign
seat_sign = +1 when A is seat 0, -1 when B is seat 0
```

A true draw contributes score 0.5. A nonterminal/truncated match is an
integrity failure and contributes no rating evidence. The symmetric paired
schedule identifies and removes the global seat-0 effect from player skill.

Plain Bradley-Terry maximum likelihood can diverge when a player is undefeated
or the directed win graph is not strongly connected. Fit a Gaussian-MAP model
with a preregistered 400-Elo standard deviation on sum-to-zero skills and the
seat effect. This guarantees a unique finite estimate while leaving the prior,
convergence tolerance, iterations, gradient norm and Hessian condition visible
in the artifact. Convert log-odds to Elo with `400 / ln(10)` and shift the
finished fit so `random-v1 = 1000`; only differences carry meaning.

Uncertainty is a deterministic nonparametric cluster bootstrap. Each replicate
resamples the 24 global deal blocks with replacement. A selected block brings
both seat legs for every anchor pair from the frozen artifact and every
challenger-versus-anchor pair from the candidate artifact, preserving seat
pairing and cross-cohort common-random-number correlation. Refit and recenter
the complete model in each replicate. Report percentile 95% intervals for
ratings, rating differences and seat advantage, plus bootstrap failure count.
Individual games are never treated as independent uncertainty units.

Emit the complete observed payoff matrix with wins, losses, draws, scores,
per-seat results and paired sweep/split counts. Beside it emit model-predicted
scores, raw percentage-point residuals, Pearson residuals, cell deviance,
global log loss/deviance, and the largest residual cells. The scalar rating is
a navigation signal; residuals and matrix are the non-transitivity warning.

### 6. Separate noisy gameplay timing from fixed-root cost

Outcome workers record native per-decision latency and game throughput, but
those numbers are not the fixed-compute promotion authority because four
workers contend for the host.

Add a serialized profile stage. It selects a contract-defined corpus of 128
decision roots from the frozen `random-v1` versus `scripted-greedy-v1` traces
by canonical `(deal seed, leg, revision)` order, with 16 earlier roots used only
for warmup. The anchor freeze profiles every anchor for descriptive cohort
costs. Every challenge profiles the challenger in fresh child processes. When
an exact promotion incumbent is registered in the same cohort and compute
class, the challenge re-profiles both in the same session over two
counterbalanced passes (`incumbent, challenger`, then `challenger, incumbent`)
and pools their samples; old incumbent timing is never promotion evidence. If
the incumbent is unavailable, the challenger still receives a complete cost
profile and the fixed-compute comparison is typed unavailable. Each process
replays the prefix Commands, encodes the root through that player's observation
space, and invokes `act(...)` without applying the result. The profiler asserts
that `act(...)` did not mutate the root.

For each player report:

- native gameplay and matched-root p50/p95 decision latency;
- decisions/second and games/second, with denominator and worker count;
- search simulations/traversals per second and playout cap rate when present;
- baseline RSS, post-load RSS, peak RSS, peak RSS delta, checkpoint bytes and
  parameter count;
- host, power, thread, sampler interval and root-corpus identities.

RSS comes from a fresh isolated process using both `ru_maxrss` and a 5 ms
`psutil` series; the report states that short spikes may be missed and shared
pages may be counted. CPU-only arena v1 does not pretend RSS measures GPU
memory.

### 7. Run competencies on common scenario seeds

Use the existing authoritative S1-S5 definitions and trackers. The arena's
player factory drives every anchor and challenger through 100 runs per
scenario with the same run seeds. Persist per-run correctness, not only the
aggregate. Continue to report Wilson intervals for each standalone rate. When
an exact incumbent is eligible, candidate-minus-incumbent differences use the
same rows for a paired seed bootstrap; otherwise the noninferiority comparison
is typed unavailable without suppressing candidate competency evidence.

This is a diagnostic suite attached to a complete gameplay player. It does not
gate whether a new architecture may enter the arena.

### 8. Preregister one strict promotion rule

Production first computes an explicit eligibility record. Promotion is
eligible only when the cohort contains an incumbent registration with present,
digest-valid bytes and the challenger names that exact player; both
registrations have the same player compute class; and the run is a production
profile. Missing bytes, an absent incumbent, or a compute-class mismatch yields
`rated_not_promotion_eligible` with a typed `unavailable` reason. It is not
converted into `retain_incumbent`, and no code-only anchor is used as a proxy.

For an eligible challenger, promotion requires every clause:

1. **Fixed compute:** challenger and incumbent have the exact same compute
   class; challenger matched-root p95 is at most 1.10x incumbent, throughput is
   at least 0.90x incumbent, peak RSS delta is at most 1.10x incumbent, and the
   class's absolute latency/RSS ceilings pass.
2. **Rating improvement:** candidate-minus-incumbent point estimate is at
   least +25 Elo and the 95% global deal-block bootstrap lower bound is above
   0 Elo.
3. **Competency noninferiority:** for every S1-S5 scenario, the point
   difference is no worse than -0.10 and the one-sided 95% paired-seed
   bootstrap lower bound is above -0.10.
4. **Integrity:** zero illegal or fabricated Commands, target/offer binding
   failures, action/card/permanent truncations, viewer-private exposures,
   replay mismatches, root mutations, missing games, artifact digest failures,
   or source/runtime/registry identity mismatches. Search playout cap rate must
   remain at or below 0.001. Resource caps must pass.

Disposition precedence is deterministic:

1. any integrity failure is `invalid_integrity`;
2. a smoke/fixture run is `engineering_smoke_non_promotion`;
3. valid production evidence without an eligible incumbent is
   `rated_not_promotion_eligible`, carrying one of
   `exact_incumbent_unavailable`, `incumbent_not_in_cohort`, or
   `compute_class_mismatch`;
4. eligible production evidence is `promote` only when all fixed-compute,
   rating, and competency clauses pass, otherwise `retain_incumbent`.

The receipt includes every clause, unavailable field, reason and input digest;
no prose-only judgment can override it after results exist.

### 9. Make the result independently verifiable

The resumable runner writes only content-bound artifacts. The anchor freeze
writes the anchor registry, anchor-only match and trace evidence, descriptive
profiles, competencies, replay receipt, and matrix under one manifest. A
candidate run references that manifest and writes challenger-specific gameplay
and competency evidence, a retained checkpoint copy for checkpoint-backed
registrations, a challenger profile, an incumbent comparison only when
eligible, and the recomputed combined result:

```text
manifest.json
resource-ledger.jsonl
players.json
checkpoints/<player-id>.pt               # checkpoint registrants only
matches.jsonl
traces/<player-a>__<player-b>.commands.jsonl.gz
replay.json
competencies.json
profile.json
rating.json
payoff-matrix.json
promotion.json
report.md
```

Job outputs and stage-result bytes are immutable and SHA-bound to manifest
copies, following the repository's existing bound-stage pattern. `--verify`
validates the anchor manifest before the candidate manifest, then validates
contract/runtime/player identities, the resource-ledger chains, every artifact
digest, retained checkpoint bytes when present, trace schema, every Command
replay, competency aggregates, the
Bradley-Terry optimum and bootstrap seed, matrix/residual aggregates, cost
metrics, and the promotion decision. Verify mode refuses to create missing
artifacts, play games, invoke players, or bootstrap with an unregistered seed.

### Implementation map

- `manabot/arena/models.py`: strict registrations, arena key, match/rating and
  promotion result models.
- `manabot/arena/players.py`: stable player protocol/factory and scripted
  anchor.
- `manabot/arena/match.py`: paired-deal anchor freeze, incremental challenge,
  and instrumentation.
- `manabot/arena/replay.py`: compact Command trace writer and exact verifier.
- `manabot/arena/rating.py`: seat-aware Gaussian-MAP Bradley-Terry, Elo
  conversion, residuals and global deal-block bootstrap.
- `manabot/arena/profile.py`: fixed-root latency/throughput/RSS profiling.
- `manabot/arena/competency.py`: common-seed S1-S5 runner and paired
  noninferiority summaries.
- `experiments/runners/run_skill_arena.py`: preregistered resumable orchestration,
  ledger, report and independent verify mode.
- `experiments/contracts/int-6-skill-arena-v1.json`: immutable v1 cohort,
  schedule, gates and caps.
- `tests/arena/`: statistical, identity, schedule, replay, profiling,
  competency and end-to-end smoke coverage.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Is the selected matchup already pinned? | Current main exposes the symmetric `manabot.verify.util.INTERACTIVE_DECK` mirror, while `teacher1_evidence.runtime_fingerprints` reports its content, matchup, Observation/action ABI, protocol, engine-source, and native-extension hashes. | Arena v1 reuses and contract-pins those exact current identities rather than depending on another Task or creating a new deck/world number. |
| Which current players make the base arena executable? | `flat_mc.MatchupPlayer` is the common `act(env, obs)` seam. `make_player` already constructs seeded `RandomMatchupPlayer` and `FlatMCPlayer`; the latter stores sims/worlds/rollouts/max-steps and uses common worlds across legal root actions. | Freeze random plus flat-MC 4/16/64 directly. Add only the viewer-safe deterministic greedy implementation in `manabot.arena.players`, under the same protocol and a bound source digest. No base registration loads weights. |
| Can the existing matchup loop produce paired deals? | `flat_mc.play_games` uses `Match.swapped()` but increments the engine seed every game, so adjacent seat orientations are different deals. `Env.reset` already accepts both an exact seed and swapped-match option. | Arena owns a thin loop that calls `Env.reset(seed=deal_seed)` and `Env.reset(seed=deal_seed, options={"match": match.swapped()})` for the two legs, with player RNG identity kept separate. |
| Are arena matches replayable today? | `teacher1_evidence.build_viewer_frame` and `build_command` validate the acting viewer's frame and offered action; `Env._engine.state_digest()` witnesses roots/transitions; `replay_teacher_trajectories` rebuilds frames, validates Commands, and advances through `Env.step(command.offer_id)` without a player. | Reuse those exact seams in an arena-shaped trace/verifier. Store every Command plus pre/post hashes and never invoke policies or search during verification. |
| Should arena output claim to be a Game canonical replay? | Game's canonical replay also owns presentation tracks and authorized viewer projection. A simulator does not currently own those presentation events. | Keep arena traces authority-private Intelligence evidence. Do not fabricate Game-owned presentation state. |
| Can a plain Bradley-Terry MLE always be fitted? | No. Undefeated/winless players or a non-strongly-connected directed result graph can send MLE skill to infinity; a local synthetic 192-0 test reproduced the separation case. | Use a preregistered Gaussian-MAP Bradley-Terry fit. The same synthetic case converged to finite ratings in 12 Newton iterations. |
| Does regularization have a principled guarantee? | A Gaussian prior makes generalized Bradley-Terry MAP estimation finite and unique; the prior must be visible because it affects extreme ratings. | Freeze a 400-Elo standard deviation and publish convergence/prior metadata rather than silently clipping ratings. |
| What is the independent uncertainty unit? | Both seat legs share a deal, and the same deal seeds are intentionally reused across all cells. Game-level binomial intervals would overstate information. | Bootstrap whole global deal blocks, carrying both legs and every pair together. Use paired scenario seeds for competency differences. |
| Does seat balancing alone remove play/draw bias from the model? | It balances aggregate exposure but leaves a measurable order effect and can waste information. | Fit one global seat-0 nuisance coefficient and retain per-seat matrix rows. |
| How will non-transitivity appear? | AlphaStar-scale leagues contain strong rock-paper-scissors cycles; one scalar cannot describe them. | Always emit the full matrix, observed-versus-expected residuals and deviance beside Elo. Promotion is against the whole frozen cohort, not rating alone. |
| Can match-worker timing support fixed-compute claims? | Four-worker outcome runs contend for CPU, while Intelligence memory explicitly requires a quiet host for latency/throughput/RSS cells. | Report native match timing but authorize promotion only from a serialized, isolated, matched-root profile stage. |
| Can peak memory be attributed in a shared process? | `ru_maxrss` is process-wide and monotone; two players in one long-lived worker make per-player attribution impossible. | Profile each player in a fresh process, record baseline/peak/delta and the sampler limitations. Match-cell RSS remains a separate shared receipt. |
| Must learned anchor bytes exist before arena v1 can ship? | No exact learned incumbent bytes are available, and the code-only population is sufficient to identify a connected rating model, exercise paired matches, and rate a checkpoint challenger. It is not sufficient for a same-compute learned promotion comparison. | Freeze the five code-only anchors now. A checkpoint challenger receives `rated_not_promotion_eligible` when no exact incumbent is registered. Adding a learned anchor requires a new cohort contract/digest and cannot mutate v1. Fixture checkpoints remain smoke-only. |
| Does current main have the target authoritative semantic Command API? | No. `docs/ARCHITECTURE.md` marks managym `MatchAuthority`/`DecisionFrame`/`Command` as the target. Current Teacher-1 evidence instead validates a viewer-safe `ExperienceFrame` and prompt-bound `etude.experience_protocol.Command`, then applies its offered action through managym. | Reuse and source-pin the current tested bridge for arena v1. Do not create another semantic layer or call the trace a future canonical replay; fork arena identity if migration changes durable Command meaning. |
| Is the native Python engine ready in this worktree? | A `uv run` import found no `managym._managym` extension. | The implementation/demo checklist rebuilds the pinned cp312 extension before integration tests; missing or wrong extension identity is a preflight failure. |
| Can the arena compose current evidence without rewriting frozen helpers? | Yes. The required constructors and scenario definitions are callable from additive modules; `SCENARIOS` plus `run_scenario_once` accept the same player `act(...)` seam. | Add `manabot.arena` adapters without editing `flat_mc.py`, `teacher1_evidence.py`, or `competency.py`. The arena gets its own source digest and existing evidence keeps its historical source identity. |
| Can a stochastic player be reproduced across worker counts? | Existing players derive internal seeds from runner-local values and call order. Worker scheduling is not an identity. | Derive player seeds from the arena key, unordered pair, deal seed and player ID; reconstruct players per leg. Store all derived seeds. |
| How should draws and caps enter a binary model? | A real engine draw is a legitimate half result; a nonterminal truncation is not. Current selected-match evidence has zero draws/caps but the schema should not conflate them. | Store termination reason. Score true draws as 0.5; reject truncations; report both explicitly. |
| Does the complete anchor schedule fit the production cap? | Exp-02 ran 3,900 flat-search games at budgets 16/64/256 in 4.5 CPU-core-hours. The five-player base has 10 unordered pairs and therefore 480 production anchor games at 24 blocks × two legs, with flat search capped at 64. | Retain the 16 wall-hour/64 core-hour cap. The historical margin is ample for trace/replay/profile/competency work, while the cumulative ledger still stops launch if current instrumentation makes the estimate stale. |
| Should adaptive opponents improve arena efficiency now? | The task and research program require a stable reference cohort; OpenAI Five likewise separated adaptive training opponents from fixed rating references. | Exhaustively evaluate the frozen cohort in v1. PFSP, near-skill scheduling and exploiters stay out. |

Research references: Butler and Whelan on [Bradley-Terry MLE
existence](https://arxiv.org/abs/math/0412232), Fageot et al. on finite unique
[Gaussian-prior Bradley-Terry MAP](https://arxiv.org/abs/2308.08644), the
[AlphaStar population](https://www.nature.com/articles/s41586-019-1724-z), and
the [OpenAI Five fixed-reference versus training-opponent
split](https://cdn.openai.com/dota-2.pdf). Repository-specific prior-art
findings are recorded in
`docs/research/intelligence-development-ladders.md`.

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Continue emitting pairwise Wilson win rates | Minimal code and familiar reports. | It has no population scale, no coherent candidate-versus-cohort uncertainty, and hides non-transitive residuals. It does not satisfy the task. |
| Online Elo updates | Easy incremental leaderboard and no optimizer. | Order-dependent, awkward with paired common-random-number blocks, and encourages a mutable cohort. The task explicitly asks for a batch Bradley-Terry result. |
| TrueSkill/OpenSkill | Handles online uncertainty and future sparse scheduling. | Adds a different latent model before a sparse league exists, makes reproducibility depend on update order, and does not remove the need for a payoff matrix. Revisit after v1. |
| Davidson model for ties | Models draws generatively. | Selected-match historical cells have zero draws, and v1 can transparently treat a rare true draw as half score. Adding a tie parameter now spends complexity on no observed bottleneck. |
| Unregularized Bradley-Terry MLE with clipped ratings | Matches the textbook likelihood until separation. | Clipping is an undeclared prior and gives unstable bootstrap replicates. A visible Gaussian prior is more honest. |
| Hessian-only standard errors | Fast and conventional under independent games. | Games are paired by deal and share seeds across cells. The independence assumption is wrong by construction. |
| Bootstrap each pair separately | Preserves the two seat legs within a matchup. | It drops correlation created by using the same deal seed across the complete combined schedule. Global deal-block resampling preserves the actual experiment. |
| Use the Game server to record canonical presentation replays | Produces the richest player-facing trace. | It routes thousands of simulator games through a product server and crosses the Game/Intelligence ownership boundary. Arena only needs Commands and exact authority replay. |
| Fold arena behavior into `flat_mc.play_games` | Reuses the current loop directly. | It changes the source identity of existing experiment evidence and overloads a pairwise helper with registry, replay, rating and promotion policy. |
| Rerun the complete anchor round robin for every challenger | Makes every candidate directory fully standalone. | It spends most evaluation cost reproducing evidence whose identities and Commands are already immutable. Arena v1 freezes and verifies the anchor submatrix once, then binds it by digest in every challenge. |
| Require a learned incumbent before shipping arena v1 | Enables an immediate promote/retain result for learned candidates. | Exact incumbent bytes do not exist, and another Task is parked. The code-only cohort already delivers a connected, replayable rating instrument; eligibility can fail closed without blocking that value. |
| Treat a code-only anchor as the learned candidate's incumbent | Produces a numerical delta and apparent promotion decision now. | Compute classes differ, so the result is not fixed-compute evidence. The arena reports `rated_not_promotion_eligible` instead of laundering a proxy into an incumbent. |
| Add PFSP, exploiters or near-rating scheduling immediately | Reduces games and may improve training. | Those are adaptive training/league policies, not the fixed evaluation instrument INT-6 owns. There is not yet a sufficiently rich admitted archive. |

## Key decisions

1. **Arena v1 rates runnable configurations, not abstract algorithms.** A
   policy checkpoint and that checkpoint plus search are different players
   with different compute classes.
2. **The base cohort is exactly five code-only players.** Random,
   scripted-greedy and flat-MC 4/16/64 can freeze from current source with no
   checkpoint or cross-Task dependency.
3. **The scale is stable because the world, content, estimator/prior, compute
   envelope, anchors and seed schedule are stable.** Absolute `1000` is only
   the random anchor; Elo differences are the evidence.
4. **The anchor round robin is frozen once; candidate entry is incremental.**
   The initial anchor artifact contains the complete anchor matrix and exact
   Commands. Each challenger plays every anchor on the same deal blocks and
   binds the anchor artifact by digest. The combined fit is closed and
   independently verifiable without paying repeatedly for anchor games.
5. **The matched-root profiler is part of the arena, not a benchmark appendix.**
   Fixed-compute promotion cannot depend on contended outcome-worker timing or
   compare current challenger cost against an old incumbent measurement. Every
   challenge profiles the candidate; an eligible incumbent is re-profiled
   contemporaneously, otherwise the comparison is typed unavailable.
6. **Every decision retains the current validated Command bridge.** Action
   indices remain an engine transport inside w2, never the retained evidence
   identity; arena v1 does not pre-implement the target managym Command API.
7. **Integrity gates are zero-tolerance.** The arena does not score games it
   repaired, truncated, leaked, or could not replay.
8. **The rating model admits its misspecification.** Seat effect, complete
   matrix and residuals stay visible; no Elo rank is called equilibrium or
   superhuman evidence.
9. **Learned registration extends rather than mutates the base.** A checkpoint
   challenger may be rated now. Adding an exact learned anchor requires a new
   cohort contract/digest; only that cohort can make a same-compute promotion
   decision against it.

## Wild success

Six months from now, every Teacher-1, belief-aware, semantic, and distilled
candidate arrives as a content-addressed registration and produces the same
report. A developer can open the largest residual cell, replay the exact
Commands that broke the scalar model, then turn that observed failure into the
next competency or training change. The arena becomes a shared language across
otherwise different Intelligence projects without becoming their scheduler.

## Wild failure

The arena would be removed if teams optimize only its Elo, the anchors drift
silently, timing varies with worker load, checkpoint paths masquerade as
identities, or replay stores action integers that no longer mean anything. The
contract, frozen cohort, fixed-root profiler, full residual matrix and Command
replay directly prevent those failure modes. Arena v1 still cannot detect an
exploit absent from its cohort; that limitation remains explicit until a later
task adds dedicated exploiters and a broader content suite.

## Scope

- In scope: one w2 symmetric `INTERACTIVE_DECK` mirror; strict player and
  candidate registration; random, deterministic scripted, flat-MC 4/16/64,
  and one checkpoint challenger; a one-time complete paired-deal code-only
  anchor round robin plus challenger-versus-every-anchor entry; typed
  promotion eligibility with fail-closed unavailable reasons; authority-private
  Command traces and exact replay; batch
  seat-aware Bradley-Terry/Elo; global paired-block uncertainty; full payoff
  matrix and residuals; S1-S5; legality/viewer integrity; native and
  matched-root latency/throughput/RSS; preregistered promotion; independent
  verification.
- Out of scope: PFSP or any adaptive training-opponent scheduler; sparse or
  near-skill match scheduling; dedicated exploiters or best-response claims;
  CFR/public-belief solving; training or resuming another Task; manufacturing,
  reconstructing, or substituting an incumbent checkpoint; multi-deck, format,
  Team Sealed, human, or public ladders; cross-world ratings; Game UI or
  canonical presentation replay; a superhuman claim.

## Done when

The implementation is complete when:

1. the checked production contract freezes exactly random, scripted-greedy and
   flat-MC 4/16/64 with their complete source/config identities and rejects any
   world/content/source/runtime/player/hash, estimator/prior, compute-envelope,
   or schedule drift before producing evidence;
2. the base anchor artifact has all 10 unordered anchor pairs (480 games), and
   each candidate artifact has all five challenger-versus-anchor pairs (240
   games), with exactly 24 production deal blocks, two opposite seat legs per
   block, and complete row-level identities;
3. every surfaced decision becomes a validated retained Command and independent
   replay returns zero frame, offer, Command, state, outcome, privacy or missing
   match mismatches;
4. the finite seat-aware Bradley-Terry fit, 2,000 global deal-block bootstrap
   replicates, full matrix, residuals and rating disposition recompute exactly
   from frozen match rows;
5. every player has S1-S5, legality, replay, latency, throughput and RSS output;
6. a valid production checkpoint challenge without an exact incumbent returns
   `rated_not_promotion_eligible/exact_incumbent_unavailable`, while an
   explicitly fixture-scoped checkpoint can return only
   `engineering_smoke_non_promotion`;
7. the base contract rejects any added learned anchor, and a test proves an
   exact learned registration changes `anchor_cohort_sha256` rather than
   mutating or joining base-v1 ratings;
8. verify mode performs no generation or policy/search inference;
9. focused tests and the full Python suite pass through uv, and the debug Rust
   suite passes.

Verification commands:

```bash
uv run --python 3.12 --extra play maturin develop --release \
  --manifest-path managym/Cargo.toml --features python
uv run pytest tests/arena tests/sim tests/verify
uv run pytest
cargo test --manifest-path managym/Cargo.toml
uv run ruff check manabot/arena experiments/runners/run_skill_arena.py tests/arena
uv run ruff format --check manabot/arena experiments/runners/run_skill_arena.py tests/arena
```

The production artifact is independently checked with:

```bash
uv run experiments/runners/run_skill_arena.py challenge \
  --contract experiments/contracts/int-6-skill-arena-v1.json \
  --anchor-artifact .runs/int-6-skill-arena-v1/anchors/manifest.json \
  --candidate experiments/candidates/<candidate>.json \
  --candidate-checkpoint /absolute/path/to/candidate.pt \
  --out-dir .runs/int-6-skill-arena-v1/<candidate> \
  --verify
```

## Measure

Arena v1 advances these Intelligence measures from `wave/intelligence/GOAL.md`:

- **“Every admitted candidate enters a versioned, world-pinned skill arena.”**
  Evidence: exact arena key, immutable registration, population Elo with
  paired-deal uncertainty, complete matrix and promotion receipt.
- **“Search teachers and students are compared ... with legality,
  competencies, seat-balanced strength, calibration, p50/p95 decision
  latency, rollout throughput, label cost, and uncertainty.”** Evidence here:
  legality, competencies, paired seat/deal strength, p50/p95, decision/search
  throughput, RSS and uncertainty. Target calibration and label cost remain
  player/teacher evidence fields when available; arena v1 does not fabricate
  them for players that do not emit them.
- **“Any superhuman claim names the matchup and content boundary, information
  boundary, model and opponent cohort, compute budget, seeds ... and
  uncertainty.”** Arena rows carry all of those identities, while the report
  explicitly stops short of a superhuman claim.

Primary quantitative outputs are the candidate's arena-local Elo coordinate and
global deal-block interval, the complete payoff/residual matrix, S1-S5, exact
replay/integrity counts, and matched-root p50/p95, decisions/s and RSS. When an
exact same-compute incumbent exists in a later cohort, the same schema also
reports candidate-minus-incumbent Elo, paired competency noninferiority and
cost ratios. Better means clearing the preregistered promotion rule when
eligible; `rated_not_promotion_eligible` is the honest base-v1 production
outcome, not a failed instrument.
