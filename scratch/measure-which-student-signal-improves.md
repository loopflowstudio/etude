# INT-8: Which student signal should guide the next PUCT teacher?

## Problem

INT-4 retained a real, replayed search corpus and two policy-only students that
differ only in their policy target: chosen actions versus root visit
distributions. INT-8 should answer the next systems question: at the same
bounded PUCT compute, does either frozen student provide a better exploration
prior than uniform search?

The beneficiary is the Search Teacher and Student Arena loop. A useful result
chooses which policy signal deserves a new, larger corpus. A null result kills
scaling these one-seed smoke students as PUCT guidance. It does not rate or
promote an agent. The retained corpus has 507 labels and one training seed, so
the strongest permitted conclusion is a bounded engineering decision, always
named `engineering_smoke_only_no_admission_claim`.

The landed INT-6 arena already owns the authoritative matchup, Commands,
replay, competencies, profiling, payoff matrix, and rating scale. INT-8 extends
that implementation in place with the one missing candidate semantics: a
checkpoint-bound PUCT player that uses checkpoint logits only for legal-action
priors and retains the existing random terminal leaf evaluator. The generic
`agent_puct` path is not reused because it also consumes checkpoint value
output and would change two factors at once.

The fail-closed boundary is the retained input, not the missing adapter. If the
Task-owned bytes, manifests, or exact current-loader compatibility fail, stop
with exact evidence. If they pass, implement the minimal INT-6 extension and
run the three-arm diagnostic. No second arena, INT-9 edit, managym edit, or
shared authority-kernel change is permitted.

## The demo

After the retained input is frozen, a developer runs:

```bash
uv run python experiments/runners/run_int8_student_signal_guidance.py \
  --input-manifest \
    experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/input-manifest.json \
  --out-dir .runs/int-8-student-signal-guidance-v1/result
```

The command verifies every frozen byte and loader check, freezes the unchanged
INT-6 smoke anchor cohort under the advanced implementation receipt, executes
one complete uniform/chosen/visit diagnostic cohort through the existing arena,
verifies every Command replay, and prints the three prior arms at matched
compute. It ends with exactly one of
`next_corpus_chosen_action`, `next_corpus_visit_distribution`, or
`kill_retained_smoke_policy_guidance`, alongside the immutable artifact path.

## Computable contract

### User-visible outcome

The developer running INT-8 sees one verified, smoke-only INT-6 comparison of
uniform, chosen-policy-only, and visit-policy-only PUCT priors at matched
compute. The command reports the complete arena and mechanism metrics and
terminates in exactly one next-corpus-or-kill decision. It never presents the
result as admission, promotion, gameplay strength, or a method-level claim.

### End-to-end proof

From a fresh canonical checkout with no sibling INT-4 worktree, run the demo
command against the checked-in input manifest. The proof crosses the exact
13-file payload and separate contract bytes, all four current checkpoint
loaders, the checkpoint-prior evaluator, the existing INT-6 preflight/match/
competency/profile/replay/matrix consumers, and the final verifier. It holds
only when the command produces a complete verified 28-cell artifact, exact
replay of every new Command trace, the declared mechanism and cost metrics,
unchanged frozen anchor identities/scale, and one immutable decision.

### Source of truth

The retained-input authority is
`experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/input-manifest.json`,
which binds the byte-identical payload leaves and the separate exact contract
copy. The evaluation authority remains the existing INT-6 `ArenaKey`, frozen
anchor registrations/cohort, semantic Command traces, replay verifier, payoff
matrix, competency rows, and matched-root profiles. The human-readable report,
summary metrics, and final decision are derived views and must recompute from
those retained records.

### Affected surfaces and consumers

- The checked-in INT-8 input manifest, payload, contract dependency, and their
  conversion-free verifier/loader gate.
- `PlayerRegistration` serialization for the additive smoke-only
  `policy_prior_puct` kind and one arena-level resolver for that kind.
