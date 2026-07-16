# Dense Page-COW Fork + Undo and the Branching Decision

## Problem

Search KR2 still lacks its third candidate and therefore cannot choose a
branching representation. `full_clone/current_game_v1` and
`compact_clone_undo/current_game_v1` already satisfy the logical
`manabot.search-branching.v1` workload, but the existing matched receipt is
decision-incomplete: clone plus undo is slower in both flat cells and uses more
RSS, while no implementation has tested whether simultaneously retained
siblings can share their large immutable prefixes cheaply.

This work supplies an honest dense page-COW fork plus the existing local undo
journal, runs all three candidates from one source tree and one release binary,
and writes a threshold-driven decision. Search and training developers benefit:
they get a representation choice backed by whole-rollout throughput and peak
RSS instead of clone latency or doctrine. The design advances the Search
Project KR requiring the three-driver reproducible harness, single/saturated
flat and retained cells, raw evidence, and an explicit selection or baseline
retention. It also serves the Rules wave goal by preserving exact identity,
viewer-safe projections, deterministic state, and safe high-throughput forks
without broadening card or format semantics.

## The demo

Infrastructure-only demo: run
`uv run scripts/bench_branching.py run-matrix`, followed by
`uv run scripts/bench_branching.py verify-matrix`. The command emits three
verified raw receipts plus a decision table whose logical checksums match in
every cell and whose final row says either which driver wins under the
pre-registered throughput/RSS thresholds or `retain full clone`.

## Approach

### 1. Page the measured allocation dominant, not the whole object graph

Add an internal `EventLog` storage abstraction for `GameState::{events,
pending_events, observation_events}` with two modes:

- `Dense(Vec<GameEvent>)` is the default for canonical games, full clone, and
  clone plus undo. It retains today's contiguous mutation and exact deep-clone
  behavior.
- `Paged(PagedEventLog)` is admitted only for
  `dense_page_cow_undo/event_pages_4k_v1`. It stores logical event order in a
  small page table of `Arc<EventPage>` values. Each page holds at most
  `max(1, 4096 / size_of::<GameEvent>())` events; on the measured arm64 build,
  `GameEvent` is 48 bytes and a page holds 85 inline events (4,080 bytes).

The release layout probe on `interactive-heavy-80-v1` found:

| Mutable collection | Shape | Reserved inline bytes |
|---|---:|---:|
| committed events | 498 / capacity 512 | 24,576 |
| observation events | 516 / capacity 1,024 | 49,152 |
| cards | 120 / capacity 128 | 4,096 |
| permanents | 28 / capacity 32 | 1,792 |
| card-to-permanent | 120 / capacity 128 | 2,048 |
| incarnations | 120 / capacity 128 | 512 |

The existing allocation receipt independently measures an exact heavy `Game`
clone at 50,507 requested bytes (`51,719,168 / 1,024`). The two long event
histories therefore dominate the bytes actually copied; paging the four entity
arrays in this candidate would add invasive collection APIs to save little
additional memory. The small scalar header, entity arrays, zones, stack, and
flow state remain eagerly dense and isolated on each page-COW fork.

`PagedEventLog` is a safe collection with no raw-pointer or unsafe ownership
code. Forking clones only the `Arc` page table. A write to a shared tail page
copies that page once; later writes are ordinary dense writes. New full pages
are branch-private. `clear`, `take`, and whole-page truncate detach pages
without copying. Logical length is separate from physical tail length so
rollback can hide appended suffixes and drop later pages; a later append makes
the retained tail unique before overwriting hidden entries.

`GameState::page_cow_fork` and `Game::page_cow_fork` are exhaustive manual
constructors: every non-event field is cloned, all three event logs call
`fork_shared`, and no `..` wildcard is allowed. Adding a future authority field
must fail compilation until both the deterministic hash and page fork classify
it. The normal `Clone for Game` remains a deep clone.

The page driver converts the exact fixture's three dense event logs to paged
form once during driver admission, after fixture validation and before the
worker-ready barrier. Conversion moves `GameEvent` values into pages; it does
not change event order or clone nested target vectors. Root admission remains
outside every timed region, just like representation-specific root
construction. Every timed world/simulation fork then exercises shared pages.

