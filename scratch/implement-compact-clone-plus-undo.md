# W2-198: Compact Clone Plus Journaled Undo

## Problem

Search KR2 needs matched whole-rollout evidence for three branching strategies.
W2-182 established `manabot.search-branching.v1` and the compact full-clone
reference; W2-198 must add only Candidate 2, compact clone plus undo. Search
workers should retain exact isolated clones at ownership boundaries while a
sequential worker reuses one dense world state across sibling rollouts through
`mark`/`rollback`.

The implementation is useful only if rollback restores the complete rules
authority, including facts that are easy to miss because they are outside
combat: RNG continuation, zone order and reverse membership, identities and
allocation watermarks, event ledgers, stack and resolution queues, triggers,
choices, caches, and the surfaced action space. It must also preserve the
contract's workload. In particular, retained policy slots must remain
simultaneously live and independently mutable.

This Task advances the Search Project's second KR by adding the second of the
three required drivers and matched full-clone evidence. It does not complete
the KR: dense page-COW plus undo remains W2-199, and no representation may be
selected until that comparison exists.

## The demo

Run `cargo test --manifest-path managym/Cargo.toml --test search_state_contract`
and see both drivers pass the same fork, nested rollback, failure-no-op,
visibility, stale-object, allocation, and seeded-trace gates. Then run
`uv run scripts/bench_branching.py run` followed by
`uv run scripts/bench_branching.py verify` and get a matched full-clone versus
clone-plus-undo report for all four whole-rollout cells, including throughput,
latency, RSS, live-state counts, eager forks, and journal/checkpoint counters.

## Approach

### 1. Add transaction metadata without changing rules state or public ABIs

Add an optional `UndoJournal` to `Game`. The field is internal transaction
metadata: it is excluded from witnesses, observations, policy inputs, Python
bindings, hashes, and serialization. Ordinary product and environment games
leave it disabled and retain their current behavior. `ClonePlusUndoDriver`
enables it for benchmark states and uses the stable ID
`compact_clone_undo/current_game_v1`.

Keep `BranchDriver::State = Game` for both drivers. This avoids a parallel
rules engine and lets every existing transition continue to execute the same
code. Replace the derived `Game::clone` with an explicit logical clone that
copies every authority field but starts with fresh, empty transaction metadata;
an exact fork must never copy another branch's undo history or active marks.
`Arc<ContentPack>` remains shared exactly as it is now.

`ClonePlusUndoMark` contains only:

- the journal cursor, branch ID, nested mark ID, and LIFO depth;
- a compact RNG checkpoint (`seed`, `stream`, and `word_pos`);
- the object-ID watermark and allocation-vector lengths needed to discard
  newly allocated cards/permanent slots and reverse-index entries; and
- the live journal-byte value at the cursor for accounting.

The pinned `rand_chacha` 0.3.1 API exposes `get_seed`, `get_stream`, and
`get_word_pos`, plus their restoration operations. Those values reproduce the
next RNG word and avoid logging every random draw. `IdGenerator` and
`ZoneManager` gain narrow watermark/restore helpers; no allocator or raw-memory
snapshot is used.

Marks are branch-local, revision-bound, nested, and LIFO. Debug builds reject
a mark from another exact fork, a reused mark, and out-of-order rollback.
Rollback replays inverse entries in reverse order, restores the fixed
checkpoint, truncates allocation vectors to their watermarks, and leaves the
journal at the recorded cursor.

### 2. Route every admitted mutation through explicit inverse operations

Introduce journal-aware mutation helpers and remove raw mutable access from the
runtime paths reached by `Game::step`, determinization, reseeding, and
observation-event consumption. A helper records an inverse only when a journal
scope is active; the canonical non-search path pays one predictable disabled
branch and performs the same mutation.

Start with explicit, uncoalesced before-images. Repeated writes may create
repeated entries; that cost is part of Candidate 2's evidence and must not be
hidden by a risky first implementation of nested dirty-bit coalescing.