- Existing INT-6 candidate preflight, match execution, competency evaluation,
  matched-root profiling, Command replay, payoff-matrix/rating fit, resource
  ledger, artifact manifest, and verification consumers.
- The INT-6 arena CLI's smoke-only three-candidate mode and the documented
  one-command INT-8 runner that orchestrates the gate and diagnostic.
- Existing player kinds, production arena invocations, and frozen base-v1
  anchor payloads remain byte- and behavior-compatible. There is no wire DTO,
  Etude app, Study consumer, or external automation change in this Task.

### Absent and error states

- Before the initial rescue, a missing sibling source file or unexpected
  source path fails the 13-file copy and produces exact path evidence; no
  partial payload becomes authoritative.
- A payload, contract, manifest, shard, checkpoint, replay, or identity
  mismatch fails before checkpoint loading or experiment execution.
- Current-loader rejection, nondeterministic/nonfinite inference, illegal-mask
  mass, invalid prior normalization, or checkpoint substitution emits
  `input_incompatible`; there is no conversion, retraining, uniform-prior
  fallback, or evidence adaptation.
- Missing arena adapter support is implementation work, not `evidence_wait`.
  An incomplete cell schedule, replay/integrity failure, private exposure,
  root mutation, cap breach, or invalid matrix makes the diagnostic artifact
  invalid and emits no next-corpus decision.
- An empty advantage, a tie, or failure of either learned arm to clear every
  registered gate yields `kill_retained_smoke_policy_guidance`; it is not an
  execution error and says nothing general about PUCT or supervision methods.

### Operational boundary

The entire diagnostic is local, CPU checkpoint inference, network-free, and
run through `uv`. It is capped at 2 wall hours, 8 core hours, 1 GiB of new
artifacts, and 4 workers, with `max_steps=2000`, four sampled worlds, no root
noise, 8/32/128 matched-root traversals, and only 32-traversal candidates in
arena gameplay. Common comparison seeds are mandatory; a cap or identity
violation stops rather than degrading the run.

### Exclusions

No checkpoint retraining, reconstruction, conversion, or policy-value arm
comparison; no Teacher-0 controls; no INT-5, INT-7, INT-9, or INT-12 work; no
managym, reusable search/MCTS, or authority-kernel edits; no second arena; no
base-v1 scale mutation; and no Study, admission, promotion, gameplay-strength,
or method-level claim.

## Approach

### 1. Freeze the recovered evidence before writing experiment code

The first implementation action is a durable byte rescue, not a loader probe
and not a new runner. `.runs/` is gitignored and may hold only staging and run
output; it is not an input authority.

1. Copy the complete 13-file source tree from
   `/Users/jack/src/etude.run-the-first-visit-based/.runs/int-4-visit-teacher-smoke-post-pr132-v1`
   into
   `.runs/int-8-student-signal-guidance-v1/staging/<nonce>/payload`. Require
   this exact closed allowlist of regular files and reject every other path,
   symlink, device, or socket:

   ```text
   dataset/manifest.json
   dataset/shard_000.npz
   dataset/shard_001.npz
   manifest.json
   report.md
   study-artifact.json
   training/chosen_policy_only-seed-197-9004b87e2be4a893.pt
   training/chosen_policy_value-seed-197-1c31b6c4f76ad86f.pt
   training/manifest.json
   training/visit_policy_only-seed-197-c2c8dcec02dbcf19.pt
   training/visit_policy_value-seed-197-b97a4796a6cbcad0.pt
   trajectory-audit.json
   verification.json
   ```
2. Hash only the staged copy. For each of the 13 files record the relative
   path, byte count, and SHA-256. Define the payload identity as the repository
   canonical SHA-256 of that sorted file table; metadata such as source path,
   permissions, and mtime is not part of content identity. Require the
   recovered payload tree identity to be
   `13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0`.
3. Install the exact payload at the checked-in canonical location
   `experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/payload`.
   This follows the repository's existing `experiments/data/` convention for
   retained JSON and NPZ evidence. The payload remains byte-for-byte identical;
   no original manifest is rewritten.
