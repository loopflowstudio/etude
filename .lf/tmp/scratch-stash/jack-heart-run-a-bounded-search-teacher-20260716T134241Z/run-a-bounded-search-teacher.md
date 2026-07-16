# W2-234: Bounded Teacher-1 search and distillation pilot

## Problem

W2-234 asks whether a search teacher on the current w2 observation and legal-action
ABI produces policy and value supervision that a practical student can learn. The
beneficiaries are the Intelligence research loop and every later semantic-policy
experiment: they need a strong, attributable teacher before model architecture is
blamed for weak labels.

PRs #105 and #106 already landed two useful but deliberately incomplete slices:

- Teacher-0 is flat determinized Monte Carlo. Its policy target is a score softmax,
  not MCTS visits.
- Teacher-1 is deterministic multi-world PUCT with uniform priors and random leaf
  playouts. It emits real root visits and root values, but only smoke evidence exists.

The in-flight `jack-heart/resilient-search-datagen` recovery owns
`experiments/runners/run_distill_datagen.py`,
`experiments/runners/run_search_supervised.py`, `manabot/sim/distill.py`, and its
focused tests. Its active bounded launch is Teacher-0 (`flat_mc`, 256 playouts per
legal action, seed 197), not Teacher-1. It can establish crash recovery, cost, and a
frozen policy control; it cannot close any MCTS-visit or multi-seed KR. This work
must not modify those files or start competing compute until that recovery lands.

The smallest non-overlapping next experiment is therefore **Teacher-1 quality and
evidence admission**: freeze the contract, evaluate three PUCT budgets against
matched controls and scripted competencies, and prove replay and information
safety. Only a passing high-budget teacher earns the much more expensive
three-seed distillation stage.

This directly advances the wave measures that require a current-ABI search teacher,
an overnight supervised visit-target experiment, and later matched-compute strength.
It deliberately does not wait for semantic inputs and does not claim
information-set-consistent search.

## The demo

After the resilience PR lands, a developer runs:

```bash
uv run experiments/runners/run_teacher1_pilot.py \
  --contract experiments/contracts/w2-234-teacher1-pilot-v1.json \
  --stage teacher-gate \
  --out-dir .runs/w2-234-teacher1-pilot-v1
```

The command resumes atomically after interruption, then prints one table with the
8/32/128-traversal Teacher-1 ladder, matched-wall Teacher-0 and frozen-checkpoint
controls, five competency scores, replay/legality results, root-value calibration,
and p50/p95 cost. It either writes `teacher_gate: pass` and the exact command for the
three-seed distillation stage, or writes a named failure branch and produces no
student checkpoints.

## Approach

### 1. Land a real pre-registration before confirmatory MCTS compute

The implementation iteration adds a checked-in, machine-readable contract plus a
timeless report stub. The contract is invalid until every placeholder has been
resolved and hashed. The runner refuses an invalid or source-mismatched contract.

The contract pins:

- world `w2` and the complete tensor-shape tuple;
- source commit, `managym` extension SHA-256, engine state-hash contract version,
  content manifest and digest, observation schema hash, action enum/encoder hash,
  and experience-protocol schema hash;
- the symmetric `INTERACTIVE_DECK` matchup by canonical JSON and SHA-256;
- Teacher-1 as `determinized_puct_uniform_prior_random_leaf/v1`, with total root
  traversals per decision, four determinizations, `c_puct=1.5`, and a 2,000-step
  leaf cap;
- Teacher-0 as `flat_determinized_mc_score_softmax/v1`, explicitly preserving its
  different budget semantics: playouts per legal root action;
- the exact recovery checkpoint path and SHA-256 used as the policy-only frozen
  control, after the recovery manifest reaches a terminal successful state;
- opponent classes as distinct enums (`random`, `scripted`, `search`,
  `checkpoint`), never one pooled “baseline” label;
