# Measure Played Release and Training Workloads

## Problem

PR #153 proves that one fixed UR Lessons versus GW Allies match uses the
compiled semantic authority correctly across Etude live play, direct headless
execution, and canonical replay. RUL-2 separately proves that the selected
`full_clone/current_game_v1` BranchDriver can produce real PUCT teacher labels.
Neither receipt answers the product question RUL-9 owns: whether the current
world remains cheap enough when it is exercised as release software and as a
production training workload, with every required metric derived from retained
raw samples and every fallback visible.

The immediate beneficiaries are the Etude release owner, who needs an
interactive latency and memory bound, and the manabot training owner, who needs
whole-workload rather than microbenchmark throughput. This work advances these
Rules measures from `wave/rules/GOAL.md`:

- “A selected Etude Fantasia matchup reaches terminal through compiled typed
  programs and structured `InteractionOffer`/`Command` values as the production
  rules authority.”
- “The retained branch representation runs real search and interactive Study
  workloads with exact isolation and return, bounded p95 latency, competitive
  whole-rollout throughput, and measured peak RSS.”

The evidence must not become another authority, replay implementation, semantic
projection, or search driver. It measures the providers that already landed.

## The demo

Run `./scripts/verify-rul9-played-workloads`. It rebuilds the pinned release
extension, verifies the checked receipt against its contract and current source
identity, and prints one `RUL9_WORKLOADS_OK` line containing release Command
p50/p95, release steps/s and games/s, training steps/s and games/s, rollout
traversals/s, both peak-RSS values, semantic token maxima, and
`fallbacks=0 overflow=0`.

## Approach

Land one pre-registered contract and one source-bound evidence receipt with two
measurement cells.

### 1. Release cell: the PR #153 fixed tape

Consume, without changing, the checked seed-0 authority tape at
`conformance/authored-match-v1/release-stack-ur-vs-gw-seed-0.json` and the
live/headless/replay parity provider in `etude.authored_match_parity`. Run one
warm-up and ten measured repetitions of the exact 132-Command terminal tape on
each surface:

- **live:** Commands enter over `/ws/play`, are accepted by `GameSession`, and
  include the Etude protocol/presentation work a player actually waits for;
- **headless:** the same semantic Commands execute directly through managym;
- **replay:** persisted canonical Commands execute through the same managym
  authority, not through a replay-specific mutation path.

The correctness pass must retain the existing 133 revision witnesses and 132
ordered consequence groups per surface. Performance samples are additional
RUL-9 evidence and do not alter the RUL-5 receipt. Record every individual
accepted-Command duration, every measured game duration, process RSS samples,
terminal witnesses, and surface parity hashes. Report live WebSocket Command
latency as the interactive metric and retain inner semantic-apply timing as the
first attribution layer if the live budget misses.

### 2. Training cell: selected production BranchDriver teacher

Run the already-landed RUL-2 saturated production shape against the current
source, selected driver only:

- driver: `full_clone/current_game_v1`;
- four workers, one authored game per worker;
- deal seeds `1197`, `1419`, `1887`, `2197`, alternating the UR seat;
- 128 PUCT simulations, four determinized worlds, 2,000 max playout steps,
  and 500 max root decisions.

The isolated RUL-9 runner may orchestrate the public environment, structured
offer, semantic Command, and `determinized_puct` APIs to attach timers and token
census data. It must not edit or add hooks to the selected BranchDriver,
managym decision/search kernels, or manabot environment. Every root action must
remain one revision-bound structured Command and one native apply. Record raw
root Command latency, PUCT decision duration, root decision count, completed
game count and duration, traversal count, worker CPU time, process-tree RSS at
a 5 ms target interval, result hashes, and the complete branch receipt counter
set.

### 3. Semantic-program accounting

Bind `BoundSemanticPack` once to the exact ContentPack and project the acting
viewer’s already-safe observation at every measured root decision. Record:

- the active shared catalog token count;
- definition and program token distributions;
- visible object-reference counts per decision;
- expanded tokens attributable to visible physical objects, retained as a
  pressure diagnostic;
- projection failures, unadmitted definitions, and overflow count.

The selected representation encodes the catalog once and carries ragged
definition-row references for visible objects. Therefore the actual capacity
gate is the catalog plus reference frontier, not the counterfactual sum produced
by copying every program for every physical object. Preserve the expanded count
in raw evidence so future content growth cannot hide behind catalog sharing.
Any projection failure, unadmitted visible definition, clipping, truncation, or
unknown primitive is an overflow and fails the run; there is no “unsupported”
success path.

### 4. Identity and evidence

The contract lives at
`experiments/contracts/rul-9-played-workloads-v1.json`. It is committed before
the evidence run and contains the workload shapes, seeds, exact expected
counter names, budgets, predictions, and diagnostic policy. The runner and
contract must be committed before the measured run so the evidence is not tuned
after seeing its result.

The checked receipt at
`experiments/data/rul-9-played-workloads-v1.json` retains all latency and RSS
samples needed to recompute the report. It binds:

- the contract SHA-256;
- the unchanged RUL-5 authority and parity receipt SHA-256 values;
- deck, semantic source, compiled IR, ContentPack manifest, learning schema,
  and semantic-pack identities;
- a relative-path-and-file-SHA source closure for the measured providers and
  RUL-9 measurement code;
- release profile, native extension SHA-256, Python/Rust/package versions,
  host, logical CPU count, and worker/thread configuration;
- selected BranchDriver ID and complete workload coordinates.

The report at `experiments/rul-9-played-workloads-v1.md` is generated from the
checked receipt, names the strongest confound (single-host performance), and
states pass/miss independently for release and training. Raw timings remain in
the receipt; the report does not become a second source of numbers.

### 5. Fail-closed verification and diagnosis

The verifier recomputes identities, raw-sample hashes, all percentiles/rates,
all gates, and the final verdict. It rejects missing or non-finite samples,
unknown or omitted fallback counters, a positive overflow/fallback/cap count,
nonterminal games, a root step that is not exactly one native Command apply,
source/binary drift, receipt tampering, or a summary that does not rederive
byte-for-byte.

For any latency or throughput miss, retain a focused attribution alongside the
receipt:

1. subtract native semantic Command apply from live round-trip or PUCT decision
   time;
2. separate search, projection, presentation/protocol, and process scheduling;
3. correlate the slowest 5% with action family, visible object/reference count,
   event count, and worker RSS;
4. state whether the miss is engine, consumer, measurement-host, or workload
   pressure.