4. Freeze an exact byte copy of
   `experiments/contracts/int-4-visit-teacher-iteration-v1.json` at
   `experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/dependencies/int-4-visit-teacher-iteration-v1.json`.
   It is a separate dependency because the 13-file recovered payload references
   the contract but does not contain it. Require the copied file's raw-byte
   SHA-256 to be
   `bbbba03856b74047a8f8cec44f23a3c28b92558bff0001c602e4211c75d260a7`
   and independently parse and recompute its recorded canonical contract
   identity as
   `9c3f0f600b70ca4fef7131086f6d9b350f9612e690cbb9d621e961a5de27d03c`.
5. Write the Task-owned receipt at
   `experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/input-manifest.json`,
   beside and never inside the payload. It binds the 13 leaf identities, the
   payload tree identity, and the separate exact contract path and identity.
   It may map frozen relative paths to absolute paths embedded in the original
   manifests, but it may not adapt those manifests.
6. Complete and check in the payload, dependency, and receipt before writing
   experiment code. Deleteable `.runs/` staging is not part of the retained
   input. After the initial copy, never read the sibling worktree again. Every
   later gate and run resolves only through the checked-in receipt, and the
   documented runner must pass from a fresh canonical checkout where the
   sibling INT-4 worktree does not exist.

The input gate recomputes and requires:

- copied contract raw-byte SHA-256
  `bbbba03856b74047a8f8cec44f23a3c28b92558bff0001c602e4211c75d260a7`
  and canonical contract identity
  `9c3f0f600b70ca4fef7131086f6d9b350f9612e690cbb9d621e961a5de27d03c`;
- profile SHA-256
  `88181734b699253374b9d99c74e278f42c8dff8de5f85ac81fb65ca1fb39a8e4`;
- shard SHA-256 values
  `8338bdc0bbdf964abd71a94ca3de477c884ee3c65c06b5e7236361c2b162eab6`
  and
  `b31d5087b3a9b539acf6d643a31a50f4a6cfe100143cf76b47a08d20068aa03e`;
- checkpoint SHA-256 values
  `9004b87e2be4a8934b9e276c5249c8881ab74aa96d6b0327c4fc71fcd2f051dc`,
  `1c31b6c4f76ad86fd1d00fe7273212ab9e0e25d660ff8a01f92455d551a29801`,
  `c2c8dcec02dbcf19ffcf3441ecc70f3d36b505b8439e1b2b9c7853cd52b35624`,
  and
  `b97a4796a6cbcad0188257d72e9078cb991b96252af4b2f475977ca261e82b2e`;
- dataset provenance source commit
  `1a993155ea08962a39e0a678743b3aa831016692` on both shards;
- exactly two shards, four games, 507 labels, four named checkpoints, and no
  unmanifested checkpoint or shard;
- the original verification receipt's 175 decisions, zero frame, Command,
  outcome, search-action, visit, Q, value, world, and metadata mismatches, zero
  private-card exposures, and `passed: true`;
- the root manifest's `engineering_smoke_non_admission` evidence class and the
  result diagnosis `engineering_smoke_only_no_admission_claim`.

Any mismatch writes a closed failure receipt containing expected and actual
path, byte count, and digest, then exits before loaders, preregistration, or
search run.

### 2. Prove current-loader compatibility without changing bytes

Only after the content-addressed copy passes:

1. Load both frozen shards through the current `load_shards` path and require
   507 rows, finite values, legal masks, and the frozen observation/action
   shapes. Do not resave NPZ files.
2. Load all four checkpoints through `load_checkpoint_agent` on CPU. Require
   the saved hypers, state dict, parameter count, and output shapes to be
   accepted exactly. Do not use permissive state-dict loading.
3. Run one inference-only batch from the frozen corpus through each checkpoint
   twice. Require finite logits/value outputs, deterministic repeated bytes,
   and no probability mass outside the saved legal mask. This is a loader
   check, not an evaluation result.
