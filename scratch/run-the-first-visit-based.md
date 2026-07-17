# First Visit-Based Teacher and Student Arena Iteration

## Problem

Etude has a working flat-Monte-Carlo Teacher-0 pipeline, a real deterministic
PUCT reference, a viewer-safe replay/evidence recorder, visit-distribution
training support, and an authoritative rules branching decision. It does not
yet have the thing the Search Teacher and Student Arena Project is measured on:
one complete visit-based iteration that searches real selected-matchup states,
freezes replayable labels, trains multiple independent students, returns those
students to the actual matchup, and makes an evidence-backed continue, revise,
or kill decision.

The remaining work is integration and measurement rather than another search
representation study. The Rules benchmark has already selected
`full_clone/current_game_v1` as the production default: it was faster and
smaller than clone-plus-undo in the flat workload, while page COW missed its
registered throughput and memory bars. The existing Python PUCT path already
uses the production `Env.fork()`/full-clone boundary. INT-4 should run that
system, measure its live Python tree economics, and port the search loop behind
the Rust `FullCloneDriver` only if Python bookkeeping—not state
representation—is the measured limiter.

The direct beneficiaries are intelligence developers and Study. Intelligence
gets a runnable teacher → dataset → student → search-augmented student loop with
honest controls. Study gets one real historical decision whose policy and
search evidence is produced by the running systems instead of fixture numbers.

## The demo

Run:

```bash
uv run experiments/runners/run_visit_teacher_iteration.py \
  --contract experiments/contracts/int-4-visit-teacher-iteration-v1.json \
  --out-dir .runs/int-4-visit-teacher-iteration-v1
```

The resumable command ends with a four-agent arena table for Teacher-1,
visit-policy-only, visit-policy/value, and student-guided PUCT; an explicit
`continue`, `revise`, or `kill` verdict; and a validated Study artifact linked
to an exactly replayed historical decision. Running the same command with
`--verify` reproduces every trajectory and the predeclared sampled search roots
without generating new evidence.

## Approach

### 1. Finish one contract, not a collection of ad hoc scripts

Add `experiments/contracts/int-4-visit-teacher-iteration-v1.json` and one
resumable orchestrator, `run_visit_teacher_iteration.py`. The contract is the
source of truth for stages, seeds, budgets, fingerprints, controls, arena
cells, caps, gates, and result branches. Each stage writes atomically and may
resume only when its contract, input, and runtime hashes match.

The existing `w2-234-teacher1-pilot-v1` contract remains frozen evidence. Its
8/32/128 budgets, four worlds, `c_puct=1.5`, random terminal leaves, 2,000-step
cap, evaluation seed blocks, stability roots, scenario count, replay roots,
and teacher quality thresholds carry forward unchanged. The new contract
supersedes its missing recovery dependency and its quality-as-admission policy;
it does not rewrite or claim to have executed the old experiment. The old
contract is marked superseded and its tests change from “must match the current
runtime forever” to “must reject a foreign runtime.”

The run has five ordered stages:

1. freeze controls and same-host latency calibration;
2. evaluate Teacher-1 at 8/32/128 traversals;
3. generate a replayable 256-game `t1-128-w4` self-play dataset;
4. train the matched 2×2 student arms at seeds 197, 419, and 887;
5. run the arena, competencies, and Study evidence export.

Only an integrity failure blocks later stages: illegal commands, viewer leaks,
root mutation, tree-accounting errors, non-finite targets, ABI truncation, or
failed replay. A legal but weak, unstable, slow, or poorly calibrated teacher
still runs the already-bounded student iteration. That is evidence about the
working prototype, not permission to tune after looking at the result.

### 2. Use deterministic multi-world PUCT honestly

Retain the readable `manabot.sim.mcts` algorithm as Teacher-1:

- four independently determinized worlds per root;
- a separate adaptive tree per world;
- uniform priors and seeded random terminal playouts;
- root visits summed across worlds, with total mass exactly equal to the
  declared traversal budget;
- actor-aware selection and root-actor backed-up values in `[0, 1]`;
- no root noise and deterministic visit-first/Q-tiebreak action selection.

