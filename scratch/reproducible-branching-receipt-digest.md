# Reproducible branching receipt source provenance

## Problem

The search-branching receipts claim to bind measured evidence to source through
`run.source_sha256`, but the current implementation hashes an incidental
checkout rather than source. At merge `09ac3405`, the canonical checkout and
the W2-198 worktree had identical clean Git trees while producing different
digests: the canonical checkout walked 197,361 files beneath nested
`.claude/worktrees`, and the linked worktree hashed its path-bearing `.git`
file. Both checked-in receipts therefore fail `verify` anywhere except their
now-deleted generating worktree.

This blocks the Search Project KR requiring “one reproducible harness” even
though the full-clone and clone-plus-undo measurements themselves are present.
Reviewers, CI, and future candidate implementers need receipts that verify from
any checkout containing the same contract-relevant tracked source. W2-264
repairs that provenance boundary; it does not complete Search KR2 because the
page-COW candidate and the representation decision remain owned by W2-199.

## The demo

At the landed commit, run both verification commands from the canonical main
checkout and from a second checkout of that exact commit:

```bash
uv run scripts/bench_branching.py verify
uv run scripts/bench_branching.py verify --driver compact_clone_undo/current_game_v1
```

All four invocations print `"status": "verified"`, and both artifacts expose
the same `run.source_sha256` in both checkout locations despite canonical
main's nested `.claude` worktrees and the linked checkout's `.git` file.

## Approach

Replace the filesystem walk with a versioned, self-describing Git-tree digest
over an explicit source closure.

Define an exact sorted `COMPILE_TIME_SOURCE_INPUTS` tuple containing the three
tracked files embedded by the production Rust crate:

- `content/semantic/v1/coverage.evidence.json`
- `content/semantic/v1/generated/two_deck.ir.json`
- `content/semantic/v1/two_deck.source.json`

`managym/src/semantic/mod.rs` embeds `two_deck.ir.json` as the canonical typed
IR consumed by the default content pack. `managym/src/conformance.rs` embeds
`two_deck.source.json` and `coverage.evidence.json` to bind the admitted matchup
and checked coverage evidence. A byte change in any of these files can change
the compiled crate without changing a file below `managym/src`, so all three
are first-class source inputs rather than generated evidence exclusions.

Define one sorted `SOURCE_PATHS` tuple containing
`COMPILE_TIME_SOURCE_INPUTS` plus:

- `docs/benchmarks/search-branching-contract-v1.md`
- `managym/Cargo.lock`
- `managym/Cargo.toml`
- `managym/src`
- `managym/tests`
- `pyproject.toml`
- `scripts/bench_branching.py`
- `tests/bench/test_branching_benchmark.py`
- `uv.lock`

The Rust source, its three external compile-time inputs, manifests, and lock
file determine the native benchmark. The Python harness and its root dependency
lock determine orchestration, process sampling, and verification. The Rust and
Python tests encode the executable contract. The contract document defines the
admitted workload. Other files under `content/semantic/v1` are not embedded by
the production crate and remain outside this measurement-source closure;
generated raw benchmark artifacts and reports are also outside it. This keeps
the closure exact while eliminating receipt self-reference and allowing both
driver receipts to bind to one source state.

Before hashing, run a path-scoped, NUL-delimited porcelain status check against
`SOURCE_PATHS`, including tracked, untracked, and ignored entries. Fail closed
with the offending paths if any allowed input differs from `HEAD`. Perform the
same check when `source_sha256()` runs after the benchmark so a build or an
editor cannot alter an admitted input during measurement.

For a clean closure, execute the equivalent of:

```text
git ls-tree -r --full-tree -z HEAD -- <SOURCE_PATHS...>
```

SHA-256 the command's raw bytes. The records bind each selected path, object
mode, object type, and content-addressed object name. They do not contain the
checkout path, `.git` representation, untracked agent worktrees, timestamps,
or unrelated tracked files. `--full-tree` makes paths repository-root-relative
and `-z` avoids quoting/configuration differences.

