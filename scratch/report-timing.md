# Dedicated Time section

Accepted scope from the supplied brief, reconciled 2026-10-15: a dedicated Time
section through publication of this increment. Later comparison and search
increments remain deferred. Merge is outside this scope.

## End state and demo

The existing generated and locally served reports recognize numeric `time/...`
metrics and present a dedicated Time section with duration curves in seconds
and a per-run summary table: recorded observation count, mean, minimum and maximum.
The existing clock selector and run selection control the section. A caller uses
ordinary tracking:

```python
run.log({"time/collect": collect_seconds, "time/learn": learn_seconds}, updates=update)
```

No profiling SDK or new storage format is required. Keep source and documentation
specific to the public client. No customer data, private design records, external
research material or quotations belong in this repository or PR.

## Behavior

- Document `time/` as elapsed seconds for a named activity in one recorded
  observation/reporting interval. An observation need not be a function call.
- Retain nested paths as labels without assuming additive parent/child timings.
- Summarize underlying logical observations eligible for the selected clock;
  do not average chart-bucket averages without weighting their observation counts.
- Exclude missing/nonfinite data with visible coverage; unavailable is not zero.
- Plot existing timing values as recorded. Do not infer total training-time pies,
  exclusive phase costs, per-call p99 or device utilization from the prefix.
- Deselecting runs changes both the per-run summary table and its duration curves.
- Hide the section when there are no timing metrics. Preserve ordinary metrics,
  evaluation views, existing job comparisons and offline standalone reports.
- Use one renderer for generated HTML and the local server.

## Implemented shape

`src/etude/track/report.py` owns report data, `report.html` owns rendering, and
`serve.py` serves the same renderer. Time owns `time/` metrics; the generic
grouping loop excludes them. The section shares `plot` with ordinary metrics.

`TimingSummary` is a frozen Pydantic model with run identity, metric,
finite/nonfinite counts and optional mean/min/max seconds. `page_data` builds
summaries from its existing chart reads, weighted by finite observation counts
before display rounding. This reuses the logical reader without additional
storage reads or changes to continuation/retry semantics.

There is one metric chart implementation and no parallel report or timer.

## Completed deletions

- `time/` metrics are excluded from the generic grouping loop; Time preserves
  their existing bucket curves through the shared `plot` function.
- Timing job-band calculation is removed because Time owns these curves.
- The local follow-up removes `figure`'s unused SVG metric curves, `bars` mode
  flag, seed-count tooltip branch and gap-aware path handling. Its only caller
  renders evaluation intervals; uPlot owns all metric curves. No exclusive
  tests or fixtures remain for the dead branch. This cleanup is not yet published.
- No storage, retry, or continuation implementation is replaced.

## Implementation decisions and assumptions, 2026-10-15

The existing reader calls these aggregates `Buckets` and carries both total
and nonfinite counts. `TimingSummary` summarizes those same logical
observations with finite-count weights before display rounding, requiring no
additional reads. The recorded implementation assumption in `questions.md` is
that Time stays per-run in job mode; ordinary job comparisons remain unchanged.
Missing observations
are excluded and explained; each row shows finite and nonfinite coverage.
All planned deletions are complete. Shared uPlot instances are destroyed before
redraw so selection and clock changes do not accumulate stale synchronized charts.

## Evidence and remaining work, 2026-10-15

Tracker-written tests cover unequal bucket populations, clock changes, zero,
nonfinite/missing values, nested labels, latest-write behavior and ordinary
reports. The storage counting test pins both ordinary and timing reports to
nine requests and two listings. No additional remote read path was introduced.

The existing evidence includes headless Chromium interaction on generated and
served reports: selection affects rows and curves, clock changes statistics,
job mode keeps Time per-run, URL reload preserves selection, and unavailable
and ordinary views behave correctly without JavaScript errors. This evidence
predates the local SVG cleanup; that cleanup has separate Node syntax and
before/after SVG output evidence for empty, singleton and multiple evaluation
intervals with focused tooltips. Reconciliation reused these results without
rerunning them; no production code changed in this pass.

Checks (existing results): `uv run python scripts/test_network.py uv run pytest` — published baseline 234 passed, 6 skipped, 3 deselected; `uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .` — baseline passed; `uv run pytest tests/track/test_report.py tests/track/test_track.py -q` — local cleanup 14 passed; broader cleanup acceptance remains with gate.

The initial increment is published as
[PR #8](https://github.com/loopflowstudio/etude/pull/8),
commit `28a1f0317f2a82201077a675af8b4e5c77d60e99`. The PR is open and ready for
review, with no auto-merge request. Reconciliation confirmed the remote head,
no review comments, and no upstream main commits beyond the branch's base
`b13b1cd`. Newly fetched dependency-update branches do not change this report
implementation. No Wave is associated with this work.

Remaining: gate acceptance of the local SVG cleanup, including the repository's
full-suite and static checks before pushing, and publication of that diff to
the existing PR. Scratch remains local. Validation requires no rented machines
or training. No implementation mismatch or unresolved product decision was
identified; later increments have not started.

## Later increments — not this Flow

2. Selection-aware overlay/mean comparisons for arbitrary selected runs with
   contributor count and optional range; retain distinct comparable-seed statistics.
   Existing precomputed job means ignore checkbox deselection and need replacement.
3. Searchable run table in the existing local `etude reports serve` server:
   explicit clock bounds and metadata filters, sorting, stable multiselection
   across experiments, and a selected-run report. Filtering must not change selection.