This is actual MCTS/PUCT over authoritative engine clones, but it is not
information-set-consistent search. The artifact and report must call it
`determinized_puct_uniform_prior_random_leaf/v1`; they must not call it ISMCTS,
public-belief search, or an unbiased estimator of equilibrium value. ISMCTS
searches information-set trees precisely because independently determinized
state trees have different semantics. That limitation is a future treatment
candidate, not a reason to withhold the first running baseline.

Extend `PuctResult` with per-world root visits, per-world Q values, and
per-world backed-up root values. Aggregate output remains byte-for-byte
compatible for the uniform/random evaluator. These world-level statistics are
needed for honest sampled-world robustness and uncertainty; they are not new
learner inputs.

Add a leaf-evaluator interface with two implementations:

- `UniformRandomLeafEvaluator`, which exactly reproduces Teacher-1;
- `AgentLeafEvaluator`, which uses a frozen student on CPU for masked priors
  and actor-relative sigmoid value at newly expanded nodes.

`AgentLeafEvaluator` makes the final `student+search` arm real. At a root it
uses the already-projected acting-viewer observation. At child nodes it uses
the observation returned by the authoritative step for that node's acting
player. Values are converted from node-actor perspective to root-actor
perspective before backup. Search inference stays on CPU: single-leaf MPS
calls cannot exploit the measured batch-256 advantage and multi-process MPS
contention would make the cost comparison host-topology dependent.

### 3. Make the viewer boundary executable

The learner shard contains only the acting viewer's encoded observation,
encoded legal mask, chosen command index, aggregate root visits, aggregate Q
values, root value, seat, game/decision identities, and terminal outcome.
Authority-only material—deal seeds, determinization seeds, complete state
witnesses, and sampled-world detail—lives in a separate audit bundle and never
enters model input or the Study artifact.

Add a viewer-equivalence conformance test. Two roots with identical acting
viewer projection, public history, legal offers, and unseen-card multiset but a
different actual opponent-hand/library allocation must produce identical
visits, Q values, root value, and selected command under the same search seed.
This proves the teacher is sampling from the viewer's information boundary
rather than accidentally consulting the authoritative opponent hand.

Every generated game writes a compact replay sidecar containing:

- deal seed and matchup identity;
- ordered frame hash, viewer, prompt, legal-offer hash, played `Command`, and
  post-command authority hash for every surfaced decision;
- winner/termination receipt;
- teacher budget, player seed, search call index, and search call seed;
- input and output artifact hashes.

Verification reconstructs every one of the 256 trajectories and requires exact
frame, offer, command, state-hash, and outcome equality. It also exactly reruns
the eight predeclared sampled search roots from the carried-forward contract,
including action, visits, Q values, root value, world statistics, node count,
depth, and cap hits. Search is not rerun at every training row because that
would duplicate label cost; trajectory replay is exhaustive and search replay
is predeclared and sampled.

### 4. Measure the teacher at multiple real budgets and controls

Evaluate budgets 8, 32, and 128 in the symmetric 60-card
`INTERACTIVE_DECK` matchup. Every cell uses the existing three fixed
seat-balanced blocks of 16 games at seeds 1197, 1419, and 1887 and reports each
block plus the 48-game aggregate. Evaluation blocks quantify search/evaluation
noise; they are not mislabeled as independent training seeds.

Each budget plays:

- uniform random;
- the frozen Teacher-0 policy-only checkpoint;
- the frozen Teacher-0 policy/value checkpoint;
- Teacher-0 flat MC calibrated within 10% of realized p50 decision latency on
  the same host.

The 128-traversal teacher also plays the 8-traversal teacher. All budgets run
the five scripted competency scenarios, with 100 trials per cell. Root
stability uses the already registered 192 roots and repeat seeds 2197001,
2197002, and 2197003.

The available terminal Teacher-0 pilot checkpoints are the frozen incumbents:

- policy-only SHA-256
  `3bfedccf5aa6ed7621d99284ea8cea3975d8b195cecf6426d37dd7abc812c978`;
- policy/value SHA-256
  `92ced7abb31bc68298b48cc08ed7eb57f3dde50295a22d50ea2fe32f7e359176`.

They are controls, not a claim that the interrupted 3,000-game Teacher-0
recovery finished. The stronger 512-game snapshot checkpoint hashes are
documented, but the checkpoint bytes are no longer present; silently
reconstructing or substituting them would weaken provenance.