For token pressure, compare shared-catalog active tokens with the expanded
physical-object equivalent and attribute the tail by zone and definition. An
exploratory seed-1197 run found a 2,088-token shared catalog with a 148-token
largest definition, while naïve physical-object expansion reached 5,454 tokens
late in the game. That is evidence to keep the current catalog/reference
representation and expose the expansion diagnostic, not evidence for a new
branch or semantic representation. No representation change belongs in RUL-9;
one would require a separately pre-registered comparison on the missed product
metric.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Does the fixed release authority still run on this branch? | `./scripts/verify-authored-match-parity` rebuilt the release cp312 extension and passed all 132 Commands, 133 witnesses, 798 viewer checks, and stale-object checks. | Consume that provider unchanged and bind its receipt hashes. |
| Can the RUL-2 checked artifact simply supply the training numbers? | No. Its verifier fails closed because current relevant source SHA `65f6f…999e` differs from the frozen expected `24d301…8ec`. | Mint current RUL-9 evidence under a new contract; retain RUL-2 only as calibration and workload provenance. |
| Which landed training workload best measures the product world? | The selected BranchDriver teacher plays full UR/GW games through revision-bound Commands and already has interactive and saturated workload shapes. INT-11’s runtime-policy arena is a bounded one-Command terminal micro-matchup. | Select the saturated 4×128 BranchDriver teacher; do not rerun the INT-11 development arena. |
| Are the required measurements observable without crossing the INT-9/shared-kernel frontier? | Yes. Semantic Commands, structured offers, branch receipts, current viewer observations, `BoundSemanticPack`, process-tree RSS, and native extension location are exposed to Python callers. | Add measurement only in new RUL-9 runner/contract/evidence/report/test paths. |
| Is semantic token overflow silently hidden? | No fixed-width token buffer is on this path. The pack exposes ragged catalog offsets and projection raises on unknown or unadmitted definitions. | Count exact catalog/object tokens; define every projection/capacity exception as fatal overflow. |
| Does binary setup need to be part of the contract? | A fresh `uv run` failed before measurement because `managym._managym` was absent; the repository release verifier then built the required cp312 binary. | The verifier rebuilds through the pinned `uv run maturin` command and binds the resulting extension SHA/toolchain. |
| What are realistic budgets? | Exploratory fixed-tape headless execution measured 1.91 ms Command p95, 664 steps/s, 5.03 games/s, and 249 MiB peak RSS; live inner Command work measured 7.43 ms p95 and about 0.85–0.97 s per terminal game. Frozen RUL-2 selected training measured 4.70 decisions/s, 601 traversals/s, 1,343 ms PUCT p95, and 951 MiB peak RSS. | Pre-register useful bounds with margin, while keeping raw evidence and diagnostics for host-sensitive misses. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Re-verify and summarize the RUL-2 and INT-11 receipts | Cheapest and preserves historical evidence. | RUL-2 is source-stale and neither receipt jointly contains the fixed release surfaces, exact token census, full fallback inventory, and required complete-game metric. |
| Use the INT-11 semantic runtime-policy runner as the training cell | Directly measures learned inference and semantic model tokens. | Its paired arena terminates after one Command and is deliberately non-promotional; it does not measure production whole-game UR/GW training pressure. |
| Add profiling hooks to shared managym or manabot kernels | Could expose timing with less orchestration in the RUL-9 runner. | It crosses the explicit concurrent-work frontier and changes the thing being measured. Public read-only APIs are sufficient. |
| Run only synthetic step/clone microbenchmarks | Produces stable high-sample latency numbers quickly. | It does not satisfy the Project’s “world as played” requirement or expose complete-game, prompt mix, semantic visibility, and fallback behavior. |

## Key decisions

- Measure two real consumers, not a blended aggregate. Release and training can
  pass or miss independently.
- Gate the player-facing live WebSocket Command p95 at **100 ms** and retain a
  **10 ms** inner semantic-Command p95 attribution bound.
- Gate release headless/replay semantic steps at **500 steps/s**, live terminal
  completion at **1.0 game/s**, and release process peak RSS at **512 MiB**.
- Gate saturated training at **4.0 root steps/s**, **512 PUCT traversals/s**,
  **0.04 complete games/s**, **2,000 ms PUCT-decision p95**, **10 ms root
  Command p95**, and **2 GiB** aggregate process-tree peak RSS.
- Gate semantic capacity at **4,096 active shared-catalog tokens**, **160 tokens
  per admitted definition**, **128 visible object references per decision**,
  and exactly zero overflow/unadmitted/projection failures. Expanded
  physical-object-equivalent tokens are diagnostic, not a hidden second input
  representation.
- Require exact zero for `legacy_fixed_action`, `card_name_dispatch`,
  `candidate_cap`, `client_legality`, `indexed_fallbacks`, root/search cap hits,
  and random-playout cap hits. The verifier rejects a missing counter name just
  as it rejects a positive value.
- Keep the representation. A miss produces an attribution capsule and a
  decision-bearing follow-up proposal, never an optimization inside this PR.
- Preserve the RUL-5 receipt byte-for-byte and use no raw git/worktree/GitHub
  operations.

## Success and failure test