### 2. Keep observation/event consumers representation-neutral

Do not flatten paged events merely to feed an observation. Add an internal
borrowed event-view path that lets `Observation` iterate either dense or paged
events in logical order. Internal engine, environment, benchmark, and
`RolloutPool` paths take an `EventLog` batch and project it directly. Preserve
the public `Game::take_observation_events() -> Vec<GameEvent>` compatibility
method by flattening only callers that explicitly request a `Vec`.

`EventLog` implements deterministic sequence serialization and logical
`PartialEq`, so dense and paged modes serialize byte-identically as JSON arrays.
Search witness schema 2 and its BLAKE3 authority/legal/viewer hashes remain
unchanged and contain no page size, pointer, reference count, or allocator
state.

### 3. Reuse the proven undo journal inside each private branch

Add `DensePageCowUndoDriver` with the same `BranchDriver<State = Game>` hooks as
the other candidates:

- `fork_exact` calls the exhaustive page-sharing fork and enables a fresh,
  branch-local undo journal;
- `determinize`, `reseed_rollout`, `mark`, `apply`, and `rollback` reuse the
  clone-plus-undo journal and its LIFO branch/mark validation;
- failed commands remain atomic through the inner mark/rollback used by
  `apply`;
- retained slots remain independently usable `Game` values. They may share
  clean event pages, but never journals, scalar headers, entity arrays, RNGs,
  action spaces, or mutable page contents.

The page driver must produce byte-identical `BranchContractReceipt` values to
the full-clone reference on both fixtures and every contract seed. Add physical
tests proving clean event pages are pointer-shared at fork, first append copies
only the tail page, root and sibling pages/facts remain unchanged, nested
rollback restores the complete witness, and page ownership never changes the
viewer projection or stale-`ObjectRef` result.

### 4. Make driver counters tell the truth

Add a shared `CowStats` owned by the page driver. A copied page allocation owns
an accounting token; allocation increments current copied bytes, final drop
decrements it, and an atomic high-water mark records peak live copied bytes.
Count the page's fixed inline allocation plus any nested `GameEvent` vector
capacity cloned with it. Clean shared pages and page-table pointers are excluded
from `cow_bytes`, as required by contract v1; RSS still captures all physical
overhead.

Correct two existing generic-accounting defects before producing new evidence:

1. Move `eager_forks` and `checkpoint_copies` into driver counters. Full clone
   counts every deep fork and full-snapshot mark; clone plus undo counts deep
   outer forks but zero cursor marks; page COW counts zero full logical-state
   forks and zero cursor marks. The workload must not increment these counters
   merely because it called a logical hook.
2. Summarize a peak counter as the maximum, over sequential measured repeats,
   of the sum of simultaneous worker peaks. Do not sum `cow_bytes` across all
   repeats. Apply the same process-group rule to journal peak bytes/entries.

The page report adds COW peak bytes beside journal peak. Unsupported system
allocator totals stay `null` with the exact unsupported reason; supported COW
and journal counters are numeric, including a valid observed zero.

### 5. Produce one mechanically matched evidence matrix

Extend the harness with the new driver/artifact pair and a `run-matrix`
operation. It:

1. fails closed if any admitted source input is dirty;
2. captures one explicit source closure/digest and exact
   `measurement_code_revision`;
3. builds `branching_bench` once in Cargo release/system-allocator mode and
   records the binary SHA-256;
4. captures one hardware/toolchain record;
5. runs full clone, clone plus undo, and page COW sequentially through the
   unchanged fixtures, seeds, warmups, timing boundaries, RSS sampler, and all
   six cells; and
6. fails if the source tree or release binary changes before the third receipt
   is complete.

`verify-matrix` first runs each existing artifact verifier, then requires equal
contract IDs/digests, source methods/paths/digests, measurement revision,
binary hash/build profile/toolchain, hardware identity, fixture summaries,
workload dimensions, seed paths, simulation/transition/outcome totals, cap
counts, ordered result checksums, and sampled final hashes across all three
candidates. Timing, RSS, and driver counters are the only permitted
differences.

The three artifacts are:

