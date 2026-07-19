# Bring Release-Stack Live Play Within Product Budgets

## Problem

The sole remaining Playable Curated World KR is workload admission. RUL-9's
immutable measurement origin proved the fixed seed-0 UR Lessons versus GW
Allies semantic workload across live WebSocket play, direct headless execution,
persisted replay, and the selected `full_clone/current_game_v1` training load,
but it honestly missed the two player-facing release gates:

- live WebSocket Command p95 was 150.492 ms against a 100 ms maximum;
- live complete-game throughput was 0.347 games/s against a 1.0 games/s
  minimum.

The semantic engine was not the miss: inner Command p95 was 4.291 ms, direct
headless and replay execution exceeded 500 steps/s, and every training,
capacity, memory, exactness, and fallback gate passed. RUL-12 must remove the
outer release-stack waste without changing the semantic Command authority,
content, replay meaning, public-commitment ontology, branch representation, or
budgets.

This work benefits players first: each accepted action receives a current
Etude frame promptly rather than paying for reconstruction of the entire match
history. It also closes the provider project's last KR with current-source
evidence rather than relabeling or rerunning RUL-9.

The following inputs remain historical evidence and must stay byte-identical:

- PR #159 merge `d895ac9f2617be11c47353a3cd9ab98601d08b9e`;
- `experiments/data/rul-9-played-workloads-v1.measurement.json`, file SHA-256
  `9a3933a570772e8d3e04b59526faaf1d51b5fc0e26ba8c02e08eae36599bc951`,
  artifact identity
  `498df1eda031f1d6ea68f72f792dfd68195bb10eab884b006aa7772b586564da`;
- the RUL-9 metadata-only derivation receipt, fixed Command tape, contract, and
  recorded hashes.

## The demo

Run `./scripts/verify-rul12-release-stack-budget`. It rebuilds the pinned
CPython 3.12 release extension, verifies a new current-source/current-binary
receipt, and prints a passing line with live Command p50/p95 at or below the
100 ms p95 gate, at least 1.0 fixed complete games/s, exact live/headless/replay
identity, retained training budgets, zero semantic overflow, and zero fallback
counters.

## End-to-end proof

The concrete proof scenario is the same fixed seed-0, 132-Command UR Lessons
versus GW Allies authority tape used by RUL-9. The RUL-12 runner sends all 55
hero Commands through the production `/ws/play` TestClient WebSocket boundary
while the fixed villain policy completes the other authority Commands, receives
each accepted acknowledgment, persists the terminal canonical replay, then
drives the exact persisted tape through direct headless and replay execution.
It also reruns the unchanged four-worker x 128-simulation
`full_clone/current_game_v1` training workload.

`./scripts/verify-rul12-release-stack-budget` proves the user-visible outcome
only when the new receipt rederives live Command p95 <= 100 ms and >= 1.0
complete games/s while every witness, ordered consequence, viewer projection,
stale-reference rejection, public commitment, semantic token/capacity value,
fallback counter, RSS limit, and training gate remains exact. The performance
change is therefore observed through the same player acknowledgment boundary
whose delay RUL-9 measured, not through an internal microbenchmark.

## Source of truth and derived views

- managym's fixed semantic Command authority and state witnesses are the match
  truth. Etude does not create or reinterpret legality, Commands, public
  commitments, semantic events, or terminal state.
- `GameSession.canonical_decisions` contains the authoritative committed
  `ReplayDecision` rows during live play; terminal trace persistence writes
  those same rows into `CanonicalReplayV1`. Each table decision summary is a
  derived viewer-safe projection of one row, never a second replay ledger.
- A row address derives from exactly `replay.<match_id>`, `match_id`, and that
  row's exact `ReplayDecision` SHA. Row fields come from the same row; list
  position, iteration position, and reconstructed replay state have no
  authority.
- The additive RUL-12 measurement artifact is the authoritative current-run
  performance receipt. Its Markdown report and success line are derived views
  that must rederive from retained raw samples. The RUL-9 measurement origin is
  an immutable historical baseline input, never a current-run receipt.
- The checked RUL-11 receipt remains its historical public-commitment proof.
  Verifier-comparison-only source normalization cannot alter its checked bytes,
  generation/write behavior, or any non-source consequence; the RUL-12 source
  closure alone binds current `server.py` and `replay_index.py`.

## Affected surfaces and consumers

- `etude/replay_index.py`: owns the reusable exact row-address constructor and
  preserves all existing address parsing and full-replay call sites.
