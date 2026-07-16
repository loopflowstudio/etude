# W2-208: Prove ContentPack sharing and clone allocation boundary

## Directive and outcome

Directive v1 was acknowledged with:

```text
lf task acknowledge W2-208 --directive 1 --summary "Clarify a focused executable proof for shared Arc<ContentPack> identity, isolated MatchState clones, and deterministic clone-allocation independence from immutable definition bytes, while preserving existing ABIs and excluding broader performance or branching-design claims."
```

This Task does not change player-visible rules or search policy. It gives engine,
training, and search maintainers an executable guarantee that every environment
using the currently admitted default content version shares the same immutable
`Arc<ContentPack>`, while exact branches own independent mutable match facts.
It also gives reviewers a checked, reproducible allocation receipt showing that
exact clone allocations are determined by mutable state, not by the number or
byte size of immutable card definitions.

The existing Python methods, observation layout, fixed action indices, and search
results remain unchanged. No content-pack identity or measurement field is added
to the Python ABI.

## Source of truth

- `cardsets::alpha::default_content_pack()` and its process-wide
  `OnceLock<Arc<ContentPack>>` are the ownership source of truth for the one
  content version currently admitted by `Game::new`.
- The admitted version is identified by the pair
  `(ContentPack::schema_version, ContentPack::content_digest())`. The schema
  version alone is not treated as a content identity.
- `GameState::content` is the match-local shared reference. `CardDefId` and each
  card's shared definition handle are derived references into that pack;
  `GameState`'s players, zones, cards, permanents, events, choices, RNG, and ID
  watermark are mutable match facts.
- `Game::clone` is the current exact-copy primitive used by direct branches,
  `Env::fork`, flat-MC world/sibling clones, `RolloutPool`, and the full-clone
  benchmark driver. W2-208 proves the content ownership property of those
  copies; it does not redefine their broader search-state semantics.
- The executable tests are the live gate. The checked experiment artifact is a
  historical receipt for its recorded source revision and machine, not a
  timeless performance baseline.

## End-to-end proof

Use one deterministic interactive-mirror match fixture and exercise the actual
ownership consumers:

1. Reset two independently constructed `Env` values with different seeds.
   Assert that both games have the same admitted `(schema, digest)` and are
   pointer-identical to `default_content_pack()`.
2. From one environment, make an exact root fork and at least two sibling
   branches. Build a small `RolloutPool` so its world/action/rollout slots cross
   the production search clone path. Every retained game must have the same pack
   pointer and digest as the source. Existing flat-MC uses the same `Game::clone`
   primitive and remains covered by its behavioral test; do not add a parallel
   fork abstraction owned by W2-197.
3. Mutate one branch through a legal action and one explicit mutable-fact check
   (for example life total). Assert the source and untouched sibling retain
   their prior deterministic hashes/facts, while the mutated branch changes.
   Assert all three still share the original pack and card-definition handles.
4. Run a dedicated allocation test outside the general branching benchmark.
   Starting from one warmed deterministic `Game`, construct:
   - a baseline pack cloned from the admitted pack, preserving the base
     definition `Arc`s; and
   - an expanded pack made from that baseline by appending synthetic, unused
     definitions whose serialized immutable payload is at least 4 MiB larger.
     The two game fixtures are cloned from one root and differ only in their
     `GameState::content` reference.
5. With a counting global allocator and one test thread, measure preallocated
   `Arc::clone` bookkeeping, sequential `GameState::clone`, and sequential exact
   `Game::clone` for a fixed clone count. Pack construction, digesting,
   serialization, warmup, formatting, and artifact writes are outside the
   counter snapshots.

The focused proof commands are:

```bash
cargo test --manifest-path managym/Cargo.toml --test content_pack_tests
cargo test --manifest-path managym/Cargo.toml --lib content_pack_contract -- --nocapture
cargo test --release --manifest-path managym/Cargo.toml --test content_pack_clone_allocations -- --nocapture --test-threads=1
```

The proof holds only if all identity/isolation assertions pass and the allocation
gate reports:

- `Arc<ContentPack>` reference clones: exactly 0 measured allocations and 0
  measured allocated bytes after preallocating reference storage, with the
  strong count increasing by the requested reference count during the probe;
- mutable `GameState` and exact `Game` clones: positive allocation counts and
  positive allocated bytes, proving the counter observed real clone work; and
- expanded-pack minus baseline-pack clone totals: exactly 0 allocations and 0
  bytes for both `GameState` and `Game`, after proving the expanded digest differs
  and its serialized immutable payload is at least 4 MiB larger.

Exact equality is the failure threshold. This is an allocation-boundary
invariant, not a latency tolerance.

## Implementation surfaces

Expected changes are deliberately narrow:

- Extend `managym/tests/content_pack_tests.rs` with explicit admitted-version
  digest, root/sibling pointer identity, shared card-definition identity, and
  mutable-branch isolation assertions.
