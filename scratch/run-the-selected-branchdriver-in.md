# Run the Selected BranchDriver in the Visit Teacher

## Problem

RUL-1 made a binding runtime decision: retain
`full_clone/current_game_v1` as the production BranchDriver and keep
clone-plus-undo and event-page-COW-plus-undo out of production hot paths. The
current Intelligence visit teacher does not express that decision. Teacher-1
calls `clone_env()` directly from Python for world roots, tree children, and
random leaf playouts, then mutates those branches through indexed `Env.step`,
`Game::step`, and `Game::random_playout` paths. Its frozen evidence also still
uses the legacy `INTERACTIVE_DECK` mirror. That makes it impossible to prove
which driver made a label, count its branch lifecycle, know which Rules Command
was applied, or know whether the selected driver works on the compiled UR
Lessons versus GW Allies world.

This work puts the exact retained driver under the existing determinized PUCT
teacher without changing the search algorithm. It gives Intelligence a named,
measured production branch path; gives Rules exact root, Command, replay, and
viewer-boundary evidence; and gives future runtime decisions a consumer
regression contract instead of another isolated clone benchmark.

## The demo

Run:

```bash
uv run experiments/runners/run_selected_branchdriver_teacher.py \
  --contract experiments/contracts/rul-2-selected-branchdriver-teacher-v1.json \
  --out .runs/rul-2-selected-branchdriver-teacher-v1.json
uv run experiments/runners/run_selected_branchdriver_teacher.py \
  --contract experiments/contracts/rul-2-selected-branchdriver-teacher-v1.json \
  --out .runs/rul-2-selected-branchdriver-teacher-v1.json \
  --verify
```

The command prints `full_clone/current_game_v1`, zero reference/production
mismatches, the exact authored pack digest, interactive and saturated
labels/s, rollouts/s, p50/p95 decision latency, peak RSS, branch/rollback
counters, label cost, and a final `remain` verdict. If anything diverges, it
prints the replay command and path of a self-contained failure capsule instead
of publishing performance claims.

## Approach

### 1. Make the retained driver the only production branch path

Add a small search-branch boundary around the existing Rust `BranchDriver`
contract. The selected boundary is compile-time bound to `FullCloneDriver` and
reports the stable ID `full_clone/current_game_v1`. It exposes to the Python
teacher the operations the teacher actually uses:

- `fork_exact` for determinized worlds, retained tree children, and leaf
  playouts;
- determinization and rollout reseeding;
- structured-offer projection and prompt/revision-bound Command application;
- representation-neutral authority, legal, fixed-viewer, event-boundary, and
  RNG-continuation witness hashes;
- native lifecycle counters for forks, applies, marks, rollbacks, and random
  playouts, plus the existing journal/COW unsupported fields.

The normal `determinized_puct` and `DeterminizedPuctPlayer` path receives an
explicit branch backend and defaults to the selected backend. Every world,
child, and `UniformRandomLeafEvaluator` fork must use that backend; direct
`clone_env()` is not allowed inside production PUCT. The old direct full-clone
implementation remains available only as the named
`legacy_env_clone/reference_v1` backend used by differential tests and the
RUL-2 evidence runner. There is no runtime registry for the two rejected
drivers and no “try selected, fall back to clone” behavior.

#### Structured Command mutation boundary

The selected production backend has exactly one gameplay mutation entry point:

```text
apply_policy_choice(site, policy_index) -> SelectedApplyReceipt
```

`site` is one of `world`, `child`, or `leaf`. `world` names the first tree edge
applied from a determinized world root, `child` names every later retained-tree
edge, and `leaf` names every randomly sampled tail ply. At every call, the
backend must perform this sequence atomically:

1. Read the branch's current `StructuredOfferSet`, current prompt ID and
   revision, and pre-apply authority and legal-surface witness hashes.
2. Build the learner-facing policy lookup from that exact structured surface.
   Each lookup row retains the resolved `offer_id` and complete structured
   answers. The learner-selected integer is only a key into this read-only
   lookup; it is never passed to Rules as a mutation coordinate.
