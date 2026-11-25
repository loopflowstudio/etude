# v0.4.7

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.4.7 makes owned SSH hosts more dependable during long training. A host can now recover from a lost start reply, release a failed or cancelled setup, and stay occupied until its last upload has reached the bucket. Resumed experiments can now wait in the queue for capacity like any other work. Reports can now draw a baseline from another experiment without retraining it, line warm-started runs up with the checkpoint they branched from, and print quieter, more accurate notes at the top of the eval page.

## Owned SSH hosts recover and release cleanly

Before this release, a dropped start reply, a cancelled install or a courier still uploading could leave an owned host's slots wrong. A slot could be stuck as occupied, or freed while delivery was still running. Each session now records the processes that own it, and the host releases a slot only once that work has actually finished. [#146](https://github.com/loopflowstudio/etude/pull/146)

- A boot claim records the boot PID, so Etude can recover the session if the start reply is lost.
- On a bucket-backed host, the courier PID tracks liveness through final delivery, and the bucket's end receipt confirms that delivery finished. If the courier dies, the host stops any remaining worker before releasing the session.
- A shutdown request closes every slot immediately, including open slots on a host that is still installing. If you repeat a stop while courier setup is being cancelled, the diagnostics upload still finishes.

## Queue resumed and watched experiments

`etude queue` now accepts `--resume`, `--until` and `--watch`, so a resumed experiment can wait for pool capacity instead of needing a free machine at launch. [#146](https://github.com/loopflowstudio/etude/pull/146)

```sh
uv run etude queue experiment.py --resume --job base --seed 0 --until updates=4000 --pool lab
uv run etude queue experiment.py --watch 60 --pool lab
```

- `--watch` runs repeated passes and picks up newly delivered checkpoints. Use separate commands for `--resume` and `--watch`.
- With a derived source, `launch`, `queue` and `eval` list only this experiment's storage prefixes, plus any cross-experiment tournament lines they need. An explicit source still lists its whole prefix.

## Compare against baselines and warm starts in reports

Comparing a new arm against a run you already trained used to mean training or evaluating that run again inside the new experiment. A report can now include it from the experiment where it already lives. Warm-started runs can also be placed where they branched off, so they no longer appear to start again from zero. [#145](https://github.com/loopflowstudio/etude/pull/145)

- `Report.include` takes `Include(experiment, jobs, tournaments)` entries. The named jobs' training curves and ML and perf traces, plus the eval tournaments already played, appear on the Eval, Behaviour, Perf and ML panels. A `baseline="tau-x2"` include draws that run dotted.
- Eval catch-up for an include builds its own scoped cache (`<experiment>.<store>.inc-<digest>.duckdb`) that covers only the included tournaments and job checkpoints. Your experiment's existing eval databases are not changed.
- `Report(displace_starts=True)` shifts each warm-started run's `updates`, `learner_transitions`, `training_seconds` and `running_hours` by the coordinates of the `start` checkpoint recorded in its `run.json`. The shift applies to training traces, bands, checkpoint marks and eval player placements. It is off by default.

## Clearer eval report notes

The notes above an eval report now describe what the page actually draws. [#147](https://github.com/loopflowstudio/etude/pull/147)

- Outside checkpoints seated without `at` can be named opponents. The anchor note names them when an Anchors panel includes them or when a curve uses their score as a reference.
- Environment overlap notes count overlapping scores with the page's own panel, selection and opponent rules. Each value comes from the environment whose latest match is newest.
- The Evals toolbar offers **Anchor lines** only when a plotted curve has a reference line to toggle. It stays hidden for empty splits and remains available after you switch it off.
- Seated panels find split values from their first-seat points, including scores whose first seat was unplaced.
- Limitation: overlap counting uses each panel's opponent choices, so a pinned curve whose choices differ from the section toolbar can still add a value you cannot reach from the toolbar. Not included: split-level reachability in notes.

## Operational notes

- No database migration is needed.
- Sessions still in flight from v0.4.6 have no recorded courier PID. They keep waiting for the bucket end receipt before releasing their slot.
- Host liveness and cleanup rely on numeric PIDs. If a PID is reused, a dead process can look alive, or a cleanup signal can reach an unrelated process.
- Cached report pages are rebuilt so they show the new notes.
- Report includes add `.inc-<digest>.duckdb` cache files beside an experiment's eval databases.

## Small changes

- Restore now accepts a mutable out file that grew after it was listed. HTTP transfer lengths and recorded checkpoint digests and lengths still verify each read. [#146](https://github.com/loopflowstudio/etude/pull/146)
- `etude view` keeps serving, without logging an error, when a browser disconnects. [#146](https://github.com/loopflowstudio/etude/pull/146)
- The tracking guide describes where report notes appear. The QA recipe shows how to reproduce an environment overlap with explicit matches. [#147](https://github.com/loopflowstudio/etude/pull/147)
- Dependabot no longer opens weekly version-update pull requests. [#149](https://github.com/loopflowstudio/etude/pull/149)