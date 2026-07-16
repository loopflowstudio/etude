# W2-197 — Prove the search-state fork and rollback contract

## Directive and boundary

Directive v1 is incorporated. The branch was reset to current `origin/main`
through `lf rebase` before this design was written, so the implementation starts
from merged PR #72 and its full-clone benchmark baseline.

This Task owns one focused serial PR in the current worktree. It proves the
logical contract against the full-clone reference implementation without
selecting, prototyping, or assuming an undo-journal or page-COW representation.
The checked-in W2-182 raw artifact remains immutable historical measurement
evidence; this Task does not regenerate it or reinterpret it as a three-driver
comparison.

## User-visible outcome

An engine or search developer can run one deterministic Rust contract suite and
see that any search-state driver admitted by the common interface must:

- produce an exact fork whose root and siblings remain isolated in both
  mutation directions;
- support nested LIFO `mark` / `apply` / `rollback` scopes and restore every
  logical fact, legal action, RNG continuation, event boundary, allocation
  watermark, object incarnation, and LKI record;
- return an error without mutation for an illegal or stale-precondition apply;
- produce the same canonical bytes and BLAKE3 semantic hash for equivalent
  independently allocated states driven by the same seeded command trace;
- derive fixed-player projections that expose that player's known information
  and public state, but neither opposing hidden card identities nor a canonical
  hidden-state hash; and
- reject an `ObjectRef` after its entity changes zones and enters a later
  incarnation, including across a speculative branch and rollback.

Current play, training, Python, and browser behavior remain unchanged. The
observable product of this Task is executable proof and a reusable admission
gate for future branching drivers, not a faster search implementation.

## Source of truth

`Game` / `GameState` remain the authoritative mutable rules aggregate.
`ContentPack::content_digest()` names the immutable definitions shared across
forks. `ObjectRef`, `current_object_ref`, `lookup_current_permanent`, and the LKI
table remain the authority for exact rules-object identity.

Promote the logical contract surface currently embedded in
`managym/src/benchmark.rs` into a small `managym::search_state` module:

- `BranchDriver` owns only logical operations: exact fork, explicit
  determinization, rollout reseed, mark, atomic apply, rollback, snapshot, and
  counters;
- `CanonicalSnapshotV2` owns canonical logical JSON bytes, BLAKE3 semantic
  hash, legal-action bytes/hash/count, both fixed-player projection bytes and
  hashes, event boundaries, and an eight-word cloned-RNG continuation probe;
- `FullCloneDriver` remains the reference implementation: `Game::clone` for
  fork/mark and state replacement for rollback; and
- the benchmark module imports and publicly re-exports these names so existing
  benchmark binaries, scripts, and Rust callers do not gain a second contract.

The canonical semantic document uses fixed struct field order and ordered
collections only. It contains all existing logical snapshot fields plus the
currently omitted identity/authority facts:

- physical card object ID, stable definition ID, and owner;
- `object_incarnations` and `object_lki`, with the `BTreeMap<ObjectRef, Lki>`
  encoded as a sorted vector rather than a JSON object with structural keys;
- permanents, card-to-permanent reverse mapping, players, exact zone order and
  reverse membership, turn, priority, stack, combat, and mana;
- committed, pending, and observation event ledgers; trigger queues and choice;
  delayed/exile links; suspended and pending decisions;
- trigger enqueue counter, ID allocation watermark, current action space,
  decision epoch, skip mode/count, and the cloned RNG probe.

Pointers, `Arc` counts, allocator addresses/capacity, Rust `Debug` output,
timings, RSS, driver counters, and `BehaviorTracker` analytics are excluded.
Card definitions are represented once by the immutable content digest; the
test-only `DerefMut` seam for detaching and mutating a physical card definition
is not valid inside a search branch.

Fixed-player projections derive from one Rust visibility authority. Add
`Observation::for_player(game, viewer)` as a current-state projection with no
presentation-event input, and keep `Observation::new(game, recent_events)` as a
compatibility delegate through the same internal builder using
`game.agent_player()`.
For a viewer who does not own the current decision, the projection suppresses
the other player's legal action candidates/focus rather than leaking their
private choice shape. Semantic event ledgers remain in the authoritative
snapshot; viewer-safe presentation deltas are a separate product concern.

Neither canonical semantic bytes nor their hidden-state hash cross the Python
adapter or experience protocol. Product frames, traces, and learning tensors
remain existing consumers of the unchanged `Observation::new` path.

## Concrete build target

1. Extract the current `BranchDriver`, command/receipt/counter types,
   full-clone driver, and canonical snapshot implementation into
   `managym::search_state`; keep benchmark re-exports for source compatibility.