- `etude/server.py`: `_live_decision_summaries` becomes a direct canonical-row
  projection. `/ws/play`, `TableSnapshot`, new game, rematch, reconnect,
  watcher/pilot responses, and accepted Command acknowledgments retain their
  existing DTO and ordering.
- Replay and Study: persisted `CanonicalReplayV1`, replay projections,
  decision restoration, Study addresses, presentation tracks, and terminal
  synchronous persistence remain compatible and semantically unchanged.
- RUL-11 verification: only comparison of its already checked historical
  source manifest changes; `build_receipt`, `write_receipt`, checked fixture
  bytes, and every semantic/public-commitment field remain unchanged.
- RUL-12 evidence: the new contract, runner, measurement receipt, report, and
  verifier measure and bind the current release and training paths.
- managym, semantic content/IR, public-commitment vocabulary, frontend wire
  schema, training/search algorithms, and `full_clone/current_game_v1` are
  compatibility consumers that must not change.

## Absent and error states

- A match with no committed hero decision returns `decisions: []`; it does not
  reconstruct a replay, invent a row, or emit a placeholder address.
- Villain/non-authorized rows remain absent from the hero table summary. A
  missing or malformed row identity, digest, or address fails validation; it
  never falls back to list position, raw action index, or client legality.
- If `canonical_replay`, `project_replay`, or `projection_with_addresses` is
  unavailable or poisoned, table summary construction still succeeds from
  canonical rows. Any accidental dependency is a test failure.
- A terminal trace persistence failure remains synchronous and prevents a
  successful terminal proof; RUL-12 cannot acknowledge or measure a game as
  complete without its persisted canonical replay.
- Any changed frozen RUL-9 byte or artifact identity aborts verification before
  current evidence is admitted. RUL-9 is never regenerated by an RUL-12 path.
- Missing samples, source/binary drift during measurement, changed counter
  inventory, nonzero fallback/overflow/privacy/replay mismatch, stale-reference
  mutation, RUL-11 non-source drift, or training/release budget miss fails
  admission closed.
- Missing default `rul-12-release-stack-budget-v1.json` or Markdown report is
  an incomplete Task, not an implied pass. Explicitly named contention receipts
  remain diagnostic evidence and cannot satisfy the default verifier.
- A completed controlled run that misses a performance budget still writes its
  honest independently named RUL-12 receipt and report, exits 2, and leaves the
  KR open. No threshold, sample, synchronous terminal cost, or failed game is
  removed to manufacture a pass.

## Approach

### 1. Project table decision summaries directly from canonical rows

Keep replay-address meaning in `etude/replay_index.py`. Add a constructor whose
deterministic row-address inputs are exactly `replay.<match_id>`, `match_id`,
and the exact `ReplayDecision` SHA returned by `row.sha256()`; make the existing
full-replay constructor delegate to it. Every serialized decision field comes
from that same immutable row. Incidental list position, iteration index, and
reconstructed replay state are not address inputs.

Rewrite `etude.server._live_decision_summaries` to:

1. scan `record.game.canonical_decisions` in canonical order;
2. retain only the existing authorized hero-viewer rows;
3. derive each address through the replay-index constructor;
4. emit the same five summary fields and the same serialized address.

Do not call `GameSession.canonical_replay`, `project_replay`,
`projection_with_addresses`, or deep-copy full frames and presentation tracks
for a table summary. The WebSocket schema and payload shape do not change: a
response still carries the full list of small `TableDecisionSummary` values.
Only the construction path changes.

Use the direct projection without an incremental cache. At this 132-Command
world size it already clears both gates with substantial margin, while avoiding
cache invalidation across new game, rematch, reconnect, participant transfer,
and terminal trace finalization. Canonical decisions remain the only stored
truth.

### 2. Preserve historical provider receipts while re-proving current behavior

The RUL-11 public-commitment verifier currently includes `etude/server.py` in
its historical source manifest. Follow the established authored-match parity
pattern: preserve the checked RUL-11 receipt and its source manifest
byte-for-byte, recompute every semantic/public-commitment consequence, and
normalize only the checked historical source-manifest field during comparison.
The new RUL-12 receipt supplies the current source identity; RUL-11 remains the
immutable historical proof.

This normalization has one narrow boundary: verifier comparison of the already
checked RUL-11 receipt. The checked receipt bytes, checked source-manifest
bytes, `build_receipt`, `write_receipt`, and every write/generation path remain
unchanged. During verification only, substitute the checked historical source
manifest into the freshly generated comparison value, then require strict
equality for every non-source field and every semantic, replay,
public-commitment, privacy, and atomic-rejection consequence. No RUL-9 verifier
or artifact receives normalization. Only the new independently versioned
RUL-12 receipt binds the current `etude/server.py` and
`etude/replay_index.py` source bytes.

