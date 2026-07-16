# Replace benchmark snapshot god-object with typed witnesses

## Problem

`CanonicalSnapshotV2` is not a restorable snapshot. It aggregates exact
authority identity, legal-decision identity, viewer projections, deterministic
continuation probes, and redundant diagnostics into one public equality type.
The version suffix leaks benchmark migration history into every driver, while
the benchmark contract, persisted result schema, and documentation have their
own independent versions.

The shape also hides three distinct equivalence relations:

1. exact authority equality, including `decision_epoch`;
2. semantic ABI equality, which intentionally ignores prompt-publication
   epochs; and
3. fixed-viewer projection equality and privacy.

W2-198 and W2-199 should not copy this accidental boundary into optimized
drivers.

## Design

- Rename the aggregate evidence type to `SearchStateWitness` and the driver
  operation to `witness`.
- Introduce typed components:
  - `AuthorityFingerprint`: canonical complete-`Game` bytes and digest;
  - `LegalSurfaceFingerprint`: current action bytes, digest, and count;
  - `ViewerProjectionWitness`: exact bytes and digest for one fixed viewer;
  - `ContractDiagnostics`: event-boundary counts, RNG continuation, and
    terminal status.
- Keep the witness schema version as data/manifest metadata, not a Rust type
  suffix.
- Keep canonical authority serialization exhaustive and deterministic. Do not
  weaken the fork/rollback contract or privacy checks.
- Keep benchmark result/manifest wire schemas stable unless the renamed
  witness field changes a persisted payload.
- Retain `Env::state_digest` as the deliberately weaker semantic ABI digest;
  do not conflate it with exact authority identity.

## Done when

- No `CanonicalSnapshotV1`, `CanonicalSnapshotV2`, or `EquivalenceSnapshot`
  Rust type remains.
- `BranchDriver` exposes `witness`, while `mark` remains the restoration API.
- Exact branch, nested rollback, deterministic replay, fixed-viewer privacy,
  structured/legacy differential, and benchmark tests pass unchanged in
  meaning.
- Documentation names the witness and independently states its schema version.
