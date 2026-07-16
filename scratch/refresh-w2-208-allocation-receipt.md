# W2-211: Refresh the W2-208 allocation receipt

## User-visible outcome

Runtime State Foundation reviewers and machine readers can inspect the checked
W2-208 receipt and see a passing KR1 allocation measurement tied to the exact
canonical source revision that was measured after PR #81 landed. The JSON and
Markdown agree on the revision, UTC capture time, environment, workload, raw
counts, and zero-delta threshold. The recorded revision is an ancestor of the
final W2-211 merge.

This repairs evidence freshness only. It does not change runtime behavior or
expand the claim beyond ContentPack identity and clone-allocation ownership.

## Source of truth

`managym/tests/content_pack_clone_allocations.rs` is the executable measurement
contract. Its single-threaded release run produces the authoritative raw
workload and allocation counts.

`experiments/data/w2-208-content-pack-clone-allocations.json` is the
authoritative persisted receipt. Preserve schema
`manabot.content-pack-clone-allocations.v1` and all existing workload/result
fields, adding these additive provenance fields:

- `captured_at`: RFC 3339 UTC timestamp for the successful measurement run.
- `measurement_code_revision`: the exact canonical commit measured.
- `command`: the exact single-threaded release gate command.
- `environment`: Rust and Cargo versions, CPU model, architecture, logical CPU
  count, physical memory bytes, macOS version, and Darwin kernel version.

`experiments/w2-208-content-pack-clone-allocations.md` is the human-readable
projection of that JSON receipt. Its measurement record, method, workload,
table, threshold, and scope statements must match the JSON exactly. The
executable gate remains authoritative for current behavior; the JSON is the
checked record of one run.

## End-to-end proof

1. Use `lf rebase` to start from current canonical main containing PR #81 and
   capture that exact base revision from Loopflow's Task/PR state as `R`.
2. At `R`, run:

   ```bash
   cargo test --release --manifest-path managym/Cargo.toml --test content_pack_clone_allocations -- --nocapture --test-threads=1
   ```

   The only acceptable measurement is a successful test whose emitted JSON has
   `passed: true`, zero allocations/bytes for both Arc-retention workloads,
   positive mutable clone totals, and exact zero expanded-minus-baseline
   allocation and byte deltas for both mutable clone workloads.
3. Replace the checked JSON counts with the emitted counts and add the provenance
   for that same run. Refresh the Markdown as a projection of the JSON; do not
   carry forward old counts when the current run differs.
4. Verify the independent ContentPack contracts:

   ```bash
   cargo test --manifest-path managym/Cargo.toml --test content_pack_tests
   cargo test --manifest-path managym/Cargo.toml --lib agent::env::content_pack_contract_tests::content_pack_contract_covers_env_roots_siblings_and_rollout_slots -- --exact
   ```

5. Check that the JSON parses, both artifacts name `R`, their metadata and raw
   values agree, and the Markdown still states the KR1-only evidence boundary.
6. Rebase immediately before landing. If canonical main advanced so that `R`
   is no longer the measured current base, rerun steps 2-5 at the new revision
   and replace both artifacts again. Then use headless `lf pr land -c`; never
   use `lf pr submit`.

The observable finish condition is a landed W2-211 receipt whose recorded
measurement revision is in the final PR's ancestry, whose JSON and Markdown are
internally consistent, and whose allocation and identity contracts pass.

## Affected surfaces and consumers

- Machine-readable evidence consumers: the checked JSON gains additive
  provenance while retaining its v1 schema and existing raw-result keys.
- Human reviewers and Runtime Project/Wave evidence readers: the Markdown loses
  the stale pre-report revision and mirrors the fresh receipt.
- The Cargo allocation gate is rerun but not changed.
- `content_pack_tests` and the environment root/sibling/rollout-slot unit test
  remain unchanged compatibility checks for the identity claim.
- No application CLI, Python API, Rust public API, wire DTO, browser surface,
  model observation, or automation contract changes.

## Absent and error states

- A missing, malformed, truncated, or non-passing JSON line from the allocation
  gate is no evidence: do not refresh the artifacts or land.
- Any allocation-gate assertion failure, nonzero Arc allocation, non-positive
  mutable clone workload, nonzero expanded-minus-baseline clone allocation or
  byte delta, or insufficient expanded immutable payload fails the Task.
- Missing environment values must be represented explicitly as unavailable
  with a reason; they must not be silently omitted or copied from the old run.
- A failed focused identity test means the receipt cannot be landed even if the
  allocation gate passes.
- A JSON/Markdown mismatch, a receipt still naming
  `ad79e2e84ee3bb0651a0e5bd52a21e66b254a32b`, or a revision displaced by the
  final rebase makes the receipt stale and requires regeneration.
- Normal changes in raw counts on a new canonical revision are recorded as
  observed. Old values are not treated as expected constants; only the written
  pass/fail threshold is fixed.

## Operational boundary

- Run the allocation gate in Cargo `release` mode with exactly one Rust test
  thread. Do not parallelize it with another allocator-sensitive test.
- Preserve the executable workload: 4,096 retained Arc references, 64 warmup
  clones, 1,024 measured `GameState` clones, 1,024 measured exact `Game`
  clones, and at least 4 MiB of controlled immutable-definition growth.
- Capture metadata from the same machine and run as the raw counts.
- Use Loopflow for rebase, PR, and landing mechanics. Use `uv run` for any
  Python command, although none is required for this artifact-only repair.
- No Rust source change or extension rebuild is expected.

## Exclusions

- Runtime KR3 and any step throughput, clone latency, rollout throughput, peak
  RSS, worker/actor/rollout-load, or regression-gate claim.
- Undo, page-COW, fork representation, or branching-design evidence.
- Before/after comparison with W2-179 or a new interpretation of W2-208.
- Changes to ContentPack, MatchState, allocation-test behavior, identity tests,
  application behavior, Project KRs, or Wave memory.