Add focused tests that prove:

- direct summaries are byte-for-byte equal to summaries from the former full
  replay projection over the complete 132-Command authored match;
- a negative characterization test poisons `GameSession.canonical_replay`,
  `project_replay`, and `projection_with_addresses` so each raises immediately,
  then proves `_live_decision_summaries` still returns the byte-identical
  expected rows without calling any of them;
- ordering, hero-viewer filtering, decision digest, address parsing, and all
  five public summary fields are unchanged;
- a non-actor still receives no private hand or command identity;
- the RUL-11 receipt still recomputes 62 commitments with zero
  `RulesProviderGap` and no mutation while its checked bytes remain unchanged.

### 3. Produce an independent RUL-12 workload receipt

Create an additive v1 contract, runner, report, measurement artifact, and
verifier under RUL-12 names. Do not modify the RUL-9 contract, runner, report,
measurement origin, or derivation receipt.

The RUL-12 contract retains the RUL-9 budgets and workload coordinates:

- release: one warmup and ten measured seed-0 games, the same 132-Command tape,
  UR Lessons versus GW Allies, live WebSocket/TestClient ASGI, direct headless,
  and persisted canonical replay surfaces;
- training: `full_clone/current_game_v1`, four workers x 128 simulations, four
  worlds, deal seeds 1197/1419/1887/2197, alternating UR seats, and the existing
  limits;
- release gates: live Command p95 <= 100 ms, inner Command p95 <= 10 ms,
  headless/replay >= 500 steps/s, live >= 1.0 games/s, peak RSS <= 512 MiB;
- training gates: >= 4 roots/s, >= 512 traversals/s, >= 0.04 games/s, PUCT p95
  <= 2000 ms, Command p95 <= 10 ms, peak RSS <= 2 GiB;
- semantic gates: <= 4096 catalog tokens, <= 160 tokens per definition, <= 128
  visible references, and zero overflow, projection failure, or unadmitted
  visible definitions.

Retain raw per-Command and per-game samples. Record at minimum:

- live client-send-to-accepted-ack p50/p95/max and game/step throughput;
- inner server Command p50/p95/max;
- headless and persisted-replay Command p50/p95 and step/game throughput;
- release and training RSS samples and peaks;
- catalog, definition, program, visible-reference, and expanded semantic-token
  distributions plus overflow counts;
- every authority and training fallback counter;
- terminal witnesses, ordered logical consequence hashes, persisted Command
  tape hash, and current public-commitment identity-stream hash;
- the fixed stale-object and stale-revision rejection proofs with unchanged
  state witness and semantic-event cursor;
- viewer privacy counts, spectator rejection, and persisted presentation-track
  privacy;
- selected branch-driver training outcomes, native-apply mismatch count, and
  training throughput.

The end-to-end latency timer remains the RUL-9 timer—from client send through
the accepted WebSocket response—so the result is comparable. TestClient is the
pre-registered ASGI transport used by RUL-9; changing to a different network
harness would reinterpret the budget. Record its relevant runtime versions
(`fastapi`, `starlette`, `pydantic`, `pydantic-core`, `anyio`, `httpx`,
`uvicorn`, and `websockets`) as part of the environment identity and retain the
single-host limitation.

The proof and mutation boundary is fixed: use no summary cache, retain this
same TestClient client-send-to-accepted-ack latency boundary, and keep terminal
trace persistence synchronous and inside the measured game. RUL-12 may remove
only full canonical-replay reconstruction from table-summary construction. It
must rerun the unchanged release and `full_clone/current_game_v1` training
coordinates and gates in the new independently source/binary-bound receipt.

Before starting that full workload, use a two-stage host-admission check. First,
`lf top` must no longer name an unrelated high-CPU search, training, calibration,
Rust/Xcode build, or equivalent competing workload. Then run exactly one seed-0
live game and one direct-headless game through the same RUL-9
`_measure_live_game` and `_measure_engine_game` helpers used by the formal
runner. The control admits the full run only when live Command p95 is at most
100 ms, inner Command p95 is at most 10 ms, live completion is at least 1.0
games/s, headless execution is at least 500 steps/s, and every authority
fallback remains zero. Aggregate host load alone is not sufficient: the
end-to-end control is the readiness decision because prior low one-minute loads
still failed throughput.