- `experiments/data/w2-182-search-branching-v1.json` and its generated report;
- `experiments/data/w2-198-compact-clone-undo-v1.json` and its generated report;
- `experiments/data/w2-199-dense-page-cow-undo-v1.json` and its generated
  report.

All prior candidate receipts must be regenerated because the page storage,
counter corrections, revision field, and binary hash change the measured
source closure. No number from the current two-driver receipt may be mixed into
the final decision.

### 6. Decide from pre-registered whole-rollout thresholds

Before implementation timing, add
`docs/benchmarks/dense-page-cow-prereg-v1.md` to the admitted source closure.
Correctness and matched provenance are absolute gates. Then apply these rules:

1. Clone plus undo is eligible for sequential use only if both flat cells are
   at least 20% faster in simulations/s than full clone, with no more than 10%
   worse p99 root latency or absolute peak RSS in either cell. This preserves
   W2-198's existing preregistration.
2. Page COW is a meaningful retained-memory win only if both retained cells are
   within 10% of full-clone simulations/s, neither has worse absolute peak RSS,
   `retained-saturated-16-v1` lowers absolute peak RSS by at least 15% and RSS
   delta by at least 40%, and `retained-single-8-v1` lowers RSS delta by at
   least 25%. Absolute RSS has a large fixed process floor in the single cell;
   requiring both absolute non-regression and delta reduction prevents claiming
   that floor as COW memory.
3. Select page COW as the general driver only if every flat cell also stays
   within 10% of full-clone throughput, p99 latency, and absolute peak RSS.
4. If rule 2 passes but rule 3 does not, select a split design: page COW only
   for simultaneously retained `RolloutPool` slots and compact full clone for
   sequential flat rollouts. If clone plus undo independently passes rule 1,
   it may replace full clone only for sequential shapes.
5. If no optimized driver clears its workload-specific bar, explicitly retain
   compact full clone as the production default and keep the optimized drivers
   as conformance/benchmark implementations, not runtime complexity.