4. Bind the compatibility receipt to the Task payload SHA-256, current loader
   source SHA-256, current world/content/matchup and observation/action ABI
   identities, and all four checkpoint identities.

Conversion, key renaming, dtype conversion, reconstructing a seed-197 model,
retraining, and substituting another checkpoint are forbidden. A failure emits
`input_incompatible` and stops.

### 3. Add prior-only checkpoint guidance inside INT-6

Only after the retained-input and current-loader gates pass, extend the existing
INT-6 arena. This is an additive challenger path, not a new authority.

#### Candidate semantics

Add `policy_prior_puct` as a fully explicit checkpoint-backed
`PlayerRegistration` variant. Its immutable identity includes checkpoint
SHA-256, byte count, parameter count, training seed, artifact ID, implementation
source SHA-256, compute class, comparison-seed derivation, branch driver, PUCT
budget, worlds, `c_puct`, maximum steps, root-noise identity, prior identity,
and leaf-evaluator identity. Only smoke-profile fixture registrations may use
the two retained INT-4 checkpoints.

Implement a `PolicyPriorRandomLeafEvaluator` in a new INT-6 arena support
module rather than changing `manabot/sim/mcts.py`:

1. load the exact checkpoint once on CPU through `load_checkpoint_agent`;
2. at the root and every expanded nonterminal node, encode the acting viewer's
   observation and softmax only the current legal logits;
3. call the existing `UniformRandomLeafEvaluator.evaluate` for the terminal
   random playout and root-perspective value;
4. replace only that evaluator's uniform child priors with the checkpoint
   policy priors; and
5. discard the checkpoint value output and never report, blend, or branch on
   it.

The wrapper records the exact input prior used at each searched root so visit
entropy and prior-to-search shift are recomputable. The ordinary uniform PUCT
candidate continues to use `UniformRandomLeafEvaluator` unchanged.

Keep `manabot/arena/players.py` and the reusable PUCT/search kernels unchanged
so every frozen code anchor's execution-source digest remains stable. Add one
arena-level player resolver that delegates all existing registrations to
`build_player` byte-for-byte and handles only `policy_prior_puct` itself. Route
the existing match, competency, matched-root profile, and candidate-preflight
paths through that resolver.

#### Preserve the base-v1 scale

The implementation source receipt and checked arena contract SHA-256 will
change because INT-6 gains a capability. The rating scale must not:

- keep the exact `ArenaKey`, rating model/prior SHA-256, five anchor
  registration payloads, anchor registration identity SHA-256 values, and
  `anchor_cohort_sha256`;
- keep the smoke and production schedules, selected matchup, viewer boundary,
  content, ABI, and compute-envelope identities unchanged;
- add the new support module to the arena implementation source bundle and
  update only the contract's implementation-source receipt;
- regression-test that all five anchor registrations serialize to their
  pre-INT-8 payloads and identities; and
- run INT-8 only with the smoke profile and an explicit non-promotion
  disposition.

Regenerate a Task-owned smoke anchor artifact through INT-6 after the source
update. Its anchor rows define the unchanged scale for the diagnostic; INT-8
does not mutate or append learned anchors to the frozen base cohort.

#### Complete diagnostic cohort

Add a smoke-only multi-candidate diagnostic command to
`run_skill_arena.py`. It accepts exactly three challengers—uniform PUCT,
chosen-policy-only-prior PUCT, and visit-policy-only-prior PUCT—and reuses the
normal INT-6 preflight, match, Command trace/replay, competency, matched-root
profile, payoff-matrix, Bradley-Terry, ledger, cap, and manifest verifiers.

The diagnostic artifact contains:

- the 10 retained anchor-anchor cells;
- all 15 candidate-versus-anchor cells;
- all 3 candidate-versus-candidate cells;
- one combined eight-player, 28-cell payoff matrix and diagnostic rating fit;
- S1-S5 rows for all three candidates on the same competency seeds; and
- one matched-root corpus shared by all prior/budget variants.