- deal, search, evaluation, dataset, and training seeds;
- the Apple M4 Max host identity, Python 3.12, PyTorch/MPS versions, at most four
  confirmatory search workers, artifact caps, and wall/core-hour caps;
- all predictions, gates, kill rules, and the result-contingent branch table below.

The active Teacher-0 recovery remains exploratory for W2-234 because it began with
one training seed and without the Teacher-1 contract. Its artifacts may be frozen as
controls and used to calibrate cost, but its result may not change predictions or
gates.

### 2. Admit the trajectory and information boundary before judging strength

Teacher-1 continues to search cloned `managym.Env` roots. Every hidden world is
created by `clone_env()` followed immediately by `determinize(...,
perspective=acting_seat)`; no value, prior, or action selection may inspect the true
opponent hand before determinization. Separate trees are built per determinization
and only root visits/Q values are aggregated. The artifact calls this
`ensemble_determinized_puct`, not ISMCTS, public-belief search, or information-set
consistent MCTS.

The live match has one coordinator. At every authoritative decision it emits two
linked records:

1. a learner row containing only the acting viewer's encoded observation, exact
   legal mask, selected action, declared target kind, root visits, root Q values,
   root value, seat, and terminal outcome; and
2. an audit record containing the viewer-safe protocol frame/legal offers, the
   prompt-bound chosen command, match/revision IDs, search seed/spec/cost, opponent
   class, and full run provenance.

The learner never receives the deal seed, opponent private hand, authority state
hash, or audit sidecar. Seeds and private authority witnesses remain in the audit
bundle solely to reproduce the run.

Replay reconstructs the match from its pinned config and seed, regenerates each
acting-viewer frame and legal offer set, checks the frame hash, reapplies the chosen
command through the same coordinator, and checks the next frame and terminal
outcome. A deterministic 1% root sample also reruns search and requires exact visit,
Q, root-value, and selected-action equality. The verifier may compare the internal
`MatchStateHash` in memory, but no hidden-state digest is placed in the learner
shard or public viewer frame.

Hard admission gates, before any strength result is read:

- zero illegal teacher or student actions;
- zero encoded-action truncations and exact legal-mask/offer cardinality agreement;
- zero replay frame, command, outcome, or sampled-search mismatches;
- root visit mass is finite, non-negative, legal-only, and sums exactly to the
  declared total traversals;
- root values and Q values are finite and in `[0, 1]`;
- the authoritative root is unchanged by every search call;
- opponent hand rows are absent from the acting viewer projection;
- playout cap rate is below 0.1%.

Any failure invalidates the run. It is an engine/coordinator/ABI defect, not a weak
teacher result.

### 3. Run the three-budget Teacher-1 quality gate

The fixed Teacher-1 ladder is:

| ID | Total traversals/decision | Worlds | Traversals/world | Purpose |
|---|---:|---:|---:|---|
| `t1-8-w4` | 8 | 4 | 2 | root-only/low-information anchor |
| `t1-32-w4` | 32 | 4 | 8 | bounded middle point |
| `t1-128-w4` | 128 | 4 | 32 | candidate data teacher |

Holding worlds and `c_puct` fixed makes this a budget curve rather than a hidden
algorithm sweep. Raw normalized visit counts are the policy target; no score
softmax, visit temperature, Dirichlet root noise, policy-target pruning, or
post-result tuning is introduced. The high-budget point is small by AlphaZero or
KataGo standards, which is precisely why label stability is measured rather than
assumed.

Before the confirmatory run, a read-only calibration on the recovery host maps each
Teacher-1 tier to the nearest Teacher-0 `sims/action` with median decision latency
within 10%. Those three integer mappings and the calibration artifact hash are
committed into the contract; they cannot change after results begin. If no mapping
is within 10%, both costs are reported and the cell is named `cost-unmatched`—it
cannot support a matched-compute claim.

Evaluation is paired and deal-diverse:

- 48 seat-balanced games per Teacher-1 budget versus uniform random;
- 48 per budget versus the exact frozen policy-only checkpoint;
- 48 per budget versus its committed matched-wall Teacher-0 control;
- 100 runs per budget on each of S1–S5 against the existing deterministic scripted
  villains;
- a fixed bank of 192 acting-viewer roots, searched three times per budget with
  independent search seeds to measure top-action agreement, Jensen-Shannon
  divergence, target entropy, visit coverage, and value variance;
- Wilson intervals for game/scenario sampling noise and paired bootstrap intervals
  over deal seeds for head-to-head deltas.

Pre-registered predictions:

1. Strength rises with search budget: `t1-128-w4` wins at least 55% head-to-head
   against `t1-8-w4`, at least 55% against the frozen policy-only control, and at
   least 80% against random.
2. Root labels stabilize: at 128 traversals, independent searches agree on the top
   action at least 70% of nontrivial roots and median pairwise Jensen-Shannon
   divergence is at most 0.10.
3. Tree reuse is real: mean depth and nodes beyond the four world roots increase
   from 8 to 128 traversals; a root-only degeneration fails the teacher.
4. Current Teacher-1 will remain imperfect at delayed control, but must show some
   usable mechanism signal: at least one of S1, S2, or S5 reaches 20% correct at
   128 traversals, and no scenario is more than 10 points below random.
5. The 128-traversal teacher stays within 500 ms p95 per decision on the declared
   host, produces at least two labels/second/worker, and completes the entire gate
   within 8 wall hours / 32 core-hours.

Teacher quality passes only if all integrity gates pass and predictions 1–5 pass.
Root-value Brier score, ECE, reliability bins, and Spearman ordering against terminal
outcomes are reported, but are diagnostic rather than hard gates because the root
value estimates random-leaf continuation while the realized game follows the
teacher. Calibration against terminal outcomes and imitation of root values are
kept as two differently named measurements.

### 4. Distill only after the teacher passes

If the gate passes, freeze `t1-128-w4` and generate at most 384 self-play games or
50,000 decisions, whichever arrives first. The teacher is immutable for the whole
dataset; this is fixed-teacher self-play, not “latest” self-play. Generation uses at
most four workers, writes incremental atomic shards, and retains a terminal manifest
even on cap or interruption. Data is capped at 2 GiB and the complete pilot at 12
wall hours / 48 core-hours, including the teacher gate, training, and final
evaluation.

Train six primary students on the exact same game-level split and data:

| Arm | Policy target | Value target | Training seeds |
|---|---|---|---|
| chosen | one-hot selected command | Teacher-1 root value | 197, 419, 887 |
| visits | normalized root visits | Teacher-1 root value | 197, 419, 887 |

For a given seed, both arms share the initial weights, game split, minibatch order,
optimizer, learning rate (`1e-3`), batch size (1,024), 25-epoch cap, shared encoder,
policy/value heads, and current default Agent capacity. No fine-tuning and no
capacity sweep occur in the primary comparison. Three training seeds are the unit
of method uncertainty; game-level CIs describe only evaluation noise for one
checkpoint.

Sample efficiency is measured at fixed 10%, 25%, 50%, and 100% game-prefixes for
held-out KL/action agreement/value metrics. Only the 100% students receive the full
gameplay battery. Prefix selection is by whole game and identical between arms and
seeds.

Pre-registered student prediction and gate:

- visits beats chosen by at least 0.05 nats in held-out KL to the canonical visit
  distribution while remaining within five points of chosen-action top-1 agreement;
- visits has lower held-out visit KL in at least two of three paired training seeds;
- both arms reduce root-value MSE below the untrained 0.25 baseline, while terminal
  Brier/ECE are reported separately;
- the visits student wins at least 55% in the paired, seat-balanced aggregate over
  the chosen student and is no more than five points weaker than chosen against the
  frozen policy control;
- seed-level means, ranges, and bootstrap intervals are reported; no game-pooled
  pseudo-replication is used for a method claim.

Failure to beat the chosen-action arm is a label-formulation result, not permission
to tune temperature after seeing the data.