2. Make the canonical snapshot complete for identity and decision authority,
   expose legal-action count and fixed-viewer projections, and bump the current
   shallow schema 1 to canonical snapshot schema 2 because its serialized field
   set changes. Do not hash physical representation or diagnostic fields.
3. Refactor `Observation` to accept an explicit fixed viewer while preserving
   byte-for-byte behavior for the existing acting-player constructor. Suppress
   action candidates for a non-acting viewer and add focused visibility tests.
4. Add a generic contract verifier/test helper parameterized only by
   `BranchDriver`. It must compare complete snapshots, not `Game` fields or
   `Clone`, so later compact-undo and page-COW drivers can invoke the same gate.
5. Add `managym/tests/search_state_contract.rs` for the full-clone reference,
   plus focused identity assertions where `ObjectRef` lookup itself is the
   subject. Keep existing benchmark equivalence tests as compatibility coverage;
   do not count their shallower single-mark check as W2-197 proof.
6. Document canonical inclusions/exclusions and well-formed mark usage in module
   docs. Valid nested use is executable in v1; enforcement diagnostics for a
   foreign, reused, or out-of-order mark belong to a driver that represents
   mark identity and are not simulated with pointer bookkeeping in the
   full-clone reference.

## Contract matrix

| Invariant | Executable probe |
| --- | --- |
| Exact fork | Root, left, and right snapshots are byte/hash/action/RNG/event/projection equal immediately after `fork_exact`; immutable content may be `Arc`-shared. |
| Root and sibling isolation | Apply and determinize left; root and right remain exact. Mutate the root afterward; the already-created siblings remain unchanged. |
| Nested rollback | Mark root, apply A, mark A, apply B, rollback inner to A, replay B to the same result, then rollback outer to root. Check complete snapshots at every boundary. |
| Failed apply is atomic | Use a mismatched expected state/action hash and an out-of-range action. Each returns `Err` and leaves the complete snapshot identical. |
| Representation-neutral equivalence | Build two fixtures independently so their definition `Arc`s are distinct; drive both with the same external seed-derived legal action indices and compare canonical bytes/hash/actions/projections after every step. |
| RNG and seed determinism | Same determinization seed plus same rollout seed yields equal traces and eight-word RNG probes; a controlled different seed changes the authoritative world when hidden state permits it. |
| Hidden-information boundary | Two worlds that differ only in information hidden from player 0 have different semantic hashes but identical player-0 projection bytes/hash. Player 1's own-hand projection detects the changed identities. No projection contains the semantic hash. |
| Stale incarnation | Capture a battlefield `ObjectRef`, mark, move the entity away and re-enter it, then require `StaleIncarnation` for the old ref and success for the new ref. Rollback restores the old ref/LKI/incarnation/snapshot and rejects the speculative new ref. |
| Snapshot completeness | Controlled probes changing incarnation/LKI, decision epoch through a published decision, RNG, event length, allocation watermark, or zone order must change semantic bytes/hash; rollback restores each. |

## End-to-end proof

The primary scenario starts from the exact independently reconstructible
`interactive-midgame-48-v1` fixture supplied by PR #72. It records the
authoritative snapshot and both fixed-viewer projections, forks root/left/right,
executes the nested transaction sequence above, proves root and sibling
isolation, and restores left to the exact root. Two independently allocated
copies are then determinized and reseeded identically and follow a bounded
external seed-derived command trace, comparing canonical bytes, semantic hash,
legal actions, viewer projections, event boundaries, and RNG continuation after
every decision.

The same test creates two different hidden worlds for player 0: authoritative
hashes must differ while player 0's fixed projection stays identical and the
player who owns a changed hidden hand can observe the difference. A focused
identity continuation captures a live object, performs a leave/re-enter inside
a mark, proves the old ref stale, and rolls back to the exact pre-transition
identity and LKI state.

The focused command is:

```bash
cd managym && cargo test --test search_state_contract
```

The proof fails rather than skipping if a fixture is terminal, has no legal
action where one is required, cannot produce distinct hidden worlds across the
fixed seed set, fails to serialize, exceeds its decision cap, or returns a
projection containing opposing hidden identities.

## Affected surfaces and compatibility

- **Rust search contract:** one representation-neutral module becomes the
  authority for snapshot/fork/mark/apply/rollback semantics.
- **W2-182 benchmark:** continues to call the same logical types through
  imports/re-exports. Its checked-in raw artifact and report remain untouched
  historical evidence at their recorded source digest and snapshot schema.