Keep `run.source_sha256`, schema `manabot.search-branching.result.v1`, and
contract ID `manabot.search-branching.v1`. Add and verify
`run.source_digest_method = "git-ls-tree-sha256-v1"` and the exact
`run.source_paths` list so the receipt describes what its digest means without
requiring historical source inspection. Update generated report prose to name
the Git-tree method, explicit closure, dirty-input behavior, and the rule that
only changes inside the closure invalidate a receipt.

Receipt regeneration also needs truthful run provenance. The current harness
hard-codes W2-198's task session, branch, and deleted worktree. Resolve the
ambient task from `LF_TASK_SESSION_ID` plus `lf status --json`, record the live
task/branch/worktree when available, and retain an explicit unavailable reason
when Loopflow context cannot be read. This is metadata-only and does not touch
fixtures, seeds, workload dimensions, or native execution.

Implement focused tests around a temporary Git repository and linked
worktree:

1. A normal checkout and linked worktree at one commit produce the same digest
   even when the normal checkout contains `.claude/worktrees/**` noise.
2. An unrelated tracked commit outside `SOURCE_PATHS` leaves the digest
   unchanged.
3. A parameterized external-input mutation test covers each exact member of
   `COMPILE_TIME_SOURCE_INPUTS`: a one-byte dirty mutation must fail the clean
   gate, and committing that mutation must produce a digest different from the
   baseline.
4. Any other committed content, path, or mode change inside `SOURCE_PATHS`
   changes the digest.
5. A staged, unstaged, untracked, or ignored change inside `SOURCE_PATHS`
   fails before hashing; unrelated dirt remains permitted.
6. Verification rejects the wrong method/path list and report rendering stays
   exact.
7. A closure-completeness test scans every tracked `managym/src/**/*.rs` for
   `include!`, `include_str!`, and `include_bytes!`. It resolves literal targets
   relative to the invoking Rust file and asserts that the discovered external
   target set equals `COMPILE_TIME_SOURCE_INPUTS`. Any occurrence with a
   non-literal/unresolvable argument fails rather than being skipped. Therefore
   adding or retargeting a compile-time include cannot pass CI until the exact
   tracked input is admitted to `SOURCE_PATHS` and both receipts are
   regenerated. The same test resolves Cargo's explicit `package.build` path
   or default `managym/build.rs` and requires any present build script to be an
   admitted source path, preventing a new compile-time root from bypassing the
   include scan.