3. Resolve the row, then construct the typed
   `managym::experience::Command` with a fresh command ID, branch-local match
   ID, `expected_revision`, `prompt_id`, resolved `offer_id`, and structured
   answers. This typed value—not a parallel JSON description—is the authority
   for the mutation and the operation tape.
4. Revalidate the policy key, offer ID, prompt, revision, source authority
   hash, and legal-surface hash against the still-current branch. Any unknown,
   stale, or mismatched value fails before mutation and leaves the witness and
   native apply counter unchanged.
5. Validate the typed Command, lower the `OfferSubmission` from that exact
   value against the same private `StructuredOfferSet`, and execute it only through
   `Env::step_structured` / `Game::apply_offer_submission`, the normal atomic
   Rules structured-command path.
6. Return a native apply receipt containing the site, accepted Command ID,
   resulting terminal state, native apply sequence/counter, and post-apply
   witness.

The selected backend must never call `Game::step(index)`, `Env.step(index)`,
`BranchDriver::apply(BenchCommand)`, `Env::step_legacy_submission`, or
`Game::random_playout`; none of those indexed or compatibility paths may be a
fallback. Random leaf evaluation therefore uses a deterministic structured
playout loop: at every ply it reads the new structured offer surface, samples a
policy lookup key from the existing seeded policy RNG, constructs a new bound
Command, and applies it through the same method. Determinization and rollout
reseeding remain explicit branch setup operations, but every gameplay mutation
after them crosses this Command boundary.

The fixed authored workload must have complete structured policy-lookup
coverage. The current `StructuredOfferSet` implementation covers only a subset
of priority and attacker decisions, so RUL-2 includes the bounded offer/answer
extensions for every decision kind actually reached by the fixed UR/GW
correctness and measurement seeds. Encountering an unsupported decision,
missing lookup row, duplicate mapping, or incomplete answer aborts the run; it
never re-enters the positional ABI.

Teacher specs and label provenance record the driver ID. Unknown or omitted
driver IDs in a frozen experiment contract fail before play. Existing frozen
INT-4 artifacts are not rewritten; the default behavior change is covered by
new RUL-2 evidence and focused compatibility tests.

The PUCT tree retains independent child states, so the selected full-clone
path does not use marks or rollback during this workload. A measured zero for
marks and rollbacks is expected and distinct from an unsupported counter.
Fork and apply counts must be positive and reconcile with the recorded
operation tape.

### 2. Exercise the authored selected matchup, not the old mirror

The contract binds the exact checked-in semantic pack and the exported
`UR_LESSONS_DECK` / `GW_ALLIES_DECK` lists. Runtime admission must report
`pack_key=ur-lessons-vs-gw-allies` and matching IR/source/content digests. The
checked-in lists currently contain 41 UR cards and 40 GW cards; the contract
records those actual counts and fails on drift rather than repeating the
historical “two 40s” shorthand.

Both seats use the unchanged
`determinized_puct_uniform_prior_random_leaf/v1` teacher so every surfaced
decision is a real visit label. The fixed workload has two cells:

| Cell | Workers | Search budget | Worlds | Games and seats | Purpose |
|---|---:|---:|---:|---|---|
| `interactive-single-8-v1` | 1 | 8 traversals/decision | 4 | two fixed games, UR once on play and once on draw | Interactive latency and single-worker label cost |
| `saturated-4x128-v1` | 4 spawned workers | 128 traversals/decision | 4 | four fixed games, alternating UR seat | Current high-budget teacher throughput and aggregate RSS |

Use fixed game seeds `1197`, `1419`, `1887`, and `2197`, with player/search
seeds derived through the current `_mix_seed` rules and written to the raw
receipt. Each worker performs one untimed authored-root warmup, resets to its
contract game, then waits at a barrier. Process import, pack compilation,
warmup, and JSON serialization are outside measurement; the complete games,
including all search decisions and authoritative root Commands, are inside.
The saturated cell requires four physical cores and is marked noncanonical if
oversubscribed.

At every decision, require native `action_count == count(actions_valid) ==
len(frame.offers)`. The authored probe reached 12 legal actions at most and had
zero mismatches across 209 decisions, so no learner ABI migration is justified
inside RUL-2. Any fixed-workload overflow fails closed and becomes a separate
structured-label task.

### 3. Separate exactness evidence from timed measurement

