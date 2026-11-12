# v0.2.16

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.16 is about long runs that behave well when storage is slow or history is large. When a bucket stops answering, storage requests now time out and retry instead of hanging, so a rented worker or `run.finish()` no longer waits forever while the rental runs down. Training code can log per-matchup result tables as rows instead of thousands of metric names, and index refreshes no longer reread a long run's history. Report pages compare exactly the matches each chart selects, and served reports refresh only when you ask. One thing to know before upgrading: the launcher database moves to version 14 and older databases are refused, not upgraded.

## Storage outages no longer hang workers or the SDK

Before this release, a storage request on a dead connection had no time limit. The worker's courier, the SDK's background uploader or `run.finish()` could wait forever, and nothing said why. Every storage path now follows one timeout policy, reports stalls, and keeps local files until delivery succeeds. ([#92](https://github.com/loopflowstudio/etude/pull/92))

- **One socket timeout everywhere.** The launcher transport, bootstrap log publication and the SDK's `S3Storage` use the standard-library module `etude.storage_timeout`. Each socket operation gets 60 s by default. Whole-body urllib uploads get extra time based on their size at 1 MiB/s, so a 64 MiB part gets 124 s. You can override these with `ETUDE_STORAGE_TIMEOUT_SECONDS` and `ETUDE_STORAGE_BYTES_PER_SECOND`. A download that keeps receiving bytes has no short total limit, but any enclosing operation deadline still caps it. A request that times out raises `StorageUnavailable`.
- **Your settings reach the worker.** If you set either limit on the controller, it is passed to rentals through the bootstrap environment and to SSH workers through `RemoteRequest.storage_environment`. Setting these as job variables alone does not configure the parent uploader.
- **The courier says when delivery is stuck.** Each delivery step is bounded by the rental deadline. A failed pass logs `output delivery has not succeeded for N minutes … local files retained`. N counts from the last pass that delivered everything, not from the first failure. The courier logs again when delivery recovers.
- **`finish(timeout=…)` keeps retrying.** Final SDK delivery now retries until the finish allowance runs out. Before, it made one last attempt. S3 connection and HTTP-client errors become `StorageUnavailable` and log a warning. Streamed GET bodies are retried up to three times.
- **`etude status` shows how long a run has gone without a confirmed delivery.** For example: `no delivery confirmed for N minutes (retained evidence; collection may be stale)`. The age counts from the latest retained successful PUT/POST for the current attempt, or from allocation if nothing has been delivered yet. It is read from the database without network access (`Status.delivery_age_seconds`, `Database.last_delivery`). Completed and local-only attempts show no age.
- **Docs.** `docs/launcher.md` covers the timeout settings and what delivery age does and does not prove. `docs/tracking.md` has a new *Storage outages and recovery* section, which starts with `etude sync --directory … --to s3://…`.

## Log result tables as rows, and keep long runs cheap

Training code that logged per-matchup results (deck × opponent × seat) had to give each combination its own metric name. Long runs could end up with thousands of names, and every index refresh re-described every permanent block. ([#91](https://github.com/loopflowstudio/etude/pull/91))

- **`run.log_table(name, rows, **clocks)`** records a collection of JSON rows at one set of clock coordinates. The rows are stored once as an immutable, content-addressed `artifacts/DIGEST.json`, and the metric row holds only the digest, so identical tables share one file. Bad rows, bad clocks, local write failures, or a damaged existing file with the same digest all raise an error. When the queue overflows, the reference is dropped and counted in `run.dropped`, the same as for `log`.
- **`RunData.read_table(name, clock)`** returns `TablePoint` records in clock order. Scalars and tables follow the same supersession rules: attempt cutoffs and last-write-wins are applied *before* any artifact is fetched, so superseded tables are never downloaded. Artifacts are found next to the metric block that references them, so a copied run still reads. A missing or damaged artifact raises `table_artifact_damaged` or `table_reference_invalid` instead of returning an empty result.
- **Warning when a run has too many metric names.** `track(..., metric_name_limit=1000)` warns once per writer when a run passes 1,000 distinct numeric metric names, and points to `log_table`. Names from earlier local attempts count. Clocks, flags, text and table references do not. Pass `None` to turn the warning off. Strict mode decides whether the warning becomes an error.
- **Index refreshes stay flat as a run grows.** `manifest.Index` replaces `write_manifest`. It describes existing blocks once, when a writer starts or resumes, and after that adds only newly sealed blocks. A refresh costs about the same at step 1,000 as at step 1,000,000. Adding a permanent block still rewrites the whole accumulated manifest, but never rereads old blocks.
- **Docs.** `docs/tracking.md` has a new "Row tables for training results" section covering writes, reads, supersession, recovery after interrupted delivery, and the name limit.

## Reports compare what each chart selects, and refresh when asked

Comparing jobs across separately tagged evaluations used to need awkward report selections. Served reports also refreshed on a timer even when nobody needed new data. Now each curve selects the matches it needs, and refresh is explicit. ([#90](https://github.com/loopflowstudio/etude/pull/90))

- **Curves can combine match selections and show one named job.** When selections overlap, each match counts once before statistics are computed. Job labels and score units make charts easier to read. The Matches picker is hidden when every panel brings its own selection.
- **Execution setup is separate from scientific variants.** `Result.execution` records device and runtime provenance without creating another player or requested match. Curves and HeadToHead work out scientific variants from the selected matches. When variants differ, they explain the ambiguity instead of averaging them. `Curve.inference` selects an exact scientific config, and the global Config control is removed.
- **Served pages refresh on demand.** `etude report … --serve` keeps showing the last generated page until you click **Refresh**. Use `--watch` for periodic updates. Concurrent refreshes share one generation, and edited helper modules are reloaded. If a refresh fails, the error is shown and the last successful page stays visible.
- **`Schedule` panels separate configured samples from each seed's recorded position**, in absolute continuation coordinates. A configured schedule shows the plan, not proof that training used those values.
- **`Report(other_metrics=False)`** loads metadata only for the metrics you request, instead of loading cumulative metric indexes.

## Operational notes

- **Move your version 13 launcher database aside before upgrading.** The launcher schema goes from 13 to 14, and the in-place upgrade path is removed. This follows the prototype rule that the launcher database has no migrations. A version 13 `ETUDE_HOME` database is refused. To read its history, keep the older Etude version installed.
- **Process exit can still take minutes on a dead connection.** `finish()` can return while a boto3 upload is still in flight, and Python exits only after s3transfer's threads finish. boto3 makes up to 5 attempts by default (`AWS_MAX_ATTEMPTS`), so exit can be held for attempts × timeout, about five minutes with the defaults. This is documented, not fixed.
- **Bootstrap subset.** `storage_timeout.py` is now standard-library-only bootstrap code. It ships in the guardian archive and is loaded by SSH's pre-install loader next to `deadline` and `logging`.
- **Existing evaluation records** default to empty execution metadata. Hardware settings already stored in scientific configs keep their existing identities.
- **Table storage is plain JSON**, with no compression or columnar layout. Very large tables logged at every update grow `artifacts/` in proportion.
- **Not yet verified.** #90 did not measure peak memory or storage requests during a real refresh on a large-vocabulary S3 workload, and did not run on a GPU (device labels were simulated). Selective reads still hold one full block header in memory.
- **Recorded checks.** #90 records 159 focused Python tests and 26 chart tests passing with the outside network denied, plus mypy and Ruff. Its full suite ran before the final opponent-variant fix. These are the PRs' recorded results; nothing was rerun for these notes.

## Small changes

- `.lf/skills/qa.md` has a recipe for reproducing storage outages: a moto proxy that silently stops forwarding, and an SSH container with iptables.
- `AGENTS.md` names `tests.run.storage.MeasuredStorage` as the fixture for counting storage reads, bytes and uploads.