A rejected control is diagnostic only. Record its exact metrics and named host
confounder, do not start the full workload, and do not create or overwrite the
default receipt paths. After a passing control, run the exact unchanged warmup,
ten measured release games, and four-worker x 128-simulation training cell. If
that full run becomes contended and misses, preserve its source-bound receipt
and report under an explicit diagnostic suffix and leave the default paths
absent. Only a passing full run may occupy the default receipt and report paths.

The receipt must bind:

- a relative-path/file-SHA source closure containing the changed replay/server
  path, the RUL-12 runner/contract/verifier, semantic compiler/projection,
  training/search path, and all managym Rust sources;
- the loaded `_managym.cpython-312-darwin.so` SHA-256, release profile, Python,
  uv, Cargo, rustc, platform, CPU count, and WebSocket-stack versions;
- the exact contract bytes, authority receipt, Command tape, semantic IR/source,
  learning schema, current parity proof, and current public-commitment proof;
- the frozen RUL-9 file and artifact identities as an input with an explicit
  `rerun: false` role.

Source and binary identity are captured before and after measurement and must
be identical. Summary and verdict rederive from retained raw samples. The
artifact uses canonical-JSON SHA-256 excluding its own hash field. Verification
fails closed on identity drift, missing samples, changed counter inventories,
nonzero fallback/overflow, parity mismatch, privacy/stale-reference regression,
or budget miss.

If an admitted full run unexpectedly misses either live gate, still write the
honest source-bound receipt and report under a diagnostic suffix, return status
2, and rank the retained phase samples for the next decision. Do not weaken a
threshold or add a second optimization in the same artifact.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Are the RUL-9 inputs still immutable? | The measurement file is still `9a3933...`, its embedded artifact is still `498df1...`, and the two-deck semantic IR/source/schema remain `73070b...` / `8154eb...` / `a156c5...`. | The RUL-12 verifier will assert these bytes before doing current work and will never write RUL-9 paths. |
| Is the semantic engine responsible for the live miss? | RUL-9 inner Command p95 was 4.291 ms; headless/replay were 588.5/599.1 steps/s and all selected training gates passed. A current profiled game spent only 0.245 s cumulatively in 132 measured `_step_and_record` calls. | Do not touch managym semantics, branch representation, content, or training code for the fix. |
| Where is current outer time spent? | A kickoff-only cProfile run took 5.43 s and 36.8 million calls. `_response_with_participant` consumed 4.234 s, `_table_snapshot` 4.201 s, and `_live_decision_summaries` 4.188 s. Full canonical replay construction consumed 2.789 s; recursive `deepcopy` consumed 4.137 s. | Remove full replay reconstruction from the per-response summary adapter. No broad WebSocket rewrite is justified. |
| Does the table actually need a replay projection? | `TableDecisionSummary` contains only address, ordinal, revision, prompt ID, and offer ID. The deterministic address namespace/digest inputs are exactly `replay.<match_id>`, `match_id`, and the exact `ReplayDecision` SHA; it does not inspect presentation tracks, reconstructed replay state, or incidental list position. | Add one replay-owned row-address constructor and project summaries directly. |
| Does the smaller projection preserve public bytes? | Over a terminal 132-row authored session, the direct prototype produced exactly the same 55 hero-viewer summary dictionaries and serialized addresses as the existing full replay projection. All four authority fallback counters remained zero. | Lock equivalence with a characterization test before deleting the wasteful route. |
| Is the change large enough to clear both gates? | A kickoff diagnostic—not RUL-9 evidence—ran ten current-source games with the direct projection: live p50 4.956 ms, p95 30.119 ms, 1.638 games/s, 216.2 authority steps/s, inner p95 2.094 ms, one terminal/logical hash, and zero authority fallbacks. | The direct non-cached change has enough margin; stop there and measure it formally. |
| What remains slow after the fix? | Terminal trace finalization produces a roughly 216-230 ms maximum ack in the diagnostic, but it is one of 55 player acknowledgements per game and does not move p95 beyond 100 ms. Trace persistence is the durability boundary. | Keep synchronous terminal persistence. Report max separately; do not trade replay durability for a non-gating tail. |
| Could instrumentation manufacture the pass? | The prototype retained RUL-9's token census, authority-evidence capture, TestClient send/receive cadence, and synchronous terminal trace save. | The formal receipt uses the same conservative path and timers; diagnostics are not subtracted from admission metrics. |
| Will the server edit invalidate settled RUL-11 evidence? | RUL-11's checked receipt includes the server source hash even though the semantic identity stream is unchanged. Authored-match parity already treats a checked source manifest as historical provenance while re-running all consequences. | Apply that same historical-source comparison rule to RUL-11, keep its fixture bytes fixed, and bind current source in RUL-12. |
| Are privacy and stale references orthogonal to the optimization? | Authored parity already checks 798 viewer projections, two redacted presentation tracks, spectator rejection, `stale_object`, and `stale_revision` without mutation. RUL-11 adds 62 public commitments and zero provider gaps. | Recompute and bind both proofs in RUL-12 instead of inferring safety from unchanged payload shapes. |
| What environmental facts can confound the absolute result? | The WebSocket measurement is single-host and package-version-sensitive. Current resolved versions include FastAPI 0.135.1, Starlette 0.52.1, Pydantic 2.12.5, AnyIO 4.12.1, HTTPX 0.28.1, Uvicorn 0.41.0, and websockets 16.0. | Bind those versions and the native extension hash; retain the single-host limitation in the report. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Direct row-to-summary projection through replay-owned address construction | Linear in the small visible decision list; preserves the payload and canonical row authority; measured with strong margin. | Chosen. |
| Incrementally cache summaries or a projected replay | Makes steady-state summary work O(new rows), but introduces derived mutable state and invalidation rules for rematch, reconnect, viewer changes, and terminal transition. | The uncached direct projection already passes; a cache adds correctness surface without a product win. |
| Send only decision deltas or move decision history to a separate endpoint | Could reduce wire size further, but changes protocol recovery, frontend state, reconnect semantics, and compatibility behavior. | This is a wider Game protocol project and is unnecessary for the budgets. |
| Defer or asynchronously persist the terminal replay | Removes the isolated maximum-latency sample, but weakens the accepted-command durability boundary and does little for p95 or total throughput compared with the identified O(history x full-frame) work. | Keep synchronous persistence and exact replay durability. |
| Optimize or replace `full_clone/current_game_v1` | Does not address the profiled outer table-snapshot cost; RUL-9 and RUL-6 already admitted the representation under real training and Study workloads. | Explicitly out of scope and unsupported by the diagnostic. |