- Add a focused unit contract adjacent to `agent::env` and only the minimum
  `#[cfg(test)] pub(crate)` inspection seam needed to verify games retained by
  `RolloutPool`. Test-only inspection must not appear in the Rust public API,
  PyO3 bindings, or `.pyi` file.
- Add `managym/tests/content_pack_clone_allocations.rs` as the single-threaded
  counting-allocator gate. It prints one stable machine-readable record
  containing workload constants, pack definition counts/digests/serialized
  bytes, raw allocation counts/bytes, deltas, and threshold results. It records
  no elapsed time, throughput, RSS, step, or rollout metric.
- Check in `experiments/data/w2-208-content-pack-clone-allocations.json` with the
  exact release-test record and
  `experiments/w2-208-content-pack-clone-allocations.md` with the method,
  command, source revision, Rust toolchain, release profile, OS/architecture,
  CPU, memory, workload, raw counts/bytes, thresholds, and scope disclaimer.
  Record the committed measurement-code revision; if rebase rewrites that
  revision, rerun and refresh the receipt before landing.
- Leave `managym/examples/measure_content_pack.rs`, W2-179's evidence-boundary
  document, and W2-182's branching artifact semantically unchanged. A narrow
  cross-link from the W2-179 conclusion to W2-208 is optional only if it avoids
  restating any withdrawn number.

## Affected consumers and compatibility

- Rust: `Game::new`, `GameState::content`, `Game::clone`, `Env::reset`,
  `Env::fork`, `Env::flat_mc_scores`, `RolloutPool::from_game`, and the current
  full-clone branch driver are covered ownership consumers. Their public
  signatures and behavior remain compatible.
- Python/PyO3: `Env`, `VectorEnv`, observations, `fork`, `action_count`, and
  `flat_mc_scores` remain byte-for-byte API compatible; no new Python-visible
  field or method is introduced.
- Fixed-action, observation, replay, deterministic-hash, and content semantic-IR
  consumers continue using existing `CardDefId` values and content digests.
- CI/review receives two new focused Rust gates and a checked evidence receipt.
  The allocation receipt does not become the full search-branching regression
  gate owned by W2-182/W2-197.

## Absent and error states

- An `Env` before `reset` has no admitted match content. Existing `fork` and
  search errors remain authoritative; tests must not fabricate an identity for
  the absent state.
- Unknown deck content and duplicate registration keep their existing failure
  behavior. W2-208 does not add content admission or fallback resolution.
- Pointer mismatch, schema/digest mismatch, loss of card-definition sharing, or
  mutation leaking into a source/sibling fails the contract test immediately.
- The allocation control is invalid if the expanded pack has the same digest,
  does not exceed the 4 MiB serialized-payload delta, or if the mutable fixtures
  are not cloned from the same root with only `content` replaced.
- Zero mutable-clone allocations means the counter failed to observe the
  workload and is a test failure, not evidence of a free clone.
- Any nonzero baseline-versus-expanded allocation delta fails the gate. Missing
  source, hardware, toolchain, build-profile, workload, raw count, or threshold
  metadata makes the checked receipt incomplete and forbids a KR claim.
- A receipt whose recorded measurement-code revision was rewritten or whose
  raw record no longer matches a fresh run is stale. Rerun it; do not silently
  relabel it as current evidence.

## Operational boundary and verification

The allocation gate is local, deterministic, single-process, and explicitly
single-threaded. Use a fixed seed, fixture, expansion size, warmup count, and
clone count. Use allocator-requested bytes rather than RSS or timing. It must be
fast enough for a focused release-profile CI invocation and must not call the
network or spawn benchmark workers.

After Rust changes, rebuild the Python 3.12 extension exactly through the
uv-managed environment:

```bash
cd managym
uv run maturin build --release -i ../.venv/bin/python
```

Place the cp312 extension from the built wheel at
`managym/_managym.cpython-312-darwin.so`, then run proportionate compatibility
gates with every Python command through `uv`, including the focused engine and
flat-MC Python tests. Also run Rust formatting, clippy, focused tests, and the
full Rust suite before landing.

Once evidence and CI are clean, verify W2-196 is landed, run the loopflow rebase
immediately before landing, rerun any gate affected by the rebase, and use
`lf pr land -c`. Never use `lf pr submit`.

## Exclusions

- No undo journal, page-COW, persistent collection, mark/rollback
  implementation, or branching-representation selection.
- No duplicate of W2-197's search-state contract/equivalence proof or W2-207's
  content admission/version work.
- No before/after W2-179 claim and no general clone latency, step throughput,
  rollout throughput, or RSS claim.
- No content-pack hot reload, multi-version registry, serialization redesign,
  new card semantics, or Python/fixed-action ABI change.
- No second worktree, parallel Task, or additional PR unless the Task runner
  serially rotates this same Task after an independently reviewable first PR.