Land the implementation and regenerated evidence in two logical commits. The
first commit contains the harness, tests, and report-template change. Run both
full canonical benchmarks from that clean `HEAD`; the resulting artifact and
report edits are outside `SOURCE_PATHS`, so the second driver sees the same
digest. The second commit records both regenerated artifact pairs. A squash or
rebase that preserves selected tree entries preserves the digest; a final
rebase that changes any selected entry requires regenerating both pairs.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Can Git provide a checkout-independent encoding of selected source? | Yes. [`git ls-tree`](https://git-scm.com/docs/git-ls-tree.html) emits tree entries containing mode, type, object name, and path; `--full-tree` makes paths root-relative and `-z` emits verbatim NUL-terminated names. At `09ac3405`, hashing the complete selected records from `/Users/jack/src/manabot` and this linked worktree produced the identical experimental digest `a87f731b38a1bb3f8b465b86057d85f616be14a638a3efd3d10ed8d6e1bb443b`. | Hash raw selected `ls-tree` records, not filesystem bytes or checkout metadata. |
| Does a Git object name actually bind content? | Yes. Git is a content-addressable store; blobs identify file content and trees associate object names with modes and paths ([Git Objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)). | The outer SHA-256 can bind the canonical selected tree-entry stream without reading incidental worktree files. |
| Would hashing `HEAD` hide local code used by a run? | Yes, unless dirt is rejected. A modified working file can affect the compiled binary while `ls-tree HEAD` remains unchanged. Git's porcelain status is stable for scripts and reports staged, unstaged, and untracked differences ([git-status](https://git-scm.com/docs/git-status)). | Path-scoped clean checks are mandatory before and after measurement, not optional diagnostics. |
| Is the suggested source list closed over dependencies? | Not quite. The native binary also depends on `managym/Cargo.toml`; Python orchestration and RSS sampling depend on root `pyproject.toml` and `uv.lock` (currently locking `psutil` 7.2.2). More importantly, the production Rust source has exactly three compile-time include sites: `semantic/mod.rs` embeds `two_deck.ir.json`, while `conformance.rs` embeds `two_deck.source.json` and `coverage.evidence.json`. No tracked `managym/build.rs` exists at the reviewed tree. | Include both language manifests/locks and the three exact embedded content files, while continuing to record actual tool versions and hardware in each artifact. |
| How does a future external compile-time input avoid drifting silently? | A directory allowlist alone cannot prove closure completeness. A new `include_str!`, `include_bytes!`, or `include!` could point outside `managym/src` without changing an already admitted external file. | Make source scanning a blocking test: every production include occurrence must resolve, and its external target set must equal `COMPILE_TIME_SOURCE_INPUTS`. The Cargo manifest is itself admitted; adding a build script must likewise update the closure before receipts can be regenerated. |
| Can the whole commit SHA be used instead? | No. The receipt is committed after measurement, so a whole-commit or whole-tree ID would change when the receipt containing that ID is committed. Unrelated docs would also invalidate it. | Hash only contract-relevant paths and exclude generated evidence by construction. |
| Can receipts be honestly rerun without another provenance fix? | No. `run_benchmark()` hard-codes W2-198's task, session, branch, and worktree. A W2-264 rerun would claim it ran in a deleted worktree despite a different `run.cwd`. | Resolve current Loopflow metadata dynamically as part of this provenance-only change. |
| Does regeneration imply changing the benchmark decision? | No. The current full-clone and clone-plus-undo manifests are byte-identical after removing the driver ID, their per-cell deterministic checksums match, and each full run takes about 87 seconds on the recorded host. | Rerun both canonical profiles solely to issue truthful matched receipts. Do not interpret timing drift, select a representation, or add a candidate. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Add `.claude` to exclusions and reject `.git` as either file or directory | Smallest patch; would fix the two observed path leaks. | The exclusion list remains permanently incomplete, untracked/ignored files still affect identity, and unrelated repository work still invalidates every receipt. It repairs examples rather than the provenance model. |
| Walk an explicit filesystem allowlist and hash bytes | Cross-checkout paths disappear and local edits are naturally included. | Checkout filters, line-ending conversion, file metadata edge cases, and untracked inputs remain part of an ad hoc source model. It also lacks Git's canonical path/mode/object encoding. |
| Hash the whole commit or root tree | Extremely simple and reproducible before evidence is committed. | Generated artifacts make the landing tree different from the measured commit, creating a circular/stale receipt; unrelated docs and wave state would continue to force expensive reruns. |
| Hash selected Git tree entries with a clean-source gate | Canonical across clones and worktrees, sensitive only to admitted source, and fail-closed for local divergence. | Chosen. The explicit closure must be reviewed when a new build or harness input is introduced. |

## Key decisions

- “Source” means selected tracked content, paths, and executable modes at
  `HEAD`, not everything present below the checkout directory.
- The digest closure includes dependency resolution and verifier tests, not
  only files compiled into the release binary. A dependency or acceptance
  change is a source change for evidence purposes.
- The three production external compile-time inputs are admitted individually,
  not by hashing all of `content/semantic/v1`. This covers bytes compiled into
  the crate without making unrelated generated semantic reports part of the
  branching measurement identity.
- Closure completeness is executable: the production Rust include scanner's
  discovered external paths must exactly equal `COMPILE_TIME_SOURCE_INPUTS`.
  It is not a comment that can silently become stale.
- Dirty admitted inputs invalidate both `run` and canonical `verify`; dirt
  outside the closure does not. This keeps verification honest without making
  agent worktrees or unrelated wave docs part of benchmark identity.
- The method and path list are receipt data checked by the verifier. Future
  closure changes are explicit provenance changes and require regenerating all
  candidate receipts.
- Both existing drivers are rerun from the same clean selected tree. Timing and
  RSS values may naturally move, but workload definitions, fixtures, seeds,
  contract ID, and deterministic outcomes may not.
- This repair makes W2-199's future page-COW evidence use the same provenance
  mechanism but does not implement, measure, or select page-COW here.

### Success pressure test

Six months from now, a reviewer can clone the landing commit on a clean machine,
verify the checked-in receipts immediately, and see exactly which source paths
the digest covers. A documentation-only rebase or background agent worktree no
longer burns two benchmark reruns, while changing the engine, harness,
dependencies, tests, or contract fails deterministically.

### Failure pressure test

The repair fails if the allowlist omits a real build/runtime input, if `HEAD` is
hashed while dirty admitted files are executed, or if evidence is generated
before the provenance implementation is committed. The exact external-input
list, production include scanner, parameterized one-byte mutation test,
pre/post clean gates, temporary-worktree tests, self-describing receipt fields,
and two-commit regeneration sequence directly guard those failure modes.

## Scope

- In scope: Git-tree source digesting, exact external compile-time content
  inputs, an executable closure-completeness guard, admitted-source clean
  checks, self-describing receipt provenance, truthful ambient Loopflow
  metadata, targeted harness tests, report wording, and regeneration of both
  existing full canonical artifact/report pairs.
- Out of scope: Rust engine or driver changes; fixture, seed, workload,
  sampling, profile, threshold, schema ID, or contract ID changes; page-COW;
  cross-candidate decision analysis; selecting or rejecting a branching
  representation.

## Done when

1. The focused Python tests pass:

   ```bash
   uv run pytest tests/bench/test_branching_benchmark.py
   ```

2. The assertion-backed search-state contract passes in debug:

   ```bash
   cargo test --manifest-path managym/Cargo.toml --test search_state_contract
   ```

3. Both canonical artifacts are regenerated from one clean selected tree, and
   their manifests match after removing only `manifest.driver`; their ordered
   deterministic per-cell checksums remain equal.
4. Both `verify` commands pass before submission, after the final rebase, from
   the task worktree, and at the landing commit from canonical main plus a
   second checkout of that same commit.
5. The two artifacts carry one identical `source_sha256`, exact
   `source_digest_method`/`source_paths`, unchanged contract ID, fixtures,
   seeds, workload cells, and profile semantics.
6. Canonical main may contain `.claude/worktrees/**`, and the second checkout
   may use a regular-file `.git`; neither affects the digest or verification.
7. The closure scanner finds exactly the three declared production external
   compile-time inputs, and the parameterized mutation test proves each one
   independently invalidates or changes the digest.

This restores the reproducibility clause of Search KR2 for the two landed
candidates. It does not mark KR2 complete until W2-199 supplies page-COW
evidence and the decision record.

## Measure

Baseline at `09ac3405`:

- both checked-in `verify` commands fail with `source digest mismatch`;
- identical clean Git trees yielded filesystem digests `549f6738...` and
  `ba778d31...`;
- the filesystem walk admitted 197,361 versus 510 files by checkout;
- complete selected Git-tree records, including all three embedded content
  files, already produced one experimental digest `a87f731b...` from both
  repository locations.

Target at landing:

- 4/4 verification invocations pass across two checkout locations;
- 1 source digest across two artifacts and two checkouts;
- 0 changes to contract ID, fixtures, seeds, workload dimensions, canonical
  profile rules, or deterministic per-cell outcomes;
- unrelated tracked files, `.claude/**`, and `.git` representation cause 0
  digest changes, while every admitted source mutation changes the digest or
  fails the clean-source gate;
- 3/3 production external compile-time input mutations are detected, and any
  future unadmitted production include target fails the closure scanner.

The regenerated throughput and RSS values remain evidence for their respective
drivers, but W2-264 makes no before/after performance claim and no branching
representation decision.
