# v0.4.5

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.4.5 lets you train on Google Colab and track what a run consumes in Compute Units (CU). Following a run is faster, too. An unchanged S3 or R2 report can reuse its rendered page, and you can keep querying evaluation results while another process refreshes them.

## Train on Colab and account for Compute Units

Add Colab with `etude provider set colab --kind colab --storage <storage>`. The launcher authenticates locally and starts the worker through a Jupyter kernel, so your account credentials stay on the launcher. ([#137](https://github.com/loopflowstudio/etude/pull/137))

- Choose a CPU runtime or one T4, L4, G4, A100 or H100 GPU. Etude checks the template when you add it, before requesting a runtime.
- Track estimated consumption in CU. The default dollar valuation is $0 per CU. Set `--usd-per-unit` on the provider to use your own valuation for dollar estimates and budgets.
- Colab reveals the hourly CU rate after assignment. With a positive dollar valuation, `etude launch --quote` flags that timing. If the observed rate exceeds `--max-usd-per-hour`, Etude releases the runtime before starting the worker. If a dollar budget shortens the run, Etude reserves time to deliver checkpoints.
- Concurrent assignments can make an individual runtime’s rate impossible to measure. In that case, `etude machines` and `etude status` show `CU rate unobserved`.

## Refresh reports while others read the results

On an R2 experiment with about 92,000 keys and 42 panels, the median unchanged refresh fell from 369 seconds to 8 seconds. Each refresh made one live bucket listing and zero object GETs, with the same displayed scores. ([#136](https://github.com/loopflowstudio/etude/pull/136))

- `etude report` runs the experiment file before checking the page cache. Edits to external inputs read by `report()` or `eval_jobs()` that change the report are picked up on the next refresh. A cold rebuild reads training blocks and evaluation tables in one pass.
- `statsdb()` now returns a read-only connection. Readers can finish queries against their current snapshot while Etude publishes an updated one, avoiding the `statsdb_in_use` error during a concurrent refresh.

## Operational notes

Run `etude database upgrade` on an existing launcher database before using the upgraded launcher. The migration adds provider and rental billing fields. Existing records keep their USD valuation at one dollar per unit. ([#137](https://github.com/loopflowstudio/etude/pull/137))