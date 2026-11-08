# v0.2.15

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.15 adds Lium as a third GPU provider beside RunPod and Vast.ai. You can now choose how many GPUs a machine gets, and run several runs on one machine at once. Launched evaluation passes are no longer opaque while they run: status shows matches played, pace and time left, and logs can be read during the run. When a machine ends before delivery is confirmed, the attempt keeps the outcome the job reported instead of being lost. Accounting stays conservative across all three providers: a reported charge counts as settled only when the provider shows what billing period it covers.

## Rent from Lium, with the GPU count you need

Lium uses the same availability, quote, launch, wait and output-fetch commands as the other providers. `--gpu-count` now works on all three. ([#83](https://github.com/loopflowstudio/etude/pull/83))

- **Register Lium with `--kind lium`.** Follow the Lium setup in `docs/launcher.md`, then check capacity and cost without renting: `etude availability rtx5090 --timeout 30` and `etude launch job.py --rent rtx5090 --quote`.
- **Lium picks secure nodes using the quoted allocation resources and respects each node's minimum GPU count.**
- **Budget reservations include Lium's price-change tolerance and its whole-hour fallback timer.** A short job reserves a whole hour, but its execution deadline does not change. The reservation is not a billing minimum.
- **Acquisition follows the Vast pattern.** Rental intent is saved before the create request. A lost reply is reconciled without sending another paid request. SSH bootstrap waits until Lium acknowledges a scheduled removal, and a replayed SSH start cannot launch a second worker. The account key stays on the controller.
- **Deletion and costs require evidence.** Deletion completes only after the pod is confirmed gone. Actual costs are reported only when validated ledger statements show billing coverage. Otherwise the estimate stays.

## Run several runs on one machine

A sweep of small jobs used to need one machine per run, or had to run one after another. Now a pool or on-demand can declare `--slots N`, and each of its machines runs up to N runs at the same time. ([#85](https://github.com/loopflowstudio/etude/pull/85))

- **Set it with `etude pool add … --slots N` or `etude ondemand add … --slots N`.** The default of 1 keeps the old one-run-at-a-time behaviour. `pools` and `ondemands` show the slot count when it is above 1.
- **Placement fills a machine's free slots before taking another machine**, so a cohort's `parallel` still counts machines, not runs. A running machine takes more waiting runs as slots free up, as long as each run's hours fit in the rental's remaining time.
- **Budgets remain per run.** A run is admitted only if its own `max_usd` would cover the machine running that run alone. Each moment of a rental is split equally among the runs on it at that moment. Idle time belongs to the rental, not to any run.
- **Runs stay independent.** Each attempt has its own folder, outputs, log and delivery. Cancelling or failing one run leaves the others running.
- **Sharing is visible.** `status` adds `shared by K runs`. `--quote` shows each run's machine, its slot count and the runs placed with it.
- **Nothing is partitioned.** Runs on one machine compete for the same GPU memory and CPUs. A slot count that is too high shows up as out-of-memory errors or slowdowns, not as a scheduler refusal. The live canary ran on a CPU pod, so contention between runs on a GPU is untested.

## Follow a launched evaluation while it plays

Before this release, a launched eval pass told you nothing until it ended. It now reports progress, makes its output readable during the run, and keeps what it delivered if the machine ends early. ([#87](https://github.com/loopflowstudio/etude/pull/87))

- **Status shows progress**, for example `running, 412/1800 matches, 940/hour, ~1.5 hours left, ~$0.31`. Eval runs write a `tally` to `etude-result.json` at most every 15 seconds, and other jobs can write one too. The pace covers the last 50 matches, and time left is omitted until two matches have been played. When no retry fits the remaining hours or budget, status also gives the reason the last attempt ended.
- **`etude logs` works during the run.** The controller fetches the log only when it has grown, at most every 30 seconds. Each read is bounded by the supply's `log_seconds`, or 30 seconds if the supply sets none. The old fixed 2-second cap on final-log reads is gone.
- **The outcome survives the machine.** The courier records how the job ended (`exited`) before it uploads files, then sets `delivered`. If the machine disappears before delivery, the attempt is settled with the outcome the job reported, plus a diagnostic saying the files were not confirmed stored. If one attempt fails to deliver, the courier still delivers the other attempts, the worker log and usage before it raises the error.
- **Partial results are labelled.** `statsdb`'s `runs` table gains a `state` column, which stays null until the run ends. `etude report` adds a "Partial results" note naming eval runs with no recorded end.
- **A failed pass blocks only the matches it touches.** Before, any failed run in the ledger stopped every launched pass on the computer. Now a pass stops with `launched_pass_failed` only when it would launch matches that went to a failed run. `--retry-failed` launches those matches again under a new run name (`…-2`, `…-3`).
- **Evaluation designs can see when checkpoints were saved.** Each `Checkpoint` from `observe` now carries `wall_ms`, its save time in milliseconds since the epoch, so a design can pair checkpoints by real training time while runs are still going. ([#86](https://github.com/loopflowstudio/etude/pull/86))

## Operational notes

- **Update the controller, workers and job projects together.** `AttemptRecord.finished` is renamed to `exited`, and a `delivered` flag is added. If a worker runs older code, this controller won't read its records as ended. Separately, a launched evaluation job that lists `checkpoints=` fails if its project pins an older Etude, because etude-sdk 0.2.14 rejects `checkpoints.0.wall_ms` as an extra field. Designs that only seat checkpoints in matchups are unaffected.
- **Databases upgrade on first open.** According to the PRs, the launcher database moves from version 11 to 12 (adds `pools.slots`) and then to 13 (adds `attempts.tally` and `sessions.finish_requested_at`), upgrading in place. On-demands stored before this release read as one slot. The `statsdb` schema moves to version 8, and a kept database is rebuilt from its store on the next read.
- **Lium shutdown is best effort.** A watchdog starts before dependencies install and stops local work, but it cannot delete the pod or end billing. Cleanup depends on the controller and on Lium's timers, and delays in boot or removal can push spending past the reservation. Keep the controller running so it can reconcile and retry cleanup.
- **Vast charges are read but not settled.** Charge queries now cover whole UTC days, including the day an instance was created. They follow every page, reject duplicate totals and validate dates. The endpoint supplies no billing-coverage watermark, so `VastMachines.bill` reports no settled bill, and confirmed deletion does not change that. A read of five terminated rentals returned $0.398 in reported charges, but released no reservations. ([#82](https://github.com/loopflowstudio/etude/pull/82))
- **Not yet verified on paid machines.** For Lium, paid custom-image boot, SSH startup and removal timing are untested. Verifying them needs a funded account, SSH access and journal storage, and should produce retained output, confirmed pod removal and a settled ledger statement. Live eval progress has not been run on a rented machine either. The suggested check is `etude eval … --rent cpu8` against a real bucket, watching `etude status RUN` and `etude logs` while it plays.
- **Recorded checks.** The PRs record network-isolated suites with 595 passed (#82) and 627 passed (#83, run before its final focused gate of 328 tests), each with 6 skipped and 3 deselected. Mypy and Ruff also passed. #86 ran focused checks only. These are the PRs' recorded results; nothing was rerun for these notes.

## Small changes

- The scheduler now records `finish_requested_at` before it tells an idle rented session to finish, and placement skips sessions in that state. Before, work could land on a worker that had already left its loop, and that work ended as `create_unconfirmed`.
- `etude session` runs a packet's attempts on a thread pool sized to its slot count. If the worker is told to end, a shared halt flag stops every running phase and cleanup still runs.
- `docs/launcher.md` gains a "Several runs on one machine" section, and `docs/evaluation.md` lists `wall_ms` among the checkpoint fields.