| Authority family | Inverse representation | Primary call sites |
|---|---|---|
| Player/permanent facts and mana caches | Compact cell before-image keyed by player or permanent ID | `damage`, `mana`, `zones`, `resolution`, `tick`, combat actions |
| Turn and priority | Before-image of the compact state or a field-level old value | `tick`, `stabilization`, `action`, `play`, triggers |
| Combat | Replace/create/drop entries plus inverse queue/map edits | `combat_actions`, `tick`, token creation |
| Ordered zones and reverse membership | Card, owner, old/new zone, exact old/new position, old reverse entry; whole-zone before-image only for shuffle/resample/reorder | `zones`, `decision`, `search`, SBA |
| Stack and resolution queues | Old length for push; removed index and value for pop/remove; old option for suspended resolution | `zones`, `resolution`, `decision`, triggers |
| Events and trigger queues | Old length for append; moved vector for `take`/clear; removed index and value for ordered removal | `zones`, `tick`, `triggers` |
| Delayed triggers and exile links | Push/truncate or moved-vector before-image for filter/replace | `zones`, `resolution` |
| Identity and allocation | Incarnation cell, prior LKI map value, mark-time ID/vector watermarks, reverse-slot before-image | `identity`, `zones`, `play`, triggers |
| Choice and published decision | Prior `PendingChoice`, current `ActionSpace`, decision epoch, skip count, and terminal tracker counters | `play`, `decision`, `tick` |
| RNG | Fixed mark checkpoint and exact reseed before-image for nested scopes | driver reseed, random tails, shuffle/determinize |

Do not expose a generic journal-blind `zone_cards_mut` or mutable permanent/
player accessor to these paths. Use narrow closures or semantic operations so a
future edit has one obvious place to add an inverse. Scenario-only setup helpers
may remain outside transaction mode, but any helper invoked after a driver mark
must use the mutation authority.

`ClonePlusUndoDriver::apply` validates state/action preconditions and index
bounds before mutation. It then opens an internal nested mark around
`Game::step`. On success it commits that internal mark while retaining inverse
entries required by any outer search mark; when no outer mark exists it clears
committed entries. On any error it rolls back the internal mark before returning
the error. This makes failure an exact no-op without a cloned `Game` checkpoint.

Journal accounting is explicit rather than based on `size_of_val`, which does
not include allocations owned behind `Vec`, `String`, or maps. Each entry
reports its inline size and owned heap capacity. The driver tracks current and
peak allocated journal bytes, current/peak entry counts, marks, commits, and
rollbacks. `journal_bytes` is peak live allocated undo storage, including the
journal and mark-stack backing capacities. `checkpoint_copies` remains zero
because a fixed mark checkpoint is not a full-state copy. Full clone reports
the new journal counters as `null` with a reason; clone-plus-undo reports COW
as `null` with a reason. System-allocation counters remain unsupported unless
an existing counting hook is added for both drivers.

### 3. Exercise the exact same contract, plus adversarial journal coverage

Parameterize the existing contract tests over `FullCloneDriver` and
`ClonePlusUndoDriver`. Both run `verify_branch_contract` and
`verify_seeded_trace_equivalence` unchanged on both fixtures and all four
contract seeds. Add a cross-driver oracle check that drives full clone and
clone-plus-undo through the same external actions and compares the complete
`SearchStateWitness` after every transition.

Add focused tests that cannot pass with a combat-only journal:

- mark, determinize/reseed, and rollback restores both hidden-zone order and
  the eight-word RNG continuation probe;
- a long seeded trace from each fixture runs under one outer mark, reaches its
  terminal/cap witness, rolls back exactly, then replays to the same result;
- token creation allocates card, permanent, reverse-map, incarnation, and
  object IDs; moving it through zones advances incarnation/LKI; rollback
  restores vector lengths, watermarks, order, lookups, and stale/new
  `ObjectRef` behavior;
- nested marks cover stack push/removal, events, pending triggers, a surfaced
  choice/action space, and replay after inner rollback;
- stale state hash, stale action hash, and out-of-range index failures leave
  authority, legal surface, both viewer projections, event boundaries, RNG,
  and journal cursor unchanged; and
- reverse isolation proves mutating either the root or a fork cannot alter any
  already-created sibling.

The full-clone driver remains the differential oracle. A candidate mismatch is
a correctness failure; timing is not run and no partial performance artifact is
written.

