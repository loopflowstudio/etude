# v0.2.12

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.12 lets you upgrade from 0.2.6 without losing your launcher history. It also adds a new way to read a learning curve. Before this release, every Etude from 0.2.7 on refused a launcher database written by 0.2.6, so you had to delete your record of runs, rentals and settings or stay on the old version. Now that file is upgraded in place. Learning curves can also score each checkpoint against the same line at a fraction of its own training, which answers "does this checkpoint beat itself at half its updates?". This range also includes a fix for `etude fetch` on large outputs, which shipped in v0.2.11 but was not described in its notes.

## Keep your 0.2.6 launcher database

A launcher database records every run, rental and setting. A shared one, such as Manabot's `~/.etude/launcher.db`, can still have a 0.2.6 controller running on it, and newer launches had to go through a 0.2.6 checkout. Etude now upgrades that file instead of refusing it. ([#73](https://github.com/loopflowstudio/etude/pull/73))

- **Version 10 becomes version 11 in place.** When Etude opens a version-10 file (written by 0.2.6), it adds `attempts.prepared`, the only schema change since then, in the same transaction that checks the version. Existing rows keep their values. Attempts recorded before the upgrade have `NULL` in the new column.
- **Each upgrade is declared per version.** `_UPGRADES` in `etude.launch.database` maps each version to the statements that bring it to the next one. A version with no path to the current schema is still refused and left untouched.

## Compare a checkpoint with its own earlier self

Before this release, a curve could score each checkpoint against a fixed checkpoint (`At`) or one a fixed distance back (`Earlier`). The new `Fraction` opponent picks the checkpoint at a fraction of each checkpoint's own position. ([#65](https://github.com/loopflowstudio/etude/pull/65))

```python
Curve(opponent=Fraction(clock="updates", value=0.5))
```

With this opponent, the checkpoint at 6,000 updates plays its line's checkpoint at 3,000, and the one at 400 plays the one at 200.

- **`Fraction` lives in `etude.report`**, next to `Earlier` and `At`. `value` must be strictly between 0 and 1. The opponent must be in the same line and config, at exactly that position. If no checkpoint stands there, the curve shows a gap that says where it looked, for example `no checkpoint at updates 2,975`. The page subtitle reads `vs the checkpoint at 0.5 × its updates`.
- **Positions match by value.** `Earlier`, `Fraction` and `At` now round both the computed and the stored positions to 12 significant digits before comparing them. For example, 90 × 0.7 (`62.99999999999999`) now finds the checkpoint at 63.
- **A checkpoint never plays itself.** `At(value=400)` used to score the checkpoint at 400 against itself. That point is now a gap. `Fraction` at position 0 is also a gap.

## Fetch large outputs

`etude fetch` used to fail on any output large enough to download in parts, such as a real checkpoint. Each part ran on another thread, and the launcher tried to record that part's transfer through a SQLite connection that only its opening thread may use. Large outputs now download intact. Each part still appears in the launcher's transfer history as its own `result` transfer, with its own byte range. This fix first shipped in v0.2.11. ([#70](https://github.com/loopflowstudio/etude/pull/70))

## Operational notes

- **The database upgrade is one way.** After a newer Etude opens a version-10 `launcher.db`, the file is version 11 and 0.2.6 can no longer open it. On a shared file, upgrade only when every process that uses it can move to the new version. To try it safely, copy the file into a scratch `ETUDE_HOME` and run `etude runs`. It should list the file's runs, and `PRAGMA user_version` should then read 11.
- **Existing `At` curves look slightly different.** The point at the anchor's own position used to show a self-play score. It is now a gap. This is deliberate, but you may notice it if you compare old and new pages.
- **Bucket requests on other threads.** The launcher records a transfer directly only for requests made on the thread that opened its database. A request on any other thread is recorded only if its caller sets a sink for observations. The multipart download sets one, and it is the only path that runs bucket requests on other threads today.
- The release lists no compatibility break.

## Small changes

- Bootstrap estimates over many selections are faster, and the numbers are unchanged. This shipped in v0.2.11, which describes it in full. Processes that run estimates can hold up to about 256 MiB of cached weights. ([#71](https://github.com/loopflowstudio/etude/pull/71))
- `docs/tracking.md`, `report.py` and `page.py` now list `Fraction` alongside the other line-scoped opponents.