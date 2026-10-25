# v0.2.4

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.4 has one change: files an author writes under a run's `artifacts/` folder are sent once and never read again. Nothing breaks on upgrade from 0.2.3.

## Files under `artifacts/` are sent once

Delivery carries every file in a run's folder. Etude's own metric blocks, tables and checkpoints are sent once. Any other file used to be re-read and hashed on every delivery pass, so a change to it would be noticed. For a run that keeps thousands of files, that meant reading all of them again on every pass. A card evaluation with 5,000 games of evidence read about 450 MB per pass.

A file under `artifacts/`, at any depth, is now treated like a block or a checkpoint:
- it is sent once and never re-read;
- a bucket reader serves it from its cache without asking the bucket again;
- a later change to it is never delivered.

Write each such file once and name it by its content:

```text
RUN/artifacts/evidence/SHA256.json.gz
```

Files elsewhere in the run's folder behave as before. ([#43](https://github.com/loopflowstudio/etude/pull/43))