### 4. Make the benchmark genuinely driver-neutral without changing v1 work

Replace the full-clone constants in `benchmark.rs`, `branching_bench`, and
`bench_branching.py` with validated driver selection. Keep the contract ID,
fixtures, tapes, seeds, action order, cap, worker/actor/world/rollout dimensions,
warmups, timer boundaries, 5 ms RSS protocol, and raw worker result shape
unchanged.

All transitions in the generic workload, including policy plies and random
tails, go through `BranchDriver::apply`; direct `Game::step` calls would bypass
the candidate journal.

For sequential cells:

1. Both drivers exact-fork and determinize one independently owned world.
2. Full clone exact-forks one simulation for every action/rollout pair, as it
   does today.
3. Clone-plus-undo marks the owned world, reseeds, applies the root action,
   plays the identical tail, checksums the terminal state, and rolls back
   before the next sibling.
4. Rollback time stays inside root elapsed time and is also reported
   diagnostically. Correctness hashing remains excluded exactly as in v1.

For retained cells, both drivers exact-fork every live simulation slot.
Clone-plus-undo may use its journal to make an individual step atomic, but it
may not alias slots, reconstruct them outside the timer, or replace the pool
with one sequential scratch state. `max_live_states` must therefore match the
logical retained shape for both drivers. A sequential clone-plus-undo cell is
expected to report fewer eager forks/live states; that is the intended physical
lifecycle, not a change in simulations or action order.

Collect driver counters after measurement, not before. Extend raw metrics with
nullable journal mark/entry/rollback counters and rollback seconds while
retaining the fixed meanings of eager forks, checkpoint copies, journal bytes,
COW bytes, allocation counters, and max live states.

### 5. Preserve single-driver raw receipts and generate a matched comparison

Keep `experiments/data/w2-182-search-branching-v1.json` as a valid, regenerated
single-driver v1 receipt for `full_clone/current_game_v1`, and regenerate its
Markdown report. Add
`experiments/data/w2-198-compact-clone-plus-undo-v1.json` as the corresponding
single-driver v1 receipt for the new driver, plus a W2-198 report whose primary
tables show matched full-clone and clone-plus-undo values and ratios.

One default `run` invocation builds once, captures one source digest and one
hardware/build record, completes all equivalence checks for both drivers before
timing, measures both drivers, cross-checks fixture/workload/seed identities and
logical result checksums, then atomically writes both receipts and reports. One
default `verify` invocation validates both artifacts, both generated reports,
and their cross-driver match. Generated evidence files are explicitly excluded
from `source_sha256`; code, contract, and the permanent preregistration are not.

Before any optimized-driver timing, copy the thresholds in this design into a
permanent source-controlled preregistration under `docs/benchmarks/`. The
canonical run happens after the final Loopflow rebase. Because `source_sha256`
hashes the whole non-generated tree, any later rebase or source edit invalidates
both receipts and requires rerunning `run`; `verify` must pass again immediately
before submission/landing.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Is the existing semantic gate healthy before this change? | On 2026-07-16, `cargo test --manifest-path managym/Cargo.toml --test search_state_contract` passed all 5 existing tests. | Preserve the verifier and parameterize it; do not replace it with representation-specific assertions. |
| Is the W2-182 receipt actually stale? | `uv run scripts/bench_branching.py verify` fails first at `contract digest mismatch`. The directive identifies the subsequent whole-tree source mismatch as well. | Regenerate full-clone evidence from the final implementation tree in the same run as the candidate. |
| Can this host produce canonical saturated evidence? | The host is an Apple M4 Max with 16 physical/logical cores and 128 GiB RAM. | Run the canonical 8-worker saturated cells without oversubscription. |
| Is the harness already generic enough? | No. Driver IDs, worker validation, equivalence, lifecycle functions, artifact validation, and reporting are hard-coded to full clone despite the trait. | Driver-neutral harness work is required scope, but v1 workload semantics remain frozen. |
| Where can mutations escape? | Runtime writes are spread across action, play, decision, tick, zones, damage, mana, combat, resolution, triggers, stabilization, SBA, identity, search, and observation-event consumption. `ZoneManager::zone_cards_mut` and direct `as_mut`/`iter_mut` calls bypass an authority boundary. | Add narrow journal-aware operations and validate with long trace-then-rollback plus targeted allocation/zone tests. |
| Must every random draw become a journal entry? | No. `rand_chacha` 0.3.1 exposes seed, stream, and word position, and its source tests demonstrate cloned/restored continuation equivalence. | Store a fixed RNG checkpoint per mark and restore it exactly. |
| Can vector rollback rely on hidden allocator/layout behavior? | Rust guarantees ordered contiguous `Vec` contents and stable `len`/`capacity` APIs, but not growth strategy or field layout. | Use safe `truncate`, `insert`, `remove`, and exact indices; do not retain raw pointers or use unsafe byte snapshots. |
| Does `size_of_val` measure journal memory? | It measures the pointed-to value, not recursively owned allocations. | Maintain explicit owned-capacity accounting per undo variant and compare it with sampled RSS. |
| Can undo collapse retained policy slots? | No. The contract requires every slot to remain simultaneously usable; the current retained cell reaches 264 logical live states. | Retained cells keep exact forks. Any implementation reporting fewer logical slots is invalid, not faster. |
| Does immutable content need journaling? | No. `GameState::content` is already an `Arc<ContentPack>` and witnesses bind its digest rather than pointer/layout identity. | Exact forks share the pack; journal only mutable match authority. |
| When is a revision-bound receipt fresh? | `source_sha256` hashes the entire non-generated tree. A rebase after measurement can invalidate evidence even when benchmark files themselves are unchanged. | Rebase first, measure once at the landing tree, verify again before Loopflow submits or lands, and regenerate after any source-changing rebase. |