For every search cell, record legality and ABI receipts, p50/p95 decision
latency, labels/s/worker, traversals/s, tree nodes, depth, cap rate, process
baseline/peak/delta RSS, wall/core seconds, bytes per usable label, and core
seconds per 1,000 labels. Run each measured cell in an isolated child process
while the parent samples RSS, following the already-reviewed branching
benchmark pattern.

Carry forward the registered high-budget teacher thresholds:

- at least 55% versus `t1-8-w4` and frozen policy/value, and 80% versus random;
- at least 70% repeated top-action agreement and median JS divergence at most
  0.10;
- increasing mean nodes and maximum depth from 8 to 128;
- at least 20% correct on one of S1/S2/S5 and no scenario more than 10 points
  below random;
- p95 at most 500 ms, at least two labels/s/worker, and cap rate below 0.1%;
- flat-MC p50 matching within 10% at each budget.

These thresholds determine the diagnosis, not whether the fixed end-to-end
prototype is allowed to exist.

### 5. Freeze one dataset and train a full 2×2 ablation

Generate exactly 256 `t1-128-w4` self-play games as 32 atomic eight-game
shards. The fixed dataset is shared across all arms and all training seeds. It
must contain no illegal action, winner-less value row, non-finite target,
positive visit outside the legal mask, or visit sum different from 128.

For each seed 197, 419, and 887, use an identical model initialization, game
split, optimizer, capacity, batch order seed, and 25-epoch cap across four
arms:

| Arm | Policy target | Value target | Purpose |
|---|---|---|---|
| `chosen_policy_only` | one-hot played command | none | hard-label baseline |
| `chosen_policy_value` | one-hot played command | backed-up root value | value effect with hard labels |
| `visit_policy_only` | normalized root visits | none | visit-information effect without value |
| `visit_policy_value` | normalized root visits | backed-up root value | primary student |

This factorial design separates visit-distribution from chosen-action
supervision and policy-only from policy/value learning without changing data
or initialization. `visit_policy_value` is the predeclared student;
`visit_policy_only` is the predeclared policy-only arena control. Seed 197—not
the best result—is the predeclared Study model.

Report held-out target CE, KL, entropy, top-1 agreement, root-value MSE/Brier,
and terminal-outcome Brier/ECE separately. Teacher root-value imitation is not
reported as terminal calibration. Checkpoints are content-addressed and bind
dataset, arm, seed, model schema, optimizer, runtime, and exact weights.

MPS is allowed for the matched training arms because its measured batch
throughput is useful and every resulting checkpoint is frozen by hash. CPU is
used for arena inference and search so evaluator timing and exact replay do not
depend on MPS process scheduling.

### 6. Run the complete arena and make admission deterministic

For every training seed, run a 48-game, three-block, seat-balanced round robin
among:

- `teacher`: `t1-128-w4` uniform/random-leaf PUCT;
- `policy-only`: that seed's `visit_policy_only` student;
- `student`: that seed's `visit_policy_value` student;
- `student+search`: that same student supplying CPU priors and value leaves to
  determinized PUCT.

Calibrate the `student+search` traversal count so its realized p50 decision
latency is within 10% of Teacher-1's p50; record both raw traversal counts.
Flat MC is already matched at the same p50 in the teacher stage. Pure policy
arms are not artificially delayed: they are the zero-search cost frontier and
their lower native latency is reported explicitly.

Add two cheap head-to-head ablation cells per seed:
`visit_policy_only` versus `chosen_policy_only`, and `visit_policy_value`
versus `chosen_policy_value`. Run the primary student and student+search against
the frozen Teacher-0 policy/value incumbent and through all five competencies.

The admitted agent is selected by a fixed priority:

1. admit `student+search` if its median win rate across training seeds is at
   least 55% versus its paired student, at least two of three seeds exceed 50%,
   and no competency is more than 10 points below that student;
2. otherwise admit `student` if its median win rate versus the frozen
   Teacher-0 policy/value incumbent is at least 55%, at least two of three
   seeds exceed 50%, and no competency is more than 10 points below the
   incumbent;
3. otherwise record `prototype_failure` and choose the next build from the
   predeclared branch table.

Report Wilson intervals within each game block, paired seat/deal aggregates,
and per-training-seed results. Method claims use the three training seeds as
the experimental units and report their full range; game-level intervals are
not promoted to method uncertainty.