## Key decisions

- Canonical replay rows remain the sole decision truth. The optimization is a
  cheaper projection, not a second ledger.
- Replay address construction remains centralized in `replay_index.py`; the
  server will not duplicate address encoding or digest rules. Its deterministic
  inputs are exactly `replay.<match_id>`, `match_id`, and the exact
  `ReplayDecision` SHA, never list position or a reconstructed replay.
- Preserve the complete list of table decision summaries on every response.
  This keeps reconnect and frontend behavior unchanged while removing the
  expensive full-frame work.
- Stop after the direct projection. The diagnostic clears p95 by about 70 ms
  and exceeds the throughput gate by about 64%; speculative caching or
  asynchronous durability is not warranted.
- Compare performance with the same fixed TestClient ASGI client-ack boundary
  as RUL-9. Record actual network transport as a limitation rather than moving
  the goalposts.
- Historical-source normalization applies only while the RUL-11 verifier
  compares a fresh recomputation with its already checked receipt. Checked
  bytes and generation/write paths do not change; every non-source field stays
  strict. RUL-9 receives no normalization, and only the additive RUL-12 receipt
  binds current `server.py` and `replay_index.py` source.
- Rerun the selected training workload even though the code change is in the
  Etude response adapter. The remaining KR requires one release-and-training
  admission receipt, and source/binary binding must be current.
- A measurement miss remains a valid honest artifact but does not close the KR
  or permit budget changes.

## Success and failure modes

Wild success is deliberately boring in architecture: a player action commits
through the same managym Command, the same frame and decision addresses arrive,
Study still sees every historical decision, and the response becomes fast
because Etude stops rebuilding information it already owns. The measurement
artifact becomes the single computable closure for the project's last KR.

Wild failure would be subtler than a crash: a cached or hand-built address
could drift from persisted replay, a viewer could receive the other player's
row, a faster harness could omit serialization or terminal persistence, or a
new artifact could be presented as corrected RUL-9 evidence. The design blocks
those failures with replay-owned address construction, byte-equivalence and
privacy tests, unchanged measurement cadence, explicit frozen provenance, and
independent RUL-12 naming.

## Scope