Generate `docs/benchmarks/search-branching-decision-v1.md` from the three
verified receipts and the preregistered rule outcome. The record includes raw
artifact hashes, source digest, exact measurement revision, binary hash,
absolute and relative throughput/RSS for all four primary cells, counter
diagnostics, the selected design, rejected alternatives, and the integration
or removal consequence. Clone latency appears only as diagnostic context.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Is there enough mutable state for COW to matter after `ContentPack` separation? | Yes, but it is concentrated. The heavy exact `Game` clone requests 50,507 bytes; the fixture reserves 24 KiB of committed events and 48 KiB of observation events, versus about 8.25 KiB across the four entity arrays. | Page long event histories; keep small state dense/eager. |
| Can an `Arc<Game>` or `Arc<Vec<GameEvent>>` honestly stand in for page COW? | No. Either clones the entire mutable state/component on the first write. The local research explicitly distinguishes component COW from fixed pages. | Use fixed 4 KiB application pages and an exhaustive partial-eager fork. |
| Does Rust provide the needed safe ownership primitive? | Rust 1.96 documents `Arc` clone-on-write as cloning the inner value only when shared and notes atomic reference-count overhead. No mutex is needed because mutation still requires exclusive `&mut` access. | Use safe `Arc` page ownership; measure refcount/indirection cost in whole rollouts. See [official `Arc` docs](https://doc.rust-lang.org/std/sync/struct.Arc.html). |
| Will paging force the canonical/full-clone hot path to pay page traversal? | Not if storage has explicit dense and paged modes and driver admission occurs before timing. | Default `EventLog` to dense `Vec`; only page driver roots use paged mode. |
| Can page storage preserve representation-neutral snapshots? | Yes. Witnesses serialize logical event sequences, not layout. Custom sequence serialization can be byte-identical to `Vec<GameEvent>`. | Add dense-versus-paged serialization/witness equality tests; do not change witness schema 2. |
| Can observation draining destroy the memory win? | The current `take_observation_events() -> Vec` would flatten shared pages for every retained slot. | Add an internal iterable event batch; preserve the public Vec compatibility path only for explicit callers. |
| Does local undo compose with COW? | Yes. The journal already restores event lengths/queues, RNG, allocation watermarks, entities, action space, triggers, and nested marks. Page suffixes can be detached/dropped on rollback while shared prefixes remain immutable. | Reuse one journal per fork; add physical page sharing plus nested logical rollback tests. |
| Are current counters decision-safe? | No. The harness counts every undo cursor as a full checkpoint and sums COW peaks across sequential repeats. | Correct counter ownership/aggregation and regenerate every receipt before comparing. |
| Do current two-driver numbers choose the answer? | No. They share source digest and checksums, but they omit page COW and exact revision/binary identity. They show only a prior: undo is -2.9%/-4.4% in flat throughput and +53.8%/+50.5% in flat absolute RSS; retained is +1.9%/-0.3% throughput and +4.7%/+8.5% RSS. | Preserve the raw prior, but make no selection until the one-build three-driver matrix. |
| How is “one build” proven? | Source digest alone does not identify the emitted binary or exact code commit. | Record one measurement revision and binary SHA-256, build once in `run-matrix`, and require exact equality in `verify-matrix`. |
| Can peak copied bytes be measured without a counting allocator? | Yes. Page allocations are controlled by the driver, so lifetime tokens can track copied bytes exactly even though unrelated system allocations remain unsupported. | Emit numeric `cow_bytes`; keep global allocation fields null. |
| What could make the receipt stale after review/rebase? | Any admitted engine/harness/test change changes the git-tree source digest; revision-bound evidence has failed review before. | Run canonical evidence only after code is committed and final-rebased; rerun after any admitted change. Post-landing verification checks the merge tree from canonical main and an independent Python 3.12 clone. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| `Arc<Game>` plus `Arc::make_mut` | Minimal implementation; cheap untouched fork | Whole-state COW copies roughly 50 KiB on the first mutation and is not the commissioned fixed-page candidate. |
| `Arc<Vec<GameEvent>>` per queue | Smaller change and shares history | Component COW copies the entire 498/516-event log on the first append, eliminating the retained-slot hypothesis. |
| Page every `GameState` vector and zone immediately | Maximizes theoretical sharing | The measured entity arrays are small; it would force broad non-contiguous collection APIs through more than 500 state accesses before evidence says they matter. |
| Representation-generic rules engine with separate dense and paged `Game` types | Clean long-term abstraction | It is a flag-day kernel refactor, broadens W2-199, and risks changing rules behavior instead of measuring a storage candidate. |
| OS virtual-memory COW/fork | Hardware pages and mature kernel mechanisms | It is process-oriented, poor for in-process retained slots, non-portable to Windows/WASM, and makes deterministic application-level byte accounting opaque. |
| Persistent HAMT/RRB state | Very cheap arbitrary forks | Dense indexed facts and append histories are the measured workload; prior Phase evidence shows hashing/indirection can dominate. The wave explicitly rejects adopting persistent collections by doctrine. |
| Keep full clone without building the third driver | Simplest runtime | It cannot complete Search KR2 or establish whether retained prefixes have a material memory win. |

## Key decisions

- Driver ID is `dense_page_cow_undo/event_pages_4k_v1`; the ID names the actual
  copy granularity rather than implying every field is paged.
- Page size is fixed at 4 KiB of inline `GameEvent` storage for v1. No page-size
  sweep occurs after seeing canonical results.
- Canonical/full-clone/clone-undo states remain dense. Page conversion is a
  driver-specific root-admission cost outside the timed region and is still
  recorded/tested for correctness.
- Pages use safe `Arc` ownership with exclusive in-place mutation after
  uniqueness; there is no unsafe code, mutex, OS COW, or randomized hash map.
- Every retained slot owns a separate `Game` and journal. Page sharing never
  serializes a simultaneous workload or aliases mutable facts.
- Snapshot/witness schema 2 and contract ID stay unchanged. Physical sharing is
  tested separately and never enters semantic identity.
- Existing receipts are invalidated and regenerated on the final code tree.
- The simplest threshold winner is selected. A small microbenchmark win cannot
  override whole-rollout throughput/RSS, and a memory win cannot override
  correctness.

## Success and failure conditions

Wild success is visible when 264 retained logical states share the six or seven
clean history pages from their root, copy only dirty tails, cut saturated RSS
enough to admit a larger policy batch, and keep transition throughput within the
10% guardrail. The pleasant surprise would be that the public engine remains
dense and unchanged while only search forks opt into the representation.

Wild failure is a page abstraction that taxes every step, secretly flattens on
observation, reports cumulative bytes as a peak, or restores logical values
while retaining unbounded private page suffixes. The kill response is explicit:
if the preregistered thresholds fail, retain full clone and remove/contain the
paged runtime path rather than tuning thresholds or expanding pages across the
kernel after seeing the answer.

## Scope

- In scope: dense/paged event-log storage, the page-COW-plus-undo driver,
  physical and representation-neutral contract tests, truthful driver
  counters, one-build matrix orchestration, all three fresh raw receipts and
  reports, preregistration, and the final decision record.
- In scope: exact source revision, selected git-tree digest, binary digest,
  hardware/toolchain, raw RSS samples, deterministic checksums, failures, and
  post-landing verification instructions/evidence.
- Out of scope: new cards, rules semantics, action ABI changes, neural
  inference, changed benchmark fixtures/seeds/workloads/timing/RSS protocol,
  production search integration beyond the decision, HAMTs, OS COW, and paging
  the small entity/zone/flow components without new measured evidence.

## Done when

- Directive v1 is acknowledged and the unchanged
  `manabot.search-branching.v1` contract hash is present in all three receipts.
- Debug Rust tests pass, including full contract equality for all three drivers,
  nested rollback, root/sibling isolation, hidden viewer projections, token/ID
  allocation rollback, exact zone order, deterministic RNG/hashes, and stale
  `ObjectRef` behavior:

  ```bash
  cargo test --manifest-path managym/Cargo.toml
  ```

- Rust formatting/lint and Python harness tests pass:

  ```bash
  cargo fmt --manifest-path managym/Cargo.toml -- --check
  cargo clippy --manifest-path managym/Cargo.toml --all-targets -- -D warnings
  uv run --extra dev pytest tests/bench/test_branching_benchmark.py -q
  ```

- Because `managym/src` changes, rebuild the Python extension with the pinned
  Python 3.12 interpreter before final validation:

  ```bash
  cd managym && uv run maturin build --release -i ../.venv/bin/python
  ```

- `run-matrix` produces three canonical artifacts from one release binary and
  `verify-matrix` rejects any provenance, fixture, workload, count, checksum,
  cap, or final-hash mismatch:

  ```bash
  uv run scripts/bench_branching.py run-matrix
  uv run scripts/bench_branching.py verify-matrix
  ```

- The page receipt has supported numeric journal and COW peaks, `eager_forks =
  0`, `checkpoint_copies = 0`, identical max-live-state counts, zero contract
  failures, and no unsupported counter disguised as zero.
- `docs/benchmarks/search-branching-decision-v1.md` applies the pre-registered
  rules to all four primary cells, names the winner or retains full clone, and
  includes all three artifact hashes, source digest, exact measurement revision,
  and binary hash.
- After landing, the same matrix verifier passes from canonical main and from
  an independent clean clone with `uv venv --python 3.12`; only then may Search
  KR2 be claimed.

## Measure

The canonical comparison records, for every flat/retained and
single/saturated cell: simulations/s, transitions/s, root p50/p95/p99, absolute
peak RSS, peak RSS delta, max live states, cap rate, eager full copies, full
checkpoints, journal marks/commits/rollbacks/peak bytes/entries, page-COW peak
bytes, fork/mark/rollback time, ordered result checksum, sampled final hashes,
and failures. Clone-and-drop and step latency remain secondary diagnostics.

The current same-digest two-driver receipt is only a prior:

| Cell | clone+undo throughput vs full | clone+undo absolute RSS vs full |
|---|---:|---:|
| flat single | -2.93% | +53.80% |
| flat saturated | -4.44% | +50.45% |
| retained single | +1.91% | +4.65% |
| retained saturated | -0.25% | +8.53% |

The final report replaces this table with all three fresh candidates on the
same final source digest, exact measurement revision, and binary hash. Better
means clearing the pre-registered workload-specific thresholds above, not
merely ranking first among three noisy numbers.
