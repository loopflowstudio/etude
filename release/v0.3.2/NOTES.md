# v0.3.2

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.3.2 fixes how reports draw pairs of fixed checkpoints and lets launcher versions keep separate databases. Each fixed pair appears once on its curve, and upgrading no longer requires moving aside the database an older client needs to track live rentals. ([#109](https://github.com/loopflowstudio/etude/pull/109), [#110](https://github.com/loopflowstudio/etude/pull/110))

## Draw each fixed pair once

Removing `Curve(subject=)` in v0.3.0 left curves over pairs of jobs' fixed checkpoints without a way to avoid a mirrored line for the second job. Fixed seatings now have a defined placement that restores that use case. ([#109](https://github.com/loopflowstudio/etude/pull/109))

- A seating of two fixed models is drawn at its first seat's checkpoint, so each pair appears once. ([#109](https://github.com/loopflowstudio/etude/pull/109))

## Upgrade while older clients track rentals

The launcher still does not migrate databases. It now gives each schema version its own file, allowing a newer version to start without displacing the database used by an older client tracking live rentals. ([#110](https://github.com/loopflowstudio/etude/pull/110))

- Launcher database files are named `launcher-<schema>.db`, replacing the shared `launcher.db` filename. ([#110](https://github.com/loopflowstudio/etude/pull/110))

## Operational notes

The versioned launcher filename supersedes v0.3.1's instruction to move the older database aside before upgrading. Keep the older database available to clients still tracking rentals; separate files do not provide an in-place migration. ([#110](https://github.com/loopflowstudio/etude/pull/110))

## Small changes

- Corrected the v0.3.0 release notes: Typer was held below 0.28, and the operational notes now include the breaking `Matchup` → `Seating` rename, changes to Curve's `subject`/`opponent`/`inference`, and `HeadToHead`. This is a documentation correction, not a new API change in v0.3.2. ([#108](https://github.com/loopflowstudio/etude/pull/108))