- In scope: direct table-summary projection; reusable replay row-address
  construction; verifier-comparison-only normalization of the already checked
  RUL-11 historical source manifest, with checked bytes and generation/write
  behavior unchanged; focused characterization/privacy tests; a
  new RUL-12 contract, runner, verifier, report, and source/binary-bound
  measurement receipt; current live/headless/replay/public-commitment/stale and
  selected training proof.
- Out of scope: content breadth; Jeong or any further TLA card; semantic IR or
  public-commitment vocabulary changes; positional/client legality adapters;
  candidate caps; replay or Observation schema changes; branch representation
  replacement; cache architecture; asynchronous trace persistence; protocol
  delta streaming; budget changes; actual-network benchmark replacement;
  editing or regenerating frozen RUL-9 evidence; changing RUL-11 checked bytes,
  generation, or write behavior; removing anything beyond full canonical-replay
  reconstruction from table summaries.

## Done when

The design advances these Rules/Playable Curated World measures:

> A release and training workload records command p50/p95 latency, step and
> complete-game throughput, peak RSS, semantic-program token counts, and
> fallback counters; the compiled path stays within explicit product and
> rollout budgets.

> Live play, headless engine play, and canonical replay reproduce the same
> state witnesses and ordered semantic consequences for fixed terminal seeds,
> while viewer-private facts and stale ObjectRef values remain inaccessible at
> their public boundaries.

Completion requires all of the following:

- the immediately preceding host-admission check records `lf top` without a
  named competing workload and an exact one-game live/headless control passing
  all five readiness conditions; this schedules the measurement but does not
  replace any retained ten-game or training sample;
- `./scripts/verify-rul12-release-stack-budget` exits 0 against the checked
  default additive RUL-12 artifact and emits `RUL12_RELEASE_STACK_OK`;
- ten measured live games report Command p95 <= 100 ms and >= 1.0 complete
  games/s without excluding setup, serialization, accepted acknowledgment, or
  terminal persistence;
- headless/replay, inner Command, release RSS, all training, and all semantic
  capacity gates retain the exact RUL-9 thresholds;
- all three surfaces retain 132 Commands, 133 witnesses, one terminal hash,
  one ordered logical trace, and an exact persisted Command tape;
- with `canonical_replay`, `project_replay`, and
  `projection_with_addresses` poisoned to raise, table-summary construction
  still returns the byte-identical expected rows and proves none was called;
- current proofs retain 798 viewer checks, spectator rejection, redacted
  non-actor command identities, `stale_object` and `stale_revision` atomic
  rejection, 62 public commitments, and zero `RulesProviderGap`;
- authority fallbacks (`legacy_fixed_action`, `card_name_dispatch`,
  `candidate_cap`, `client_legality`), training fallbacks, semantic overflow,
  projection failures, unadmitted definitions, native mismatches, replay
  mismatches, and privacy mismatches are all zero;
- the receipt's source, native binary, Python/WebSocket stack, contract,
  workload, semantic inputs, frozen inputs, raw samples, summary, and verdict
  all reverify;
- SHA-256 checks confirm that the RUL-9 measurement origin, derivation receipt,
  contract, report, and fixed authored authority inputs were not modified;
- RUL-11 checked receipt/source-manifest bytes and its build/write behavior are
  unchanged, every recomputed non-source consequence compares strictly, and
  the RUL-12 source closure independently binds current `server.py` and
  `replay_index.py`;
- debug `cargo test --manifest-path managym/Cargo.toml --no-fail-fast`, focused
  and full uv-managed Python tests, and lint/format checks pass.

## Measure

Baseline authority is the frozen RUL-9 receipt, not a newly sampled pre-change
run:

| Surface | RUL-9 p50 / p95 | RUL-9 throughput | Gate |
|---------|------------------|------------------|------|
| Live WebSocket | 31.590 / 150.492 ms | 45.7 authority steps/s; 0.347 games/s | p95 <= 100 ms; games >= 1.0/s |
| Inner semantic Command | 1.874 / 4.291 ms | included above | p95 <= 10 ms |
| Direct headless | recorded in RUL-9 | 588.5 steps/s | >= 500/s |
| Persisted replay | recorded in RUL-9 | 599.1 steps/s | >= 500/s |
| 4x128 selected training | frozen RUL-9 receipt | 4.520 roots/s; 578.5 traversals/s; 0.0429 games/s | >= 4; >= 512; >= 0.04 |

The kickoff prototype's 30.119 ms p95 and 1.638 games/s are diagnostic design
evidence only. Admission uses the new independently versioned artifact produced
after implementation, with the full raw samples, RSS monitor, source closure,
binary digest, and unchanged thresholds.