Before timing, run the complete fixed game/seed set through the legacy
reference and selected production backends. Compare these fields at every
teacher decision:

- root authority hash, legal-surface hash and count, both fixed-viewer hashes,
  acting-viewer hash, event cursors, terminal flag, and RNG continuation;
- ordered legal frame offers and the prompt-bound Etude `Command` selected by
  visit count with the existing Q-value tie-break;
- per-world and aggregate visit counts, Q values, root values, cap hits, tree
  node count, and maximum depth;
- every branch Command chosen during tree traversal, including its source
  authority/legal preconditions, plus every internally sampled Command,
  outcome, and terminal witness from each seeded random leaf playout;
- unchanged source-root witness after search, the authoritative Command tape
  used to advance the real match, and the terminal winner.

The selected audit is partitioned into independent `world`, `child`, and
`leaf` operation tapes. Every accepted mutation record contains:

| Field | Required evidence |
|---|---|
| Policy lookup | Site and learner policy index |
| Structured identity | Resolved offer ID and complete serialized Command |
| Source binding | Prompt ID, expected revision, authority hash, and legal-surface/precondition hash |
| Native execution | Structured apply receipt and monotonically increasing native apply counter |
| Result | Post-apply authority/legal/viewer witness, event boundaries, RNG continuation, and terminal flag |

For each site independently, the native accepted-apply delta must equal the
number of recorded selected mutations in that site's tape. The total native
apply delta must equal the sum of the three tapes. Command IDs, apply sequence
numbers, and pre/post witness chaining must be contiguous. A positive native
apply without a tape record, a tape record without one native receipt, or any
indexed/fallback mutation is an exactness failure. Timed mode may omit the
expensive serialized witnesses, but it retains per-site native and lightweight
operation counts and must reconcile them before publishing performance.

Audit mode may collect the full operation tape and witnesses because its time
is excluded from performance claims. Timed mode uses the same selected backend
with audit hashing disabled; it still records driver identity and native
counters. This prevents proof instrumentation from becoming the measured
cost.

On the first mismatch, stop the cell and write a failure capsule containing
the content/driver digests, deal seed, prior root Command tape, revision,
viewer, search seed and budget, world/simulation index, first diverging branch
Command, and both witness components. The runner accepts `--replay-failure
<capsule>` and must reproduce the same first divergence from a fresh process.
No performance row is valid when exactness or replay fails.

### 4. Measure the consumer, then enforce its budget

For each reference and selected cell, record:

- total completed search decisions and decisions per barrier wall second;
- total PUCT traversals (the teacher's rollout unit) and traversals per barrier
  wall second;
- p50 and p95 of the existing per-decision timer, which includes all world and
  tree forks, determinization, rules steps, random leaf playouts, and result
  aggregation;
- summed worker CPU milliseconds per label, inner search milliseconds per
  label, and wall milliseconds per label;
- aggregate worker RSS baseline, peak, and peak delta sampled every 5 ms;
- branch, apply, mark, rollback, and random-playout counts, plus cap rate.

Use `multiprocessing.get_context("spawn")` explicitly so workers do not inherit
an initialized Rust/Python runtime. Python requires an importable `__main__`
for `ProcessPoolExecutor`, which is why this belongs in a checked-in runner,
not an inline command. RSS uses
`psutil.Process(pid).memory_info().rss`, which is byte-valued on supported
platforms; summed process RSS can double-count shared pages, so the receipt
states that limitation and compares only matched fresh-process cells. See the
[Python 3.12 process-pool documentation](https://docs.python.org/3.12/library/concurrent.futures.html)
and [psutil memory documentation](https://psutil.readthedocs.io/stable/index.html).

The checked-in contract predeclares these regression budgets. The absolute
numbers are conservative relative to a noncanonical authored-game sizing probe
(8 traversals: 20.65 labels/s, 165.19 traversals/s, 90.57 ms p95; 128-traversal
initial roots: 1.10 s p95) and must be evaluated in the isolated canonical run:

| Gate | Interactive single | Saturated 4×128 |
|---|---:|---:|
| Decisions/s | at least 15 | at least 4 aggregate |
| Traversals/s | at least 120 | at least 512 aggregate |
| p95 decision latency | at most 125 ms | at most 2,000 ms |
| Peak aggregate RSS | at most 512 MiB | at most 2 GiB |
| Cap rate | 0 | 0 |

In both cells, the selected path must also retain at least 90% of the matched
reference decisions/s and traversals/s, remain within 1.15× reference p95, and
remain within 1.10× reference peak RSS. Exactness requires zero mismatches,
zero source-root mutations, zero viewer-private exposures, zero replay
failures, and zero unmeasured fallbacks.

The report has one decision:

- `remain` only if both exactness and both consumer cells pass;
- `remove` if exactness fails, fallback occurs, or the selected integration
  misses its matched-reference guardrails;
- `split` is not justified by this Task because the selected and reference
  paths are the same full-clone representation. A split can be reported only
  after a different driver is run through this exact consumer contract and
  wins one workload without failing the other. Existing benchmark artifacts
  cannot supply that evidence.

If a surprising result appears, use the failure capsule or one matched
cause-separating cell. Do not regenerate the RUL-1 branching matrix unless that
diagnostic specifically requires it.

### 5. Publish durable consumer evidence

Add a new immutable experiment contract, raw verified receipt, and concise
report for RUL-2. The report links the RUL-1 decision rather than copying its
benchmark tables and states the final `remain`, `split`, or `remove` decision.
The verifier recomputes content/source identities, logical-work checksums,
metrics, ratios, counter reconciliation, RSS summaries, and verdict from the
raw receipt. Unit tests use synthetic receipts and a bounded smoke profile;
the canonical workload remains a deliberate evidence command, not a fake
microbenchmark in CI.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Which driver did RUL-1 actually select? | `docs/benchmarks/search-branching-decision-v1.md` explicitly retains `full_clone/current_game_v1` and rejects both optimized representations from production hot paths. | Production is statically bound to `FullCloneDriver`; rejected drivers are not selectable or fallback candidates. |
| Does the real selected matchup use compiled semantics? | Exact UR/GW decklists are recognized by `content_pack_for_authored_match`; a live probe admitted `ur-lessons-vs-gw-allies` with the checked-in IR/source hashes. | The evidence contract binds the authored pack and exact decklists instead of the legacy mirror. |
| Can the current PUCT run the authored world without mutating its source? | An 8-traversal/four-world probe built a real tree with no caps and left the root digest unchanged. A full 4-traversal self-play reached terminal in 209 decisions. | Keep the PUCT algorithm fixed and change only its branch backend and workload. |
| Will the 32-slot learner ABI truncate the fixed workload? | The probed authored game reached 12 legal actions maximum and raw/encoded counts matched at all 209 roots. | Keep the current ABI but assert equality at every measured root and fail closed on drift. |
| Is the current teacher already production-bound to a BranchDriver? | No. `mcts.py` calls `clone_env()` directly in world, child, and leaf paths; `Env::fork` directly clones `Game`. | Route all three fork sites through one explicit backend and test that no direct clone remains in production PUCT. |
| Can the current structured offer surface drive every selected mutation? | Not yet. `Game::structured_offers` currently covers only priority and attacker decisions, and priority coverage is intentionally partial; the existing random playout mutates through indexed `Game::step`. | Extend structured offers/answers only for decision kinds reached by the frozen UR/GW workload, replace random leaf playout with the structured loop, and fail closed on every uncovered decision. |
| Can current evidence prove viewer safety and deterministic equality? | `SearchStateWitness` already hashes full authority, legal surface, fixed viewers, event boundaries, and RNG continuation; Teacher-1 already records frame/Command replay seeds. Neither is exposed as one production branch receipt today. | Expose compact witness receipts and join them to the existing teacher replay artifact instead of inventing another state hash. |
| Are branch and rollback counters available? | RUL-1 drivers expose journal/COW counters, while eager fork/checkpoint counts are currently maintained by the benchmark harness. The teacher has no shared branch counter. | Add consumer-native lifecycle counters at the selected boundary; record real zero marks/rollbacks for retained full-clone trees. |
| How should saturated workers and RSS be measured? | The repository already uses spawned fresh workers and 5 ms `psutil` RSS sampling; official Python docs require an importable main for process pools and psutil documents RSS in bytes. | Use a checked-in spawned runner, barrier, warmup, complete RSS series, and matched-cell comparisons. |
| Does the work need new benchmark evidence? | No. RUL-1 already made the representation decision; RUL-2 needs real teacher evidence. | Preserve the benchmark matrix unchanged and add only consumer integration evidence. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Rename or annotate existing `clone_env()` as the selected driver | Nearly no code change | This is provenance theater: it does not call the RUL-1 boundary, cannot count lifecycle operations, cannot expose its witness, and leaves hidden clone sites in leaf evaluation. |
| Register all three BranchDrivers behind a runtime string | Makes future A/B experiments easy | It places two explicitly rejected drivers in the production hot path and invites silent fallback. A future candidate must first implement this consumer contract in an evidence-only arm. |
| Move determinized PUCT wholesale into Rust | Could reduce Python overhead and simplify native counters | It changes the Intelligence algorithm and observation/evaluator seam at the same time as the representation integration, invalidating comparison with the current visit teacher. |
| Replace retained tree children with mark/rollback | May reduce copies in a sequential search | PUCT retains siblings and revisits nodes; serial rollback changes the live-tree workload and repeats the failed clone-plus-undo hypothesis without consumer evidence. |
| Keep the old mirror because the RUL-1 benchmark used it | Gives direct continuity with benchmark numbers | It would prove a benchmark fixture again, not the creator-selected compiled world required by the Wave and Task. |

## Key decisions

- The product backend is a single explicit selected path, not a generic driver
  registry. Future drivers earn registration by passing this same consumer
  contract.
- Policy indices are lookup keys, never Rules commands. Every selected world,
  child, and leaf mutation resolves the current structured offer, constructs a
  typed prompt/revision-bound `managym::experience::Command`, lowers and applies
  that exact value through the normal atomic structured executor, and serializes
  the same value into its site tape; indexed, legacy, and random-playout mutation
  APIs are forbidden fallbacks.
- The current Python PUCT remains readable and unchanged in search semantics;
  Rules owns exact branching while Intelligence continues to own tree policy.
- Correctness tracing and performance timing are separate modes over the same
  backend. Witness generation may not contaminate latency or throughput, but
  per-site native apply counts always reconcile with selected mutations.
- The authored pack manifest, not copied prose, defines the matchup. The 41/40
  counts are frozen in the contract until the pack itself changes.
- Full-clone PUCT is a retained-branch workload. Zero rollback is the correct
  observation, not a reason to manufacture transactional work.
- A successful outcome makes every visit label attributable to an exact driver
  and replayable from a compact seed/Command capsule. Wild success is that a
  future runtime change becomes a one-contract consumer comparison rather than
  another representation debate.
- The failure mode to avoid is a cosmetic adapter that leaves one direct clone
  or unmeasured fallback in the leaf path. Tests reconcile every branch
  operation and fail on unknown driver IDs so that path cannot ship silently.

## Scope

- In scope: the exact retained `FullCloneDriver`; the current uniform-prior,
  random-leaf determinized PUCT teacher; UR Lessons versus GW Allies through
  the compiled pack; reference/production differential replay; root and branch
  Commands; fixed-viewer witnesses; driver counters; isolated interactive and
  saturated performance/RSS evidence; a verified report and decision.
- In scope: focused Rust and Python API/tests needed to expose the selected
  boundary, structured policy lookup and Command application, fixed-workload
  structured-offer coverage, witness receipts, deterministic failure capsules,
  and metrics.
- Out of scope: integrating clone-plus-undo or page-COW into production;
  changing PUCT selection, priors, values, or training targets; neural
  inference; Study fork/return (RUL-4); a learner action-ABI migration; new card
  semantics; general card/deck support; Team Sealed orchestration; regenerating
  RUL-1 benchmark artifacts without a demonstrated cause-separating need.

## Done when

- Normal `determinized_puct` specs report and use
  `full_clone/current_game_v1`; tests prove world, child, and leaf forks cannot
  bypass the backend and unknown IDs cannot fall back.
- Every selected world, child, and leaf gameplay mutation reads the current
  structured offers, treats the learner integer only as a lookup key, resolves
  the offer ID and answers, constructs and revalidates a typed
  `managym::experience::Command`, lowers the submission from that exact value,
  applies it through `Env::step_structured` / `Game::apply_offer_submission`,
  and serializes that same typed Command into the site tape.
  Focused tests poison `Game::step(index)`, `Env.step(index)`,
  `BranchDriver::apply(BenchCommand)`, `step_legacy_submission`, and
  `random_playout` so any selected-backend use fails immediately while a full
  structured PUCT decision still completes.
- Focused negative tests cover an out-of-range policy key, unknown/changed
  offer ID, stale prompt, stale revision, authority-precondition mismatch, and
  legal-surface mismatch. Each must fail closed with an unchanged witness and
  unchanged accepted-apply counter; there is no positional retry.
- Audit tests exercise at least one accepted `world`, `child`, and `leaf`
  mutation and reconcile each tape's policy index, offer ID, serialized
  Command, precondition hashes, native receipt/counter, and post-apply witness.
  Native accepted applies equal recorded mutations per site and in total, with
  zero unmeasured fallback.
- The complete fixed reference and selected workloads have identical legal
  roots, root and sampled branch Commands, visits/Q/root values, outcomes,
  authority/legal/viewer witnesses, event boundaries, RNG continuations, and
  replay receipts, with zero source/sibling mutation or private exposure.
- A deliberately corrupted failure capsule reproduces the same first mismatch
  in a fresh process, and the canonical run produces no failure capsules.
- Both consumer cells report decisions/s, traversals/s, p50/p95, peak RSS and
  delta, lifecycle counters, cap rate, and label cost; the verifier recomputes
  every summary and budget result from the raw receipt.
- The report makes the decision. Because both current arms are full clone,
  `split` must be recorded as unjustified unless another representation is
  actually measured through this consumer path.
- The work advances the Wave measures that “Intelligence search ... consume[s]
  projections of the same authoritative match” and that “the retained branch
  representation runs real search ... with exact isolation ... bounded p95
  latency, competitive whole-rollout throughput, and measured peak RSS.”
- Validation passes from the repository root:

  ```bash
  cargo fmt --check --manifest-path managym/Cargo.toml
  cargo clippy --manifest-path managym/Cargo.toml --all-targets --all-features -- -D warnings
  cargo test --manifest-path managym/Cargo.toml
  uv run maturin build --release -i .venv/bin/python -m managym/Cargo.toml -o managym/target/wheels
  uv run pytest tests/sim/test_mcts.py tests/sim/test_teacher1_evidence.py tests/sim/test_selected_branchdriver_teacher.py
  uv run experiments/runners/run_selected_branchdriver_teacher.py --contract experiments/contracts/rul-2-selected-branchdriver-teacher-v1.json --out .runs/rul-2-selected-branchdriver-teacher-v1.json
  uv run experiments/runners/run_selected_branchdriver_teacher.py --contract experiments/contracts/rul-2-selected-branchdriver-teacher-v1.json --out .runs/rul-2-selected-branchdriver-teacher-v1.json --verify
  ```

  Place the CPython 3.12 `_managym` shared object from the built wheel at
  `managym/_managym.cpython-312-darwin.so` before Python validation whenever
  Rust under `managym/src` changes. After all verification and review gates,
  land and complete the Task with `lf pr land -c`.

## Measure

The noncanonical kickoff probes establish only feasibility and sizing:

- authored admission: exact compiled pack, three initial legal actions, root
  unchanged after search;
- one 4-traversal self-play: terminal after 209 decisions, maximum 12 legal
  actions, zero raw/encoded mismatches;
- one 8-traversal authored game: 252 labels, 20.65 labels/s, 165.19
  traversals/s, 49.26 ms p50, 90.57 ms p95;
- five 128-traversal authored initial-root searches: 758.05 ms p50, 1,100.91
  ms p95, 166.22 traversals/s.

The implementation's canonical fresh-process receipt supersedes those sizing
numbers. “Better” means zero exactness failures and the selected driver clearing
both the absolute budgets and matched-reference ratios above. A performance
win is welcome but not required: RUL-1 selected full clone because it was the
simplest correct representation, so parity with low operational complexity is
a successful consumer result.