- **Rules identity:** existing `ObjectRef`/LKI behavior is exercised, not
  redesigned. Snapshot coverage expands to include those authoritative facts.
- **Observation/visibility:** an explicit-viewer constructor is additive;
  `Observation::new`, Python bindings, GUI payloads, traces, experience DTOs,
  and tensor encoding keep their current shapes and acting-player behavior.
- **Future drivers:** compact clone plus undo, page-COW plus undo, or another
  representation must implement `BranchDriver` and pass this unchanged suite
  before benchmark numbers are admissible.
- **Automation:** Cargo tests and clippy exercise the proof. The focused Python
  benchmark-structure tests remain green, but no benchmark measurement run is
  part of this Task.

## Absent and error states

- A nonterminal state with no legal action is a contract failure, not a trace
  endpoint. A terminal state is accepted only when both compared traces report
  the same terminal result and complete snapshot.
- An illegal action index, stale expected semantic hash, or stale expected
  action hash must return `Err` and be an exact no-op, including RNG, events,
  allocation, decision epoch, and projections.
- Snapshot serialization failure or unordered/noncanonical data invalidates
  the contract. Hash equality is never substituted for canonical-byte equality
  in the executable proof.
- Equal semantic hashes for controlled non-equivalent identity, RNG, zone, or
  hidden-world probes fail loudly. Cryptographic collision resistance is
  assumed; the tests prove field coverage, not the impossibility of collision.
- A fixed viewer sees their own hand and decision-revealed library cards, public
  zones, and counts for hidden zones. Missing viewer IDs, opponent hand/library
  identities, opponent-private action candidates, or a semantic hash in the
  projection are treated as visibility bugs.
- `lookup_current_permanent` must continue to distinguish `MissingEntity`,
  `StaleIncarnation`, and `WrongZone`; rollback must restore which result is
  correct for every ref captured by the test.
- A future optimized driver that cannot restore any canonical field, or changes
  fixture/action/seed semantics to pass, is rejected before timing evidence is
  considered.

## Operational boundary

The proof is an in-process deterministic Rust suite: no public network, Python
subprocess, benchmark worker group, RSS sampling, wall-clock assertion, or raw
artifact write. It uses the two existing bounded benchmark fixtures, a fixed
small seed set, and a finite decision cap. Canonical JSON allocation and hashing
are correctness instrumentation outside benchmark timers; this Task makes no
claim that snapshots are suitable for per-transition production hot paths.

All Python invocations remain `uv run ...`. Because implementation changes
Rust under `managym/src`, pursue must rebuild the CPython 3.12 release extension
with the repository-prescribed `uv run maturin` command, place the resulting
cp312 extension, and verify it imports before landing.

## Exclusions

- No undo journal, page-COW storage, persistent collection, allocator hook,
  representation tuning, or branching-design selection.
- No rerun or rewrite of W2-182's canonical benchmark artifact, hardware
  receipt, throughput/RSS results, or decision record.
- No differential reference reducer, property/metamorphic/fuzz campaign,
  persisted failure corpus, Phase oracle/matrix, or coverage-gap generator;
  those remain separate Project KRs.
- No product snapshot persistence, replay checkpoint format, save/load API,
  protocol revision, GUI payload, or presentation-event change.
- No broad visibility redesign. The additive fixed-viewer observation seam is
  only the minimum authority needed to prove hidden-information boundaries.
- No runtime support for mutating detached per-card definitions inside a
  search branch; production search assumes immutable `ContentPack` meaning.
- No synthetic enforcement of foreign/reused/out-of-order mark diagnostics in
  the full-clone driver. Later transactional drivers must represent and test
  their own branch/depth/revision mark identity in addition to passing valid
  nested LIFO behavior here.

## Pursue finish line

Pursue is complete when the reusable contract suite passes for
`FullCloneDriver`; the canonical snapshot demonstrably covers identity/LKI,
decision, RNG, allocation, event, zone-order, action, and both viewer
projections; the hidden-world and stale-incarnation scenarios pass; legacy
observation and benchmark callers remain compatible; and these commands are
green:

```bash
cd managym && cargo test --test search_state_contract
cd managym && cargo test
cd managym && cargo clippy --all-targets --all-features -- -D warnings
uv run pytest tests/bench/test_branching_benchmark.py
cd managym && uv run maturin build --release -i ../.venv/bin/python
uv run python -c "import managym"
```

Immediately before the headless finish, run `lf rebase --plan` and `lf rebase`
if main moved, rerun affected verification, then use `lf pr land -c`. Do not use
`lf pr submit` and do not wait for human review.
