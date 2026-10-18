# v0.1.2

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.1.2 makes failed launches easier to investigate, from preparation through startup, retries and cleanup. Operators can inspect retained evidence after a controller restart or machine deletion when it reached storage or the controller cache, and new RunPod rentals support SSH inspection. The through-line is keeping failures explainable without replaying work to discover what went wrong.

## Diagnose a launch across its full history

A failed launch can leave useful evidence even before a Run exists. Launch intent and diagnostics are retained independently, and log inspection includes earlier attempts rather than hiding them behind the latest output.

- Use `etude logs --run RUN`, `--machine MACHINE` or `--cohort COHORT` to inspect preparation diagnostics, startup output, attempts and cleanup evidence. `--job-only` narrows the output.
- Use `etude logs --run RUN --follow --timeout 60` to follow newly retained evidence without replaying earlier output, scheduling work or retrying it.
- Installer errors and worker console output are captured. Missing, silent, incomplete, truncated and unavailable output have distinct labels; retrieval failures preserve cached content.
- Failed admission preserves diagnostics without consuming the requested run ID. Unresolved machine acquisition still blocks resubmission, and cohort admission remains atomic.
- Streams stay open until their owned children exit and final console collection succeeds. Completion is read before output so a concurrent finish cannot permanently cache partial logs.

## Inspect a running RunPod rental

New Etude-created rentals request account-key injection, expose TCP port 22 and start key-only OpenSSH. This restores a direct inspection path while preserving the independent spending guardian's place before dependency installation.

- Find connection details in RunPod's Connect tab. Existing authorized keys are preserved.
- Missing account keys or OpenSSH produce a diagnostic and allow worker startup to continue.
- Existing pods are not modified.

## Operational notes

This patch changes the prototype database and log interfaces. Check these boundaries before upgrading an existing launcher installation.

- The database advances to schema 2 and refuses older files; there is no migration.
- CLI log selection uses explicit flags in place of positional selection. Python calls change from `logs(run)` returning text to `logs(run=run)` returning a structured `LogSnapshot`; `diagnostics()` reads retained database facts.
- Startup snapshots and controller caches retain at most 1 MiB per stream. Storage outages lasting through machine deletion, or death before logging starts, can still leave explicit evidence gaps.
- Log retrieval shares one timeout, including SSH controller installation.
- Recorded validation for the log-retention change included 279 passing tests with the outside network denied, six skipped and three deselected, plus passing mypy and Ruff checks. Linux platform confirmation, paid RunPod execution, a real SSH login and fresh-image performance were not verified in the supplied release evidence.

## Small changes

- Operational evidence is sanitized across read boundaries, including credential fragments split between reads.
- Startup logs publish through narrowly scoped write capabilities.