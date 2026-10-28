# v0.2.11

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.11 is a small speed release for reports that estimate rates over many selections. v0.2.10 had already cut the bootstrap down to one per page. Now a page that switches between a few block counts draws each count's weights once instead of on every switch, and intervals stay fast when some replicates have no opportunities. The numbers are identical to v0.2.10, so upgrade if a page of `estimate` calls is slow. Nothing else changes. ([#71](https://github.com/loopflowstudio/etude/pull/71))

## Faster estimates across many selections

A report that compares many selections, such as per-card rates over deals, calls `etude.eval.query.estimate` once per selection. Those selections usually take only a few distinct block counts. Before this release only the most recent weight draw was kept, so alternating between counts redrew a `REPLICATES × n` multinomial every time. Percentiles were also slow: when any value was NaN, `np.nanpercentile` fell back to one column at a time.

- **Weights are reused across block counts.** Draws are kept for several block counts. When the cache is full, the least recently used count is dropped first. The cache holds at most `WEIGHTS_KEPT` floats (2²⁵, 256 MiB). The latest draw is always kept, even if it is larger than the cap on its own. A lock guards the cache, so estimates can run on several threads.
- **Results are unchanged.** Each draw is still fixed by `SEED` and the block count, so a given selection gets the same interval whichever order a page asks for it in, and the same interval as in v0.2.10.
- **Intervals use one sort for all columns.** The 95% intervals now come from a vectorised percentile that sorts every column in one call. It reproduces numpy's default linear interpolation bit for bit, and a column that is entirely NaN still gives NaN.

## Operational notes

- The release lists no compatibility break. Stored data, APIs and commands are unchanged.
- **Memory.** A process that runs estimates can now hold up to about 256 MiB of bootstrap weights, where before it held a single draw. A draw for one block count is about 16 kB per block. A count above roughly 16k blocks is bigger than the cap by itself and is kept as the only entry.
- **numpy upgrades.** The percentile code copies numpy's private interpolation arithmetic. A test compares it exactly with `np.nanpercentile`, so if a future numpy release changes that arithmetic, the test fails rather than intervals shifting without notice.

## Small changes

- New tests in `tests/eval/test_match.py` check the percentile against `np.nanpercentile` on columns with scattered NaN, a single value, no NaN, only NaN and ties. They also check that repeating estimates over block counts 3, 5 and 8 draws each count's weights once and returns the same estimates every time.