Wild success is not merely a green benchmark. A release owner can run one
verifier and see the exact played tape, product latency, memory, semantic
capacity, and zero fallbacks; a training owner sees the same world complete
under saturated PUCT with every number traceable to raw samples. The contract
then becomes the reusable measurement shape for the next creator-selected
increment without becoming a new authority.

Wild failure would be a green summary that cannot be rederived, a synthetic
runner that no longer matches production, a benchmark-only representation
fork, or an aggregate that lets fast headless steps hide slow player Commands.
Source/binary binding, per-cell verdicts, complete raw samples, and strict scope
prevent those failure modes.

## Scope

- In scope: new RUL-9 contract, isolated runner, build/verify wrapper, checked
  raw receipt, generated report, and focused verifier/unit tests.
- In scope: read-only use of the PR #153 authority/parity providers,
  `full_clone/current_game_v1`, PUCT, structured offers/Commands,
  `BoundSemanticPack`, and process inspection.
- In scope: a focused diagnosis for every observed pre-registered miss and an
  explicit retain/reconsider decision for the selected representation.
- Out of scope: any rules, Command, offer, search, branch, replay, semantic
  projection, or binding-kernel change; training the learned runtime policy;
  content expansion; a second branch representation; altering RUL-5 evidence.
- Out of scope: all INT-9 frontier paths named in directive v1, including
  `experiments/contracts/int-9*`, `run_exact_range_player.py`,
  `manabot/belief/**`, `manabot/env/env.py`, `manabot/sim/flat_mc.py`,
  `managym/possible_worlds.py`, `managym/src/possible_worlds.rs`, and shared
  decision/structured-offer/search/binding kernels.

## Done when

- The pre-run contract is committed before measurement and its SHA is bound in
  the checked receipt.
- `uv run pytest tests/experiments/test_rul9_played_workloads.py` passes focused
  derivation, tamper, missing-counter, overflow, identity-drift, and budget-miss
  tests.
- `./scripts/verify-rul9-played-workloads` rebuilds the release extension,
  verifies current source/binary/workload identity, rederives every summary,
  confirms the RUL-5 receipt was not changed, and exits zero only when both
  selected-runtime cells meet their gates with zero fallback and overflow.
- The release cell retains ten measured repetitions of all three fixed
  surfaces with exact terminal/parity identities and reports Command p50/p95,
  steps/s, games/s, peak RSS, token counts, and all authority fallbacks.
- The training cell completes all four saturated games and reports root Command
  p50/p95, PUCT p50/p95, root steps/s, traversals/s, complete games/s, peak RSS,
  token counts, and the complete branch fallback/cap inventory.
- `experiments/rul-9-played-workloads-v1.md` traces every number to the checked
  receipt, names the strongest confound, diagnoses each observed miss, and
  records “retain `full_clone/current_game_v1`” unless the pre-registered
  workload supplies contrary decision-bearing evidence.

## Measure

Pre-run predictions:

- Release live Command p95 remains below 100 ms; headless/replay remain above
  500 semantic steps/s; terminal live play remains above 1 game/s; peak RSS
  remains below 512 MiB.
- Saturated selected training remains above 4 root steps/s, 512 traversals/s,
  and 0.04 games/s, with PUCT p95 below 2 seconds and peak RSS below 2 GiB.
- The compiled semantic catalog remains below 4,096 active tokens, every
  admitted definition remains below 160 tokens, visible references remain
  below 128, and overflow/fallback counters remain exactly zero.
- The expanded physical-object-equivalent token count may exceed 4,096 late in
  long training games. If it does, the expected cause is repeated visible
  references to an already-shared catalog, not runtime clipping; verify by zone
  and definition attribution before considering any representation change.

The checked run command is:

```bash
uv run experiments/runners/run_rul9_played_workloads.py \
  --contract experiments/contracts/rul-9-played-workloads-v1.json \
  --out experiments/data/rul-9-played-workloads-v1.json
```

The verifier must recompute from raw evidence; no report-only number is
accepted as a gate input.