Materialize fully identified 8-, 32-, and 128-traversal registration variants
for each prior during matched-root profiling. Only the three 32-traversal
registrations enter gameplay and the rating matrix; the other six exist solely
in the profile artifact with explicit compute-class and source identities.

The command must be ineligible for promotion regardless of scores. Verification
derives expected cells as `n * (n - 1) / 2`, requires every deal/seat leg,
replays every new trace, checks the unchanged anchor registry, and recomputes
the matrix, profiles, metrics, decision, ledger, and artifact digests.

Use an explicit comparison-seed alias for the three candidates when deriving
world, rollout, competency, and matched-root seeds. Candidate IDs remain
distinct for trace and registration identity, while the alias makes the
stochastic stream identical across arms for the same anchor/root/deal. Existing
arena calls omit the alias and retain their exact seed behavior.

### 4. Preregister the bounded experiment

If and only if the retained-input and current-loader gates pass, commit the
following contract before any PUCT execution:

| Factor | Frozen value |
|---|---|
| World/content/matchup | INT-6 `w2-interactive-mirror-v1`, acting-viewer history only |
| Priors | uniform; checkpoint `9004b87e...` chosen-policy-only; checkpoint `c2c8dcec...` visit-policy-only |
| Leaf evaluation | random legal semantic Commands to terminal, identical evaluator and rollout seeds |
| PUCT | `c_puct=1.5`, `worlds=4`, `max_steps=2000`, `full_clone/current_game_v1` |
| Root noise | none |
| Matched-root budgets | 8, 32, and 128 total traversals across worlds |
| Arena gameplay budget | 32 total traversals across 4 worlds |
| Arena schedule | INT-6 smoke deal seeds 61001-61002, both seat legs; competency seeds 62001-62002 |
| Training | none; only the two retained policy-only checkpoints are used |
| Evidence class | `engineering_smoke_only_no_admission_claim` |
| Resource cap | 2 wall hours, 8 core hours, 1 GiB new artifacts, at most 4 workers |

Use common matched roots and comparison seeds across the three prior arms. Run
the three arms against every frozen INT-6 code anchor and against one another;
retain the anchor-anchor cells from the one frozen smoke anchor artifact. The
combined payoff matrix must be connected and contain every scheduled
deal/seat leg. No rating or promotion disposition may escape the engineering
smoke namespace.

The preregistered prediction is that visit supervision will better reproduce
the source teacher distribution than chosen-action supervision but that neither
one-epoch, one-seed student will clear the joint arena-and-mechanism bar over
uniform PUCT. The expected decision is
`kill_retained_smoke_policy_guidance`, meaning “do not scale either recovered
student as a PUCT prior,” not “kill PUCT” and not “chosen targets are generally
equivalent to visit targets.”

### 5. Run, verify, and decide the next corpus or kill the recovered guidance

All gates below are subordinate to zero legality, replay, private-exposure,
root-mutation, and playout-cap failures.

At the 32-traversal comparison budget, define each learned arm's paired arena
delta against uniform as the mean score difference over identical anchor,
deal, and seat cells. Define high-budget label agreement as agreement of its
32-traversal selected Command with the 128-traversal uniform reference on the
matched-root corpus. A learned signal earns the next corpus only when it:

- improves paired arena score over uniform by at least 0.05;
- exceeds the other learned signal's paired arena score by at least 0.05;
- improves high-budget label agreement over uniform-32 by at least 0.05;
- is not worse than the other signal on the aggregate S1-S5 correct count;
- stays within 1.10x of uniform p95 latency and at least 0.90x of uniform nodes
  per second; and
- has no integrity failure.

