# v0.2.10

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.10 makes large evaluations cheap to store and quick to query, and makes the launcher's read commands work offline. An eval run is now sealed into a few hundred Parquet blocks, not hundreds of thousands of per-match files. Frequent summaries are added up as matches are stored, and one bootstrap covers every rate on a page. `etude status`, `etude logs` and `etude usage` now answer from the database with no provider key and no network. Upgrade if your evaluations have outgrown a file per match, or if you check a fleet from a machine without RunPod credentials. Some launcher scripts need small edits; see the operational notes.

## Eval runs are stored as sealed blocks

Before this release, a seed meant delivering hundreds of thousands of objects, and a query such as manabot's card page re-scanned millions of raw rows. `evaluate` now stages each stored match on disk and seals what it has staged into blocks that plain DuckDB can read. Frequent summaries are precomputed as rollups, so a query reads small totals instead. ([#63](https://github.com/loopflowstudio/etude/pull/63))

- **Block layout.** A block in `RUN/blocks/WRITTEN.WRITER/` holds `blobs.zip`, one `TABLE.parquet` per table, one `ROLLUP.parquet` per rollup and then `block.json` as its index. A writer seals every five minutes, once 256 MiB of blobs build up, after a failure and when the run finishes. Tables are sorted by match and game in row groups of 8192 rows, so a read filtered to a few matches skips most of a file. `read_parquet('RUN/blocks/*/plays.parquet')` works without Etude.
- **Safe delivery.** Carriage sends a block's data before its index, so a reader that finds the index can read the whole block. A block is never sent twice. Tournament planning reads block indexes to find matches that were already delivered.
- **Crashes and SIGTERM.** Staged matches from a writer that died before sealing stay on disk, and the next `evaluate` in that folder seals them. SIGTERM seals before exit.
- **Blobs.** A match can return `Result(tables=..., blobs=[...])`. `read_blobs(db, store, digests)` returns verified blobs. It fetches neighbouring blobs in one range request and keeps them locally, so a repeat request reads nothing from the store. `statsdb` gains a `blobs` table.
- **Rollups.** A `Rollup` names an author table, optional tables to join, group-by columns (including the reserved `player` and `opponents`), DuckDB sum expressions and a `where` filter. It is summed for each match when the match is stored, so a match the rollup cannot sum fails then, not at seal. `statsdb` adds each new block's partial totals to those it already holds, and a match held by two blocks counts once, from the later block. A run keeps its first block's rollups. Resuming with different rollups raises `eval_run_rollups_conflict`.
- **Rollups defined at query time.** `statsdb(..., rollups=[...])` sums each block that lacks the rollup once and keeps those totals on this computer only, never in the store. Later opens sum only new blocks. Reusing a name with a different definition raises `rollup_conflict`.
- **Faster ingest.** `statsdb` ingest and match storage now take a fixed number of DuckDB statements per block, not one per row. Before this, MTG-183's 22 converted blocks did not finish ingesting in 12 minutes.

## One bootstrap for many rates

A report that shows dozens of rates over the same deals now needs one bootstrap, not one per rate. Every rate on the page is resampled over the same draws, and a rate's interval doesn't change when columns are added or removed. Together with cheaper replicate draws, this removes most of the time MTG-183's card page spent redrawing the same weights; that page previously took 27 minutes. ([#63](https://github.com/loopflowstudio/etude/pull/63), [#68](https://github.com/loopflowstudio/etude/pull/68))

- `etude.eval.query.estimate(keys, numerators, denominators)` takes one key per block (a match or a deal) and blocks × columns arrays. It returns one `Estimate(value, interval)` per column, or `None` for a column with no opportunities. Each column gets the interval it would get if it were estimated alone.
- `estimate` raises `ValueError` when keys repeat, when the number of keys differs from the number of blocks, or when the two arrays have different shapes.
- `compare` keys its blocks by `(run, match)` and tournament curves by `match`.
- `docs/evaluation.md` has an example that estimates per-card rates over deals.

## Launcher reads work without a provider key

AGENTS.md says a read must work with no provider key and no network. `status` and `logs` were the main exceptions. Now every read answers from the database and says how fresh its answer is. Only waits, such as `etude logs --follow` and `etude wait`, contact other machines. ([#67](https://github.com/loopflowstudio/etude/pull/67))

- **`etude status`** shows runs as of the controller loop's last pass. It prints that pass's time as `last loop: ...`, along with any controller warning. It no longer advances work. At 10 workers it now runs 381 SQL statements and makes no other calls, where it previously made 20 provider calls, 10 journal calls and 653 statements.
- **`etude logs`** shows the evidence already retained. If that evidence is incomplete, it prints `retained evidence only; --follow collects new evidence from machines`. `--follow` collects new evidence, and `--timeout` is accepted only with `--follow`. In Python, `logs()` and `Scheduler.logs()` take `refresh=True` to collect first. Passing a `timeout` without `refresh` raises `ValueError`.
- **`etude usage`** and `Scheduler.rentals()` report cost from retained bills and no longer call `tick()`.
- **Positional inputs.** The one required input is now positional: `etude rent R`, `etude terminate MACHINE` and `etude availability R`.
- A counting test runs every read command with `RUNPOD_API_KEY` unset and pins zero provider, bucket, SSH and git calls. It also fails if a new read command is left out of the test.

## Operational notes

- The release lists no compatibility break for stored data.
- **Converting older eval runs.** Runs recorded before this release keep their per-file records. `etude convert-eval RUN --source URI` writes blocks beside them without replaying games. It converts only what no block holds yet, so it is safe to run again. `etude.eval.convert.convert(..., rollups=[...])` also sums rollups into the new blocks. After conversion, `etude delete-eval-files RUN --source URI` deletes only the files that delivered blocks replace. It deletes nothing while any old file in that store is still unconverted.
- **Launcher script changes.** Remove `--ondemand`, `--machine` and `--rent` from `rent`, `terminate` and `availability`, and pass the value positionally. Add `--follow` wherever `etude logs` is given `--timeout`, or that call now errors.
- **Nothing advances without a controller.** `status` no longer moves work along. If no controller loop is running, runs stay queued and `last loop:` keeps showing an old time. Run `etude wait` or `etude controller run`.
- **Plain `logs` may lag.** While a job runs, plain `logs` shows output up to the last collection. A rented machine's final output is retained when its session ends.
- **Interval shift.** Bootstrap blocks are now ordered by key, not by value. Intervals from `compare` and tournament curves therefore differ slightly from those in v0.2.9 and earlier. They come from the same distribution but use different draws. Point values are unchanged.

## Small changes

- Dense report curves (more than five points) now mark isolated points, which have no line segment beside them. A checkpoint whose neighbours were not played was invisible before. Sparse lines and `dots` charts are unchanged. ([#66](https://github.com/loopflowstudio/etude/pull/66))
- `docs/launcher.md` is updated: the command table, the logs section, and the command-kinds table, which now lists `logs` and `usage` as reads and `logs --follow` as a wait.
- `docs/evaluation.md` and `docs/tracking.md` are updated for block storage.