### 5. Close with honest matched-compute comparisons

For each of the six primary checkpoints, report batch-1 p50/p95 inference latency,
batch-256 throughput, parameter count, and checkpoint size on the pinned host. The
final table contains:

- frozen policy-only control;
- chosen-target student policy-only;
- visit-target student policy-only;
- the frozen `t1-128-w4` teacher;
- matched-wall Teacher-0;
- visit student plus search.

The current uniform-prior Teacher-1 cannot honestly be called “student plus search”
because it never reads the student. If the teacher and distillation gates pass, the
next serial PR adds student policy priors and student value leaves to the same PUCT
surface, then calibrates its budget to the teacher's measured p50 within 10%. It is
evaluated without regenerating or retraining the primary students. A pre-existing
policy-rollout or value-search player may appear as an explicitly named diagnostic,
but cannot be relabeled neural PUCT.

No table calls compute “matched” unless realized median decision latency is within
10% and both sides used the same host/load window. Search traversals, network
forwards, wall time, core time, p50/p95, and label cost are all printed; unequal
budgets remain visible.

## Existing evidence reconciled against the KRs

| KR | Landed evidence | Status after #105/#106 | Smallest missing evidence |
|---|---|---|---|
| Checked-in experiment contract | Generic runtime manifests pin much of w2; smoke docs name target kinds and budget semantics | Partial; no exact three-budget contract, action/protocol hashes, numeric branch gates, three training seeds, or frozen control hash | Commit the complete v1 contract after recovery artifacts exist and before Teacher-1 compute |
| Viewer-safe replayable trajectories | Decision shards contain acting-viewer tensors, legal masks, actions, visits, values, and provenance | Not met; Teacher-1 doc explicitly says engine trajectories are not replayable | Coordinator-backed audit trajectory plus exact replay and sampled search replay |
| Three-budget teacher evaluation | Deterministic smoke proves tree growth and target shape | Not met; two games at two traversals are plumbing only | 8/32/128 ladder, scripted competencies, checkpoint/random/matched-search controls, calibration and cost |
| Multi-seed visit-vs-chosen distillation | One smoke seed exercises both losses | Not met; no method uncertainty or bounded confirmatory dataset | Three paired training seeds after teacher gate, matched data/capacity, sample-efficiency curve |
| Matched-compute final comparison | Existing players expose search cost; Teacher-1 and Teacher-0 semantics are explicit | Not met; no frozen full battery and no student-aware PUCT | Realized-latency matching plus a separately named student-guided PUCT continuation |

