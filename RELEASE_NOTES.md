# v0.4.0

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.4.0 lets training save and resume as one run across attempts and machines, while recipes keep ownership of the learning loop. Reports take less repeated work and give checkpoint curves and evaluation summaries consistent placement and match selection. These notes cover the cycle since v0.3.0, including improvements already released in v0.3.1 and v0.3.2. ([#103](https://github.com/loopflowstudio/etude/pull/103), [#105](https://github.com/loopflowstudio/etude/pull/105), [#106](https://github.com/loopflowstudio/etude/pull/106), [#109](https://github.com/loopflowstudio/etude/pull/109))

## Carry training forward without splitting the run

Recipes previously had to manage saved state and coordinate recovery themselves. The training interface now restores registered state and carries progress across attempts and machines, keeping a run's rows and checkpoints together. Resume state is separate from checkpoints published for evaluation. ([#103](https://github.com/loopflowstudio/etude/pull/103))

- Recipes use `run.resume`, `run.steps` and `run.save` around their own learning loop. The Coin and Hanabi examples demonstrate training and evaluation. ([#103](https://github.com/loopflowstudio/etude/pull/103))
- `etude train FILE` runs locally. `--resume` continues with stored config and current code; `--resume --until` extends the target. Selections are checked before any recipe executes, so a refusal leaves every selected run untouched. ([#103](https://github.com/loopflowstudio/etude/pull/103))
- Launched runs retry planned stops within cumulative hours, dollars and attempt limits. Recovery restores delivered `out/`, removes stale output on reused hosts and avoids uploading restored bytes again. Immutable save folders are delivered before the pointer that selects them, so recovery selects a whole save. ([#103](https://github.com/loopflowstudio/etude/pull/103))
- Jobs declare seeds and receive compact `EXPERIMENT[/JOB][@SEED]` addresses. Evaluation accepts verified folder checkpoints and typed match configs. ([#103](https://github.com/loopflowstudio/etude/pull/103))

## Build reports with less repeated work

Report generation now stages rows into DuckDB in bulk and reads stored evaluation runs once per page build. On the measured TLA frontier report, cold-build CPU time fell from 14.4 to 3.9 seconds and warm-build CPU time from 6.6 to 2.2 seconds; the page was byte-identical apart from its generated time. These measurements describe that report, not a timing guarantee for every experiment. ([#106](https://github.com/loopflowstudio/etude/pull/106))

- Bulk staging also speeds writing match rows while playing. JSON lines preserve nulls, empty strings and nested values while avoiding repeated optional-pandas import searches. A counting test keeps those searches flat as match counts grow. ([#106](https://github.com/loopflowstudio/etude/pull/106))
- Tournaments share the page build's stored-run reads. `etude eval` still reads per tournament because an earlier pass can deliver matches a later tournament shares. ([#106](https://github.com/loopflowstudio/etude/pull/106))

## Read the same result in every view

Checkpoint placement and match selection now use shared rules. The cycle also restores a way to draw pairs of fixed checkpoints once, after v0.3.0 removed `Curve(subject=)`. ([#105](https://github.com/loopflowstudio/etude/pull/105), [#109](https://github.com/loopflowstudio/etude/pull/109))

- A continued run's checkpoints seated without an explicit line join the learner's line—the first run in the continuation chain. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- A seating of two fixed models is drawn at its first seat's checkpoint, so a curve over pairs of jobs' checkpoints shows each pair once. ([#109](https://github.com/loopflowstudio/etude/pull/109))
- Pages, `scores` and `standings` select the same match record, including ties. The rule that a completed record replaces a failed one has one owner. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- Equivalent stopping values such as `steps=26000.0` and `steps=26000` identify one job. Smoke runs and the scheduler share the same job identity calculation; stored job IDs change across this cycle. ([#105](https://github.com/loopflowstudio/etude/pull/105))

## Operational notes

The launcher database reaches schema 19 without migration. Each schema now has its own `launcher-<schema>.db` file, so upgrading does not require moving aside the database an older client needs to track live rentals. This supersedes v0.3.1's move-aside instruction; retain the matching Etude version and database for older launcher evidence. ([#103](https://github.com/loopflowstudio/etude/pull/103), [#110](https://github.com/loopflowstudio/etude/pull/110))

- **Recovery depends on delivery.** Only bytes delivered before a machine disappears can be restored. With upload-only bucket grants, retired-save cleanup waits for the controller. ([#103](https://github.com/loopflowstudio/etude/pull/103))
- **Local training stays outside the launcher database.** Read local runs with `etude report` and `etude checkpoints`. `etude continue` branches launched runs only. Existing stored runs remain readable, but `run.resume` does not import state files written by older custom loops. ([#103](https://github.com/loopflowstudio/etude/pull/103))
- **Update Python names when upgrading from v0.3.0.** Checkpoint positions use `at`, including `Checkpoint.at`, `ScorePoint.at` and `RunSummary.at`; a single clock's coordinate uses `value`, including `Until.value` and `Gap.value`. Stopping conditions use `Until(clock, value)`. `report.Schedule` becomes `Planned`, train report `Job` becomes `JobGroup`, and `train.block.Table` becomes `Columns`, exposed as `RunData.columns`. The former `Mismatch` names become `ContentMismatch` in cache, `RecordMismatch` in carriage and `StoredContentMismatch` in download. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- **Shares use named records.** Use `hold(share=Share(index, count))`. Tournament files write `{index, count}` and still read old `[k, n]` pairs. Rollup `opponents` is a list column; older blocks still read. The cycle advances statsdb to schema 13. ([#105](https://github.com/loopflowstudio/etude/pull/105))

## Small changes

- Job output containing ` diagnostic: ` is no longer mistaken for a diagnostic. ([#105](https://github.com/loopflowstudio/etude/pull/105))
- Corrected the v0.3.0 notes: Typer was held below 0.28, and the operational notes now include the earlier breaking `Matchup` → `Seating` rename, changes to Curve's `subject`/`opponent`/`inference`, and `HeadToHead`. These are documentation corrections for v0.3.0 behavior. ([#108](https://github.com/loopflowstudio/etude/pull/108))