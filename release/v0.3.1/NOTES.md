# v0.3.1

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.3.1 makes reports cheaper to build and more consistent with evaluation queries. Continued checkpoints stay on their learner's curve, and pages and command-line summaries use the same match record when results tie. The release also standardizes Python API names and job identity; review the database and API changes before upgrading. ([#105](https://github.com/loopflowstudio/etude/pull/105), [#106](https://github.com/loopflowstudio/etude/pull/106))

## Build reports with less repeated work

Report generation now loads rows into DuckDB in bulk and reads stored evaluation runs once per page build. On the measured TLA frontier report, cold-build CPU time fell from 14.4 to 3.9 seconds and warm-build CPU time from 6.6 to 2.2 seconds; the page was byte-identical apart from its generated time. These are results for that report, not a timing guarantee for every experiment. ([#106](https://github.com/loopflowstudio/etude/pull/106))

- Match rows also use bulk staging while playing, avoiding repeated optional-pandas import searches for individual values. JSON lines preserve nulls, empty strings and nested values. A counting test keeps those searches flat as match counts grow. ([#106](https://github.com/loopflowstudio/etude/pull/106))
- Multiple tournaments share the page build's stored-run reads. `etude eval` still reads per tournament, because an earlier pass can deliver matches that a later tournament shares. ([#106](https://github.com/loopflowstudio/etude/pull/106))

## Keep curves and summaries in agreement

Checkpoint placement and match selection now have shared rules, so different views of the same experiment agree. Job identity likewise uses one definition across smoke runs and scheduling. ([#105](https://github.com/loopflowstudio/etude/pull/105))

- A continued run's checkpoints seated without an explicit line join the learner's line—the first run in the continuation chain—instead of a separate line named after the continuation. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- Pages, `scores` and `standings` select the same match record through statsdb's `matches.newest`, including ties. The rule that a completed record replaces a failed one has one owner. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- Equivalent stopping values such as `steps=26000.0` and `steps=26000` identify one job. Smoke runs and the scheduler share the same `job_id` calculation. Stored job IDs change with this release. ([#105](https://github.com/loopflowstudio/etude/pull/105))

## Operational notes

The API cleanup includes breaking names and database schemas. Existing tournament shares and older rollup blocks retain read compatibility, but launcher databases have no migration path. ([#105](https://github.com/loopflowstudio/etude/pull/105))

- **Launcher database schema is now 17; statsdb schema is 13.** Move an older launcher database aside before starting with this version; do not expect an in-place migration. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- **Use `at` for checkpoint positions.** This includes `Checkpoint.at`, `ScorePoint.at`, `RunSummary.at`, statsdb's `training_checkpoints.at` and `players.seated_at`. A single clock's coordinate is `value`: `Run.at(clock)`, `Until.value` and `Gap.value`. Stopping conditions use the typed `Until(clock, value)`. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- **Update renamed Python types.** `report.Schedule` becomes `Planned`, train report `Job` becomes `JobGroup`, and `train.block.Table` becomes `Columns`, exposed as `RunData.columns`. The former `Mismatch` names become `ContentMismatch` in cache, `RecordMismatch` in carriage and `StoredContentMismatch` in download. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- **Pass shares as named records.** Use `hold(share=Share(index, count))`. `tournament.json` writes shares as `{index, count}` and still reads old `[k, n]` pairs. Rollup `opponents` is now a list column; older blocks still read. ([#105](https://github.com/loopflowstudio/etude/pull/105))

## Small changes

- Job output containing ` diagnostic: ` is no longer mistaken for an Etude diagnostic. ([#105](https://github.com/loopflowstudio/etude/pull/105))