If chosen alone clears every clause, emit `next_corpus_chosen_action`. If visit
alone clears every clause, emit `next_corpus_visit_distribution`. If neither
or both clear, emit `kill_retained_smoke_policy_guidance`; the smoke evidence
is too small to justify resolving a tie by post-hoc preference.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Are the corrected retained inputs actually present? | Yes. The sibling tree is 7.3 MiB and contains exactly 13 regular files and no symlinks: dataset manifest plus two shards, root manifest, report, Study artifact, training manifest plus four checkpoints, trajectory audit, and verification. Its canonical sorted-file-table SHA-256 is `13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0`. | First implementation step copies the closed 13-file allowlist into a durable checked-in content address; no regeneration is needed or allowed. |
| Is Task-local `.runs/` durable enough for the recovered input? | No. `.gitignore` excludes `.runs/`, and the Task worktree is removed after settlement. The repository already retains binary and structured experiment evidence under `experiments/data/`. | Use `.runs/` only for staging/output. Check in the exact 13-file payload, separate exact contract bytes, and Task receipt under `experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/`, then verify from a fresh canonical checkout with no sibling read. |
| Does the bundle bind the directive's identities? | Yes. Its manifests name the required canonical contract/profile, both shard digests, all four checkpoint digests, source commit, 507 labels, and smoke evidence class. The separate checked contract file has raw-byte SHA-256 `bbbba03856b74047a8f8cec44f23a3c28b92558bff0001c602e4211c75d260a7` and canonical identity `9c3f0f600b70ca4fef7131086f6d9b350f9612e690cbb9d621e961a5de27d03c`. | The gate binds both exact copied contract bytes and parsed canonical meaning, rather than trusting a filename or conflating the two hashes. |
| Is the original replay evidence substantive? | Yes. `verification.json` records 175 decisions with zero mismatches in every listed replay/search category and no private exposure. | Preserve the receipt byte-for-byte; do not claim a current-runtime replay until the new arena emits one. |
| Can the current generic loader read this checkpoint format in principle? | The frozen files use the same `hypers` plus `model_state_dict` format consumed by `load_checkpoint_agent`; the actual compatibility test is deliberately deferred until after Task-owned freezing. | Loader verification is exact and conversion-free. A failure stops rather than adapting artifacts. |
| Can existing `agent_puct` isolate policy guidance? | No. `AgentLeafEvaluator` returns both priors and a checkpoint-derived value. Policy-only training still updates the shared trunk, so the value output is neither fixed across chosen/visit nor equal to random terminal leaves. | Reject `agent_puct` for this experiment. The evaluator must be prior-only. |
| Can INT-6 represent that prior-only challenger today? | Not yet. Checkpoint registrations are direct policies, the closed PUCT registration is uniform, and challenge execution is single-candidate. These are adapter/orchestration gaps inside the existing arena, not reasons to create a second authority. | Extend INT-6 minimally after the input gate: one prior-only evaluator, one fully bound registration variant, and one complete diagnostic command. |
| Can the old INT-4 arena runner answer instead? | It can execute `agent_puct`, but it lacks INT-6's frozen authority and has the same value/prior confound. | Do not use or wrap it for outcomes, competencies, rating, or replay claims. |
| Will editing INT-6 invalidate its base scale? | The arena implementation-source and contract digests must change, but the scale is identified by the unchanged `ArenaKey`, rating prior, five anchor registration identities, anchor cohort digest, schedules, world, and matchup. Anchor execution sources can remain byte-stable by adding a separate resolver instead of editing `players.py` or search kernels. | Update the implementation receipt, regenerate Task-owned smoke anchor evidence, and regression-test exact anchor payload/identity stability. |
| Is one seed enough to choose a method? | No. The repository's evidence discipline treats training seed as the experimental unit. | The result can choose whether this exact recovered signal deserves a next corpus; it cannot generalize to chosen versus visit supervision as methods. |
| What does prior art predict? | AlphaZero and KataGo use network policy priors to allocate MCTS exploration; KataGo also treats policy-to-search disagreement as a useful training signal and warns implicitly that low-prior blind spots matter. | Measure prior-to-visit shift, entropy, and high-budget agreement in addition to game score. Do not infer causality from Elo alone. See [AlphaZero](https://arxiv.org/abs/1712.01815), [KataGo](https://arxiv.org/abs/1902.10565), and [KataGo methods](https://github.com/lightvector/KataGo/blob/master/docs/KataGoMethods.md). |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Use existing `agent_puct` for chosen and visit | Fastest path and already loads the checkpoints. | It changes both the policy prior and leaf value, while uniform uses random terminal leaves. The central causal comparison becomes invalid. |
| Compare the direct chosen/visit policies through INT-6 | Fully supported checkpoint identity and replay. | Answers which student acts better, not which signal improves bounded PUCT. |
| Reuse or copy INT-4's `_play_cell` and add an experiment-local player | Could run the desired player without touching `manabot/arena/**`. | Creates a second match/evaluation authority and violates the sole-INT-6 directive. |
| Extend INT-6 with a prior-only registration and diagnostic cohort | Keeps one arena authority and makes every requested metric replayable under the common scale. | Chosen. Keep the extension additive, smoke-only, and identity-closed. |
| Freeze the inputs and stop after discovering the missing candidate adapter | Preserves the artifacts but leaves a local, bounded implementation gap unresolved. | Rejected. Exact byte/manifest/loader incompatibility is the only fail-closed stop. |

## Wild success

The additive INT-6 arena seam works without changing the base scale, and the
visit student consistently concentrates PUCT visits on the high-budget uniform
teacher's eventual choices while spending the same nodes. The gain appears on
the difficult competency roots, not only in aggregate wins, and the arena
replay reconstructs every Command. INT-8 then makes a clean decision: collect
the next corpus with visit distributions and prioritize search-surprising
positions, without pretending the recovered smoke checkpoint is promotable.

## Wild failure

The smoke students memorize 507 low-budget labels, sharpen the wrong moves, and
reduce exploration. A tempting pairwise win comes from changed leaf values,
different seeds, or an experiment-local match loop, so a false “visit targets
win” claim drives an expensive new corpus. The fail-closed prior-only and INT-6
boundaries exist specifically to prevent that failure mode.

## Key decisions

- Preserve all four checkpoints even though only the two policy-only arms enter
  the experiment. The directive makes the whole retained training artifact an
  identity boundary.
- Keep checkpoint value outputs completely out of search. “Policy-only
  training” does not imply a constant or comparable value prediction because
  the policy loss updates the shared trunk.
- Extend the existing INT-6 arena rather than building an experiment-local
  match loop. Arena source identity may advance; its key, rating prior, anchor
  registrations, anchor cohort, schedules, and scale may not.
- Keep `manabot/arena/players.py`, `manabot/sim/mcts.py`, `manabot/sim/flat_mc.py`,
  INT-9, and managym unchanged. A separate arena resolver provides the new
  checkpoint-guided candidate while delegating all existing player kinds.
- Use 8/32/128 only for matched-root mechanism and cost curves; use the frozen
  INT-6 32-traversal compute class for gameplay. This limits the arena matrix
  while showing whether a prior helps only when compute is scarce.
- Use the INT-6 smoke schedule and disposition. Running its production schedule
  would add game precision but not another training seed, so it could not
  upgrade the method claim.
- Treat ambiguous evidence as a kill for these recovered smoke priors. A new
  corpus must be earned by a joint mechanism, cost, competency, and arena
  result rather than by choosing the more fashionable target.
- Do not update wave memory during kickoff. No new experimental result exists;
  durable input, arena, or signal findings belong in memory only after exact
  implementation evidence lands.

## Scope

- In scope: byte-exact Task-owned input freeze; contract/profile/shard/checkpoint
  identity recomputation; exact current-loader checks; a prior-only
  checkpoint-guided PUCT registration and evaluator inside INT-6; smoke-only
  complete diagnostic cohort execution and verification; preregistration for
  uniform versus chosen-policy-only versus visit-policy-only priors; complete
  diagnostic metrics; next-corpus-or-kill decision.
- Out of scope: retraining or reconstructing any checkpoint; converting or
  rewriting corpus/checkpoint bytes; policy-value arms in the comparison;
  Teacher-0 controls; INT-5, INT-7, INT-9, or INT-12; edits to managym,
  reusable MCTS/search kernels, or a second arena authority; changes to base-v1
  anchor identities or rating scale; admission, promotion, gameplay-strength,
  or method-level claims; Study evidence.

## Wave alignment

The experiment directly advances the Intelligence measure that search
teachers and students be compared “in actual selected matchups at explicit
compute budgets, with legality, competencies, seat-balanced strength,
calibration, p50/p95 decision latency, rollout throughput, and label cost.” It
also preserves the measure that every admitted candidate use the versioned,
world-pinned arena: INT-8 extends and consumes INT-6 itself rather than creating
a diagnostic substitute.

The byte/loader gate advances the evidence-discipline measure that content,
engine, observation, action, model, opponent, and compute identities be pinned
and raw rerunnable results retained. Passing that gate requires the runnable
three-arm teacher diagnostic; input preservation alone is not completion.

## Done when

The kickoff is complete when the input gate, minimal in-place arena extension,
three-arm run, and decision rule are unambiguous.

The implementation slice is complete only when:

1. the exact 13-file allowlisted payload is checked in under
   `experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/payload`,
   the separate contract bytes and Task receipt are checked in beside it, and
   every required identity and all four current loader checks pass from a fresh
   canonical checkout with no access to the sibling INT-4 worktree;
2. INT-6 accepts exact checkpoint-bound prior-only PUCT candidates without
   consuming checkpoint value output or changing reusable search kernels;
3. the arena key, five anchor payloads and identities, anchor cohort digest,
   rating prior, schedules, and scale remain exact;
4. the documented command produces one verified 28-cell smoke artifact whose
   schedule, Commands, replays, competencies, payoff matrix, root diagnostics,
   cost metrics, and final next-corpus-or-kill decision recompute from retained
   rows; and
5. every output says `engineering_smoke_only_no_admission_claim` and exposes no
   promotion eligibility.

If and only if the exact byte, manifest, or loader gate fails, stop before arena
implementation or execution and report expected versus actual evidence. A
missing arena adapter is implementation work, not a terminal condition.

Verification runs through `uv` for Python. Run the focused input, PUCT, arena
model/player, match, competency, profile, runner, replay, and full manifest
tests, then the relevant Python suite. If any Rust file changes despite scope,
the run is invalid until debug `cargo test` passes from the repository root;
the intended implementation changes no Rust.

## Measure

### Integrity and provenance

- payload tree SHA-256 and every leaf file's SHA-256/bytes;
- contract, profile, shard, checkpoint, source-commit, ABI, content, matchup,
  and loader-source identities;
- corpus rows/games/shards and replay decisions/mismatch counts;
- legal Commands, private exposures, root mutations, truncations, cap hits, and
  exact replay results for every arena game.

### Search mechanism on identical matched roots

- raw and normalized visit entropy, with normalized entropy defined as
  `H(visits / sum(visits)) / log(legal_action_count)` and zero for one legal
  action;
- root-policy shift as L1 distance and Jensen-Shannon divergence from the input
  prior to the final visit distribution;
- selected-Command agreement among all three arms and with uniform-128;
- prior-top-action versus search-top-action agreement for each learned arm;
- p50/p95 decision latency, tree nodes, traversals, nodes/second,
  decisions/second, CPU-seconds/label, and nodes/label at 8/32/128;
- all metrics bucketed by legal-action count and action-space kind so trivial
  priority roots cannot dominate the average.

### INT-6 arena diagnostic

- the complete anchor plus three-arm payoff matrix and residuals;
- paired deal/seat scores and seat-aware Bradley-Terry diagnostic ratings;
- S1-S5 run rows and aggregate correct counts;
- native and isolated p50/p95 latency, nodes/second, decisions/second, and RSS;
- exact Command trace/replay receipts and the immutable final decision.

No confidence interval over games is promoted to a claim about chosen-action or
visit-distribution supervision. There is only one frozen training seed.