Primary API references used for these findings:

- [Rust `Vec` guarantees and operations](https://doc.rust-lang.org/stable/std/vec/struct.Vec.html)
- [Rust `size_of_val`](https://doc.rust-lang.org/std/mem/fn.size_of_val.html)
- [`rand_chacha` state/continuation source](https://docs.rs/rand_chacha/latest/src/rand_chacha/chacha.rs.html)

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Clone `Game` at every mark or before every apply | Very small implementation and obvious correctness | This is the full-clone reference under another name. It violates the cursor-plus-minimal-checkpoint directive and cannot measure Candidate 2. |
| Diff a cloned before-state against the after-state and store field snapshots | Produces a journal without touching every mutator | It still copies the full state on every transition, hides mutation escapes until after the fact, and measures diffing rather than journaled execution. |
| Replay the root action tape to reset a scratch state | Avoids inverse entries for long playouts | Replay changes the timed work, can consume RNG differently, and cannot satisfy nested rollback or retained slots. It is a separate checkpoint/replay candidate, not W2-198. |
| Replace all state containers with generic `Tracked<T>` wrappers | Stronger compile-time enforcement of mutable access | It changes the dense representation and public/internal access surface broadly, adds indirection to reads, and would confound the later page-COW comparison. Narrow semantic mutators are the smaller truthful change. |
| Implement page-COW now and share its transaction layer | Could amortize some refactoring | W2-199 is explicitly gated and must provide independent third-candidate evidence. Mixing it into this PR would preselect an architecture and make attribution impossible. |

## Key decisions

- Candidate 2 is an explicit inverse journal, not a snapshot, diff, or replay
  reset disguised as undo.
- Exact eager clones remain at root/worker/actor/world boundaries and for every
  retained slot. Only sequential siblings reuse one world scratch state.
- The first journal is explicit and uncoalesced. Its actual volume and rollback
  cost are evidence; optimize coalescing only in a separately measured follow-up.
- RNG and allocation lengths are fixed mark checkpoints because logging every
  random word or append would add noise without improving semantics.
- Every benchmark transition uses the driver. The full-clone path remains the
  behavioral oracle and keeps the same logical checksums.
- The existing W2-182 receipt remains a standalone full-clone artifact; W2-198
  adds a standalone candidate receipt and a cross-verified comparison report.
- No positive timing result selects a representation or checks off Search KR2.
  W2-199's page-COW evidence is mandatory first.

## Success and failure modes

Wild success is not merely a faster microbenchmark. The same engine actions run
unchanged, a developer can mark any admitted position and explore/replay nested
siblings with exact identity and visibility semantics, and sequential search
deletes most inner full clones without harming the retained policy pool. The
journal counters explain why the gain exists and give W2-199 a trustworthy
comparison surface.

Wild failure is a fast driver that silently misses a queue, reverse index, RNG
advance, allocation, or action-space write and biases millions of rollouts. A
second failure mode is benchmark fraud by serializing retained states or moving
reset work outside the timer. Long trace rollback, targeted allocation/zone
tests, cross-driver checksums, fixed max-live-state validation, and raw journal
counters are blocking defenses against those outcomes. A truthful result in
which the journal is slower or larger than full clone is still successful W2-198
evidence.

## Scope

- In scope: the clone-plus-undo driver; optional Game transaction metadata;
  journal-aware mutations needed by both contract fixtures and targeted
  allocation/zone probes; driver-neutral benchmark execution; cross-driver
  equivalence; journal counters; canonical full-clone and candidate artifacts;
  matched report generation; stale W2-182 receipt repair.
- Out of scope: dense page-COW, a final representation decision, Search KR2
  completion, production search-entry adoption, new card/rules behavior,
  Python or policy-observation ABI changes, neural inference, alternate
  fixtures/workloads/seeds, generic persistent containers, and journal
  coalescing based on observed timings.

## Done when

- `FullCloneDriver` and `ClonePlusUndoDriver` pass the same contract suite on
  both fixtures, including nested rollback, reverse isolation, hidden viewers,
  stale `ObjectRef`, deterministic seeded traces, and exact failure no-ops.
- Long trace-then-rollback and targeted token allocation/zone-order tests prove
  the candidate restores all witness components, vector/allocation watermarks,
  reverse lookups, and RNG continuation.
- `cargo test --manifest-path managym/Cargo.toml` passes.
- `cargo clippy --manifest-path managym/Cargo.toml --all-targets -- -D warnings`
  passes, and formatting is clean.
- `uv run scripts/bench_branching.py run` completes canonical measurements for
  both drivers on this 16-core host only after all equivalence checks pass.
- `uv run scripts/bench_branching.py verify` validates both raw artifacts,
  reports, source/contract digests, worker argv, process groups, RSS series,
  summaries, driver counters, logical checksums, and matched workloads.
- The W2-182 baseline is rebound to the exact current source tree and the
  W2-198 report explicitly states that Candidate 3 and the final decision are
  pending.

This advances the KR fields for hardware, seeds, workload definitions, raw
results, total rollout throughput, and peak RSS for full clone and compact
clone-plus-undo. The KR's page-COW measurement and selection clause remain
open.

## Measure

These expectations are preregistered on 2026-07-16 before implementing or
timing the optimized driver. Promote them verbatim to a permanent benchmark
preregistration before the first clone-plus-undo timing run.

Correctness is a hard gate:

- zero witness, outcome, cap, event-boundary, action-order, or RNG mismatches;
- identical simulation/transition counts and ordered logical checksums for
  every matched driver/cell/seed/worker; and
- no timing artifact written after an equivalence failure.

Performance is decision evidence, not a Task pass/fail gate:

- A material sequential win signal is at least 20% higher simulations/s for
  clone-plus-undo in **both** `flat-single-64-v1` and
  `flat-saturated-64-v1`, with no more than 10% worse p99 root latency or peak
  RSS in either cell.
- Retained cells are expected to remain within 10% of full-clone throughput and
  peak RSS because every live slot still requires an exact clone. A larger
  regression is evidence that journaled atomic steps tax the batched-policy
  shape; a claimed retained memory win is invalid unless `max_live_states` and
  all dimensions remain identical.
- Report simulations/s, transitions/s, root p50/p95/p99, absolute peak RSS,
  peak RSS delta, max live states, eager forks, checkpoint copies, journal
  marks/entries/rollbacks, peak journal bytes, rollback time, cap rate, and
  deterministic checksum status for every primary cell.
- Report null-with-reason for unsupported counters. Never turn unsupported
  allocation or COW counters into misleading zeroes.

Whether these thresholds are met or missed, retain the raw results. Do not
select full clone or clone-plus-undo until W2-199 measures dense page-COW plus
undo under the same contract.