No W2-234 KR is marked complete from the current substrate alone.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Is Teacher-1 actually MCTS? | Yes. `manabot/sim/mcts.py` performs adaptive PUCT selection, node reuse, alternating-player backup, and returns real visit counts. It uses uniform priors and random leaves. | Keep the algorithm name precise; evaluate it before adding neural guidance. |
| Does Teacher-1 preserve the information boundary? | Search clones the authoritative root and calls engine `determinize` from the acting player's perspective; the engine resamples the opponent hand from hand+library and shuffles unknown libraries while preserving public state. | Add a hard audit/replay gate and never expose authority seeds/hashes as model features. Call the method ensemble determinized PUCT, not information-set search. |
| Are Teacher-0 and Teacher-1 `sims` comparable? | No. Teacher-0 means playouts per legal action; Teacher-1 means total traversals across worlds. | Match by realized latency and report actual playouts/traversals, not equal CLI integers. |
| Can the in-flight recovery close Teacher-1 KRs? | No. Its live manifest declares `flat_mc`, 256 playouts/action, Teacher-0 score-softmax plus outcome targets, and one seed. | Let it finish as crash-safety/cost/control evidence; do not duplicate it or count it as MCTS evidence. |
| Is the current trajectory replayable? | No. Landed shards omit protocol frames/commands; the existing GUI protocol has viewer-safe frames and prompt-bound offers, while deterministic match-state hashes and seeded replays already exist as verification tools. | Add a linked learner/audit evidence boundary instead of putting private authority data in training rows. |
| Are visits automatically better labels? | No. AlphaZero uses search visit distributions, but KataGo found exploration can contaminate policy targets and introduced policy-target pruning. Low-budget visits may be sparse or unstable. | Compare visits with chosen actions on identical data, measure repeat-search JSD/action stability, and add no post-result temperature/pruning tweak. See [AlphaZero](https://arxiv.org/abs/1712.01815) and [KataGo](https://arxiv.org/abs/1902.10565). |
| Can determinized MCTS be promoted as safe imperfect-information play? | No. Determinization has strategy-fusion and non-locality failure modes; prior Magic work improved basic MCTS with ensemble determinization and rollout/move-generation changes, but did not remove that limitation. | Competencies and the existing D1 instrument remain gates; no exploitability or information-set-consistency claim follows from mirror win rate. See [Long et al.](https://doi.org/10.1609/aaai.v24i1.7562) and [Cowling et al. on Magic](https://doi.org/10.1109/TCIAIG.2012.2204883). |
| Are three seeds enough to pool all games into one CI? | No. Repo discipline and modern few-run evaluation guidance agree that training runs, not games, are the method unit. | Pair arms by seed, report seed-level effects and interval estimates, and retain game-level Wilson CIs only for each checkpoint. See [Agarwal et al.](https://proceedings.neurips.cc/paper/2021/hash/f514cec81cb148559cf475e7426eed5e-Abstract.html). |
| Is full clone a blocker? | No. W2-198 certifies full clone and clone-plus-undo equivalence; the task explicitly permits the proven full-clone path within cap. | Use full clone for this pilot, record the driver identity, and defer scaling/representation selection to Rules. |
| Will a scalar root value prove calibration? | No. Teacher root value estimates its search continuation; terminal outcome is generated by the played policy, and the wave has already measured scalar-V failures in undecided control states. | Name imitation MSE and terminal calibration separately; neither substitutes for competencies. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Start another large Teacher-0 or Teacher-1 run now | Fastest way to accumulate rows, but duplicates the recovery worker and would precede a complete contract | Rejected. It risks machine contention, corrupts causal ordering, and violates the explicit coordination boundary. |
| Treat #106's two-game smoke as the teacher gate | No new code or compute | Rejected. It validates plumbing only and has no strength, competency, replay, matched-control, or uncertainty evidence. |
| Distill Teacher-1 immediately, then evaluate it | Could produce an overnight checkpoint sooner | Rejected. A failed student would confound teacher weakness with targets, optimization, and capacity—the exact ambiguity W2-234 exists to remove. |
| Build neural PUCT before evaluating uniform/random Teacher-1 | Closer to AlphaZero/KataGo and enables student-plus-search | Rejected for the first post-recovery slice. It changes priors, leaves, batching, and cost at once before the landed reference has a measured baseline. It remains the named continuation after the gate. |
| Use only win rate versus random | Cheap and historically saturated | Rejected. Exp-09 proves it can hide total failure on delayed control. Strength, competencies, label stability, legality, and cost must travel together. |
| Use current encoded shards as “replayable enough” | Avoids protocol/audit work | Rejected. They cannot prove the legal offer/command history or exact authoritative replay required by the KR. |

## Key decisions

1. **Teacher gate before student training.** This is the causal boundary that keeps
   teacher weakness separate from every learning failure.
2. **Teacher-1 stays unchanged for its baseline.** Four worlds, uniform priors,
   random leaves, no root noise, and raw visit targets are frozen across all three
   budgets.
3. **Quality means more than beating random.** The high teacher must improve with
   budget, beat the policy control, show stable labels, clear at least one delayed
   competency, replay exactly, and fit the latency cap.
4. **Viewer-safe learner data and private audit evidence are separate artifacts.**
   Provenance does not become an accidental hidden-state feature.
5. **The recovery result is a control, not a result-dependent design input.** Its
   successful checkpoint and manifest hashes are frozen before the MCTS contract
   lands; predictions do not change.
6. **Three paired training seeds are mandatory.** They share a fixed dataset to
   isolate target formulation. Game-level sample size is never presented as method
   replication.
7. **Matched compute is observed, not nominal.** Equal `--sims` values are forbidden
   across algorithms with different budget semantics.
8. **No student-plus-search fiction.** Student-guided PUCT is a gated continuation,
   not a label applied to uniform Teacher-1.
9. **No semantic dependency and no branching scale-up.** The pilot runs on w2 and
   full clone; semantic programs and the Rules branching choice remain outside it.

## Wild success

The 128-traversal teacher is clearly stronger than the frozen policy and low-budget
PUCT, its visit distribution is reproducible across search seeds, one or more
delayed-control competencies lift off zero, and every decision replays through the
viewer-safe command boundary. Across all three paired training seeds, visit targets
reduce held-out KL and produce a stronger policy-only student than one-hot targets.
The result is not merely a checkpoint: it is a one-command, resumable evidence
factory that can accept a neural PUCT teacher later without changing target schemas
or evaluation semantics.

## Wild failure

Six months later the pipeline is removed because sparse low-budget visits encoded
PUCT exploration noise, root values were mislabeled as calibrated outcomes, the
full-clone Python loop made every label uneconomic, and a seed in the learner rows
let the model or evaluator reconstruct hidden state. The design prevents that
failure by measuring repeat-search stability, separating value meanings, enforcing
latency and artifact caps, splitting audit from learner data, and stopping before
training whenever the teacher or boundary fails.

## Result-contingent branch table

| Observed result | Diagnosis | Next branch |
|---|---|---|
| Any illegal/truncated action, viewer leak, or replay mismatch | Decoder/coordinator/ABI defect | Invalidate all results; repair the evidence boundary before more search |
| Tree does not grow beyond roots or budget metrics disagree | Teacher implementation/budget-accounting defect | Fix Teacher-1 reference; rerun all budgets |
| 128 traversals fail strength or competency while latency passes | Teacher weakness: uniform priors/random leaves | Do not distill; add frozen policy priors and value leaves as isolated ablations |
| Teacher improves but breaches latency/label-cost cap | Inference/branching economics | Keep algorithm result; move behind `BranchDriver` and batch leaf inference before data generation |
| Visits unstable across search repeats | Label formulation/search-budget failure | Do not train on visits; raise/search-budget or pre-register pruning/temperature in a new experiment |
| Teacher passes; chosen learns, visits does not | Visit-target formulation failure | Inspect entropy/coverage and pre-register exactly one target transform; do not increase capacity |
| Teacher passes; neither arm fits training targets | Optimization failure | Run a tiny memorization/LR diagnostic on the frozen data |
| Training fit succeeds; both arms fail held-out | Capacity or dataset-diversity failure | Distinguish with one matched 2x-capacity diagnostic versus more games, not both |
| Root-value imitation succeeds; terminal calibration fails | Value-target mismatch | Retain policy result; compare terminal outcome supervision in a new value-only ablation |
| Visit arm wins validation but not gameplay | Distribution shift | Inspect competency/action buckets; do not claim distillation success |
| Visits beat chosen across seeds and gameplay | Useful MCTS supervision | Add student-guided PUCT and run the final matched-compute comparison |

## Coordination and execution order

1. Do not edit or run the four recovery-owned files while
   `resilient-search-datagen` is active.
2. Wait for that PR to merge through Loopflow's durable task/wait surface; do not
   poll GitHub or manipulate its worktree.
3. Run `lf rebase --plan`, then `lf rebase` in this worktree.
4. Verify the recovery manifest reaches a terminal state. Freeze any control
   checkpoint only by exact SHA-256; an incomplete recovery contributes cost data
   but no checkpoint control.
5. Land a pre-registration/evaluator PR before the first confirmatory Teacher-1
   search. It owns new contract/evaluation/evidence files and initially leaves the
   recovery-owned training path untouched.
6. Run the teacher gate. If it fails, publish the report/data and stop the student
   stage.
7. If it passes, extend the now-merged resilient generation path rather than
   creating a second shard implementation, run three paired training seeds, and
   publish all artifacts.
8. Land serial PRs with `lf pr land --next <slug>` while W2-234 remains open;
   propose `lf pr land -c` only after every KR has direct checked-in evidence.

## Scope

- In scope: a complete Teacher-1 pre-registration; exact w2 engine/content/ABI and
  opponent identities; viewer-safe replayable evidence; three search budgets;
  scripted competencies; random, frozen checkpoint, and matched-search controls;
  latency/throughput/label-cost accounting; a gate-controlled three-seed
  visit-versus-chosen distillation; matched-data/capacity metrics; and the branch
  decision.
- In scope after the teacher gate: student-guided PUCT as the minimum honest
  student-plus-search comparison.
- Out of scope: semantic-program inputs, structured decoder redesign, rules or
  content expansion, public-belief/CFR claims, likelihood-weighted beliefs,
  hidden decklists, moving latest-self leagues, exploitability promotion from this
  mirror alone, branching-representation selection, GUI/Study product work, and
  unbounded self-play scaling.

## Done when

The kickoff/design slice is done when this document has been reconciled after the
resilience merge and the implementation plan contains no overlapping file or run
ownership.

The Teacher-1 evidence slice is done when:

```bash
uv run experiments/runners/run_teacher1_pilot.py \
  --contract experiments/contracts/w2-234-teacher1-pilot-v1.json \
  --stage teacher-gate \
  --out-dir .runs/w2-234-teacher1-pilot-v1

uv run experiments/runners/run_teacher1_pilot.py \
  --contract experiments/contracts/w2-234-teacher1-pilot-v1.json \
  --verify .runs/w2-234-teacher1-pilot-v1

uv run pytest tests/sim/test_mcts.py tests/sim/test_teacher1_evidence.py \
  tests/sim/test_teacher1_pilot.py
```

passes; the contract predates result artifacts; the checked-in JSON/report contains
the complete ladder, competencies, replay receipts, controls, uncertainty, latency,
throughput, label cost, and branch decision; and no student checkpoint exists after
a failed teacher gate.

The full Task is done only when a passing teacher also has three-seed
visit-versus-chosen evidence and a realized matched-compute table including an
honestly student-guided search arm, or when a checked decision record kills and
redirects the MCTS branch according to the pre-registration.

## Measure

Before/controls:

- Teacher-0 recovery manifest, checkpoint hash, score-softmax target, outcome-value
  metrics, p50/p95, throughput, and label cost;
- frozen policy-only checkpoint strength and competencies;
- uniform random and scripted scenario baselines;
- full-clone branching receipt and host/load identity.

Teacher-1:

- legality/truncation/replay/search-replay counts;
- win rate and Wilson interval overall and per seat at 8/32/128 traversals;
- paired deltas versus frozen policy, random, and matched-wall Teacher-0;
- S1–S5 correct rates and behavioral details;
- root target entropy, visit coverage, top-two margin, repeated-search top-action
  agreement/Jensen-Shannon divergence, root value variance, Brier/ECE/reliability,
  tree nodes, worlds, depth, caps, p50/p95 latency, labels/s, transitions/s, wall
  time, core-hours, bytes, and dollars.

Students:

- per-seed and aggregate held-out visit KL, chosen-action agreement, target entropy,
  root-value MSE, terminal Brier/ECE, sample-efficiency curves, competencies,
  seat-balanced strength, paired student head-to-head, batch-1 p50/p95, batch-256
  throughput, checkpoint size, teacher cost, student cost, and total compute.

“Better” means the exact pre-registered teacher and student gates above, not a
post-hoc composite score.