### 7. Emit one real Study landmark

Select a landmark deterministically from the replayed high-budget trajectories:
the first predeclared audit root in `(game_index, decision_index)` order that
has at least two legal offers and at least one visit to every offer. This is a
selection by evidence completeness, not by whether the chosen action looks
good.

Generate a `StudyArtifact` using the existing version-1 contract:

- exact historical `ExperienceFrame`, chosen `InteractionOffer`, and played
  `Command` from the replay;
- seed-197 `visit_policy_value` masked policy mass;
- Teacher-1 per-offer Q values and aggregate visits;
- favorable sampled worlds, defined as worlds in which the offer's visited Q
  is maximal, with the number of visited worlds retained as the denominator;
- between-world standard error over visited per-world Q values, with its method
  named explicitly;
- content, engine, checkpoint, replay, search-budget, and producer hashes.

Policy probability, visits, expected value, sampled-world robustness, and
uncertainty remain separate fields. Missing world coverage reduces the stated
world denominator; it is never converted to zero confidence or copied from a
different metric. No deal seed, determinization seed, opponent hand, or other
authority-private search state is serialized into the Study artifact.

Validate the result through both the Rust-generated JSON Schema and Python
`StudyArtifact`, then re-run the source replay and sampled search root. The
checked-in result is evidence supplied to Study; INT-4 does not add Study UI.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Does Etude already have actual MCTS, or only a flat control? | `manabot/sim/mcts.py` grows adaptive trees, reuses nodes within a root search, applies PUCT, backs up alternating-player values, and emits exact root visits. Focused tests and the prior smoke pass. | Extend and measure the landed implementation; do not build a second teacher. |
| Which branch representation should production search use? | The registered Rules matrix selected compact full clone. Clone+undo was slower and used more RSS; page COW failed its throughput/memory bars. | Use production `Env.fork()` now. Do not reopen representation benchmarks. |
| Is the existing Teacher-1 contract runnable on this branch? | No. The landed BranchDriver work changed engine source/binary hashes, so 23 focused Python tests pass and the frozen-runtime test correctly exposes stale fingerprints. The required control lock also does not exist. | Freeze a new INT-4 contract after implementation, retain the old contract as superseded evidence, and bind current source/binary/model identities before results. |
| Is there a usable frozen Teacher-0 control? | The terminal 100-game pilot's policy-only and policy/value checkpoint bytes and hashes exist. The documented 512-game checkpoint bytes do not, and the 3,000-game recovery manifest remains nonterminal. | Use the available terminal pilot checkpoints as named incumbents; do not claim or reconstruct the missing recovery. |
| Can a determinized search accidentally read the real opponent hand? | The engine determinizer samples opponent hand and libraries from the acting viewer's unseen pool, but current tests do not prove PUCT output invariance across viewer-equivalent authorities. | Add the viewer-equivalent root test as an integrity gate. |
| Does separate-world determinization solve hidden information? | No. The ISMCTS literature searches information-set trees rather than independent state trees; the current method retains strategy fusion/non-locality. | Name the algorithm and limitation precisely; use the result as a baseline, not an equilibrium claim. See [Cowling, Powley & Whitehouse (2012)](https://eprints.whiterose.ac.uk/id/eprint/75048/). |
| Are root visits a defensible student target? | AlphaZero and MuZero define the improved root policy from visit counts and train the policy toward it; MuZero also separates policy and value targets. | Train normalized visits directly and retain a one-hot chosen-action control. See [AlphaZero](https://arxiv.org/abs/1712.01815) and [MuZero](https://arxiv.org/abs/1911.08265). |
| Can the current network supply a real student+search arm? | The `Agent` already returns policy and value logits. Root observations are available, and each child `step` returns the next actor's viewer observation. The missing piece is a small evaluator interface and perspective conversion. | Add model priors/value leaves without changing engine authority or model architecture. |
| Can Study consume the measurements without inventing confidence? | Study v1 already separates policy mass, search value, visits, world robustness, and uncertainty and rejects opponent-private hands. Current PUCT discards per-world root statistics. | Retain per-world statistics in `PuctResult` and build one real artifact; do not change the Study schema. |
| Will MPS make search timing or replay dishonest? | Existing evidence shows MPS wins at batch 256, while this tree expands one leaf at a time and multi-process MPS contention is known. | MPS only for matched student training; CPU for policy arena inference and all model-guided search. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Port PUCT to Rust before any measurement | Could remove Python tree bookkeeping and expose `FullCloneDriver` directly. | The selected state representation is already production full clone, and no live PUCT profile yet says Python is the limiter. Port only on measured economics failure. |
| Replace the teacher with ISMCTS or public-belief search now | Better information-set semantics and a path away from strategy fusion. | It would change both the teacher algorithm and the evidence pipeline before the first visit-based baseline exists. The task explicitly asks for the smallest actual teacher first. |
| Use only the old 8/32/128 admission gate | Cheapest way to decide whether Teacher-1 looks promising. | It ends one slice short of INT-4's user-visible win: no visit-trained student, no matched arena iteration, and no Study evidence. |
| Train only chosen-action versus visit/value arms | Reuses the existing two-arm runner. | It confounds the visit target with value supervision and fails the Project's matched-data policy-only versus policy/value KR. |
| Use the unavailable 512-game Teacher-0 checkpoint as incumbent | Stronger documented control. | The bytes are absent. A hash in a report is not a runnable model, and substituting a reconstruction would break provenance. |
| Generate a new diagnostic kata before the arena | Could isolate target entropy or delayed-control behavior cheaply. | No measured ambiguity exists yet. Competencies and the 2×2 training arms are already inside the runnable prototype; add a kata only if their result leaves two live explanations with different next builds. |

## Key decisions

- The first teacher is deterministic multi-world PUCT with uniform priors and
  random terminal leaves. This preserves the already-preregistered baseline.
- The production branching choice is settled: compact full clone. Search-loop
  placement, not representation, is the only performance branch left.
- Integrity gates fail closed. Quality and economics gates classify the result
  but do not prevent the bounded end-to-end iteration.
- The training experiment is a four-arm factorial at three independent seeds,
  not a single lucky checkpoint.
- Teacher-0 and Teacher-1 budgets are matched by realized p50 latency, never by
  the ambiguous integer `sims` label.
- `student+search` uses the student for both priors and leaf value. Its compute
  is latency-calibrated to Teacher-1 and its raw traversal count is reported.
- Search and replay inference use CPU. MPS is a batched training device only.
- Study evidence is generated from one actual replayed match decision and the
  predeclared seed-197 student, not from the best-looking seed or the synthetic
  fixture values.

### Wild success

The visit/value student consistently beats the frozen Teacher-0 incumbent,
student-guided PUCT improves it again at matched teacher latency, delayed-control
competencies move without legality regressions, and the Study artifact lets a
developer inspect exactly why one historical command won visits across hidden
worlds. The surprising win would be that a deliberately simple uniform/random
teacher is already a useful policy-improvement operator; the next iteration
would replace its priors and leaves with the admitted student and measure the
second crank.

### Wild failure

The teacher is legal and reproducible but its visit labels are unstable or
weak; students fit them while losing gameplay, or student-guided PUCT amplifies
value error. The 2×2 tells whether visits, values, or both transferred the
failure. A Python economics failure with intact quality sends the same
algorithm behind the Rust `FullCloneDriver`. A quality failure with stable
targets sends the next build to frozen policy priors and value leaves as
separate teacher ablations. A viewer or replay failure invalidates all results
and is repaired before any claim. None of these branches calls for another
representation benchmark or static kata by default.

## Scope

- In scope:
  - current w2 symmetric selected matchup;
  - deterministic PUCT budgets 8/32/128 and matched flat/policy controls;
  - exhaustive trajectory replay and sampled exact search replay;
  - viewer-boundary, legality, ABI, root-isolation, and accounting checks;
  - latency, throughput, RSS, label-cost, calibration, competency, and strength
    evidence;
  - 256-game visit/value dataset;
  - four matched training arms at three seeds;
  - teacher/student/policy-only/student+search arena;
  - one validated Study v1 artifact;
  - a checked-in report, immutable result manifest, and explicit next-build
    decision.
- Out of scope:
  - ISMCTS, CFR, public-belief solving, learned beliefs, or equilibrium claims;
  - semantic-program encoder work or structured-decoder ablations;
  - new card mechanics, decks, formats, or action ABI changes;
  - another clone/undo/COW representation benchmark;
  - GPU-batched MCTS or a general distributed search service;
  - Study UI, navigation, explanation copy, or research-consent behavior;
  - post-result hyperparameter, temperature, capacity, or budget tuning;
  - a diagnostic kata without a measured ambiguity and decision-changing
    outcome.

## Wave alignment

This design advances the Intelligence measures in `wave/intelligence/GOAL.md`
directly:

- it produces a runnable teacher, dataset, training loop, students, and arena
  behind one documented command;
- it compares search teachers and students on legality, competencies,
  seat-balanced strength, calibration, latency, throughput, label cost, and
  uncertainty at explicit budgets;
- its destructive ablations remove visit-distribution supervision and value
  learning at the boundary of one working prototype;
- it emits attributable, viewer-safe policy/search evidence through the
  versioned Study contract without creating a second legality, replay, or
  hidden-information system;
- every result pins matchup/content, viewer boundary, model/opponent cohort,
  compute, seeds, and uncertainty before making a strength claim.

Semantic policy inputs, typed programs, and structured decoding remain outside
INT-4 because this task advances the independent Search Teacher Project. The
teacher consumes authoritative real positions and legal offers now; it does
not wait on semantic-policy admission work that the wave has explicitly
separated.

## Done when

- Directive v1 remains acknowledged in the INT-4 Task Session.
- One uv command resumes and completes all five stages under the frozen
  contract, or records a terminal cap/integrity failure without overwriting
  partial evidence.
- Teacher budgets and controls have three seed blocks, zero illegal commands,
  declared latency/RSS/throughput/label-cost receipts, and an explicit quality
  diagnosis.
- All 256 dataset trajectories replay exactly; all eight sampled roots reproduce
  action, visits, Q, value, world statistics, and tree metadata exactly.
- Twelve matched checkpoints exist: four arms × seeds 197/419/887, with
  content-addressed receipts and held-out policy/value metrics.
- The four-agent arena and chosen-versus-visit ablations report every training
  seed, seat-balanced game blocks, competencies, calibration, and native/matched
  compute.
- The fixed admission rule produces either an admitted agent or
  `prototype_failure` with one predeclared next build.
- One actual historical position validates against Study v1 in Rust and Python,
  contains no opponent-private hand or RNG seed, and keeps policy mass, visits,
  values, robustness, and uncertainty distinct.
- Focused Python tests pass via `uv run --extra dev pytest`; all Rust changes
  pass debug `cargo test --manifest-path managym/Cargo.toml` before any release
  build evidence is accepted.
- The result is integrated into the Intelligence experiment ledger and memory,
  then the completed PR is landed with `lf pr land -c`.

## Measure

Before the run, freeze:

- w2 observation/action/protocol/content/engine/source hashes;
- exact Teacher-0 checkpoint hashes and same-host latency calibration;
- teacher/data/training/evaluation seeds and all arena cells;
- host topology, Python, torch, MPS, Rust build, worker count, and caps;
- 16 wall hours, 64 core hours, four workers, and 4 GiB total artifacts.

Report:

- **Integrity:** illegal commands, offer/mask mismatches, root mutations,
  viewer-equivalence mismatches, replay mismatches, truncations, non-finite
  targets, cap hits.
- **Teacher quality:** per-budget/block strength, competencies, action
  agreement, JS divergence, visit entropy/coverage, nodes, depth, root-value
  Brier/ECE.
- **Economics:** p50/p95 decision latency, traversals/s, labels/s/worker,
  core-seconds and bytes per 1,000 labels, baseline/peak/delta RSS, total
  wall/core time.
- **Learning:** CE, KL, target entropy, top-1, root-target error,
  terminal-outcome Brier/ECE, checkpoint size, training time, batch throughput,
  and all 2×2 effects per seed.
- **Arena:** all fixed pairings per seed and seat, Wilson intervals for game
  noise, training-seed range for method variation, competencies, and matched
  p50 search cost.
- **Study:** artifact/replay/model/engine hashes, complete per-alternative
  quantities, and cross-language validation receipts.

The iteration is better only if the predeclared admission rule selects
`student` or `student+search`. Otherwise its success criterion is an honest,
replayable prototype failure whose branch table determines the next build.
