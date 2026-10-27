# v0.2.7

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.7 stops paying twice for the same work. A retry or a second job on the same rental reuses that machine's held inputs and its prepared environment. Reports, tracking and jobs read through one shared artifact cache. Evaluation plays matches in an order that keeps loaded checkpoints in use. A finished rental now delivers its last log and end receipt before the machine is deleted. Evaluation also gets clearer names: whatever takes a seat is a `Player`, either a `Model` (a checkpoint plus an `Inference` config) or `Code`. Your recorded results still load and are not played again, but `play` functions and statsdb queries written against the old names need updating (see below).

## Retries and later jobs reuse what a machine already has

Before this release, every attempt copied its inputs next to the session and rebuilt its environment and native extensions from scratch. That happened even when a retry or a later job on the same machine needed exactly the same bytes. Inputs now stay in the machine's cache and are linked into each attempt. A job can declare a step that runs once per machine and is reused while its inputs stay the same. ([#54](https://github.com/loopflowstudio/etude/pull/54))

- **Inputs are held, not copied.** The cache keeps an input's verified bytes read-only in `ETUDE_HOME/cache` for the whole session. Each attempt checks the length and SHA256 again, then links the file at `ETUDE_INPUTS_DIR/NAME`. A worker's cache may use what it already holds plus half of the disk that was free when its session started.
- **`Job.prepare` takes a `Preparation`.** Its command writes `$ETUDE_PREPARED_DIR` and can see only its declared source `paths` and the inputs it names, at `$ETUDE_PREPARED_INPUTS/NAME`. If it reads anything else, the build fails, so a stale result is never reused. The result is keyed by those paths, the command, the inputs' digests, the job environment and the platform. Each build is checked against a receipt of file sizes and SHA256s when it is reused, and rebuilt if they don't match. `Preparation.python` becomes the job's `ETUDE_PYTHON`. `Runtime.python` is now optional.
- **Unpackaged uv projects get this automatically.** This covers a typed job with no `prepare` and no `runtime`, in a uv project that uv doesn't package and that has no workspace or path sources. It runs `uv sync --frozen --no-install-project`, keyed by `pyproject.toml`, `uv.lock` and `.python-version`, and then uses the prepared interpreter. Packaged projects keep using `uv run`. Pass `prepare=None` to opt out.
- **You can see what was reused.** `status --phases` shows a `prepared (built|reused|rejected)` milestone with the key, time, size and any rejection cause. `usage` adds one line per purpose with the bytes received and the bytes reused.
- **Presigned downloads are faster.** Each range is now 256 MiB instead of 8 MiB. It is read as 16 MiB parts over up to 32 connections, and the next range is read ahead.

## One artifact cache for reports, tracking and jobs

Before, two separate storage-read caches could download the same bytes twice. Reports, tracking and job transfers now all read through `etude/cache.py`. It is keyed by content SHA256 and length, and it reuses verified content across callers and processes. It checks whether mutable objects are still fresh, resumes downloads, honours deadlines, stays within a size bound, and can pin content. ([#51](https://github.com/loopflowstudio/etude/pull/51))

- A partial read always returns the version you asked for. Before, reading the same range of two different digests at one address could return the first version's bytes. Now a digest-pinned range downloads and verifies the whole artifact before handing back the slice. Cached content is checked again before every slice.
- Report pages don't need to be rebuilt to stay correct. A built page is reused until the experiment file or any object in its runs' folders changes. That is checked through a new `Storage.versions` listing, so an unchanged page costs one listing and no bucket bytes. `--serve` and `--watch` share these built pages. As before, edits to modules that the experiment file imports are not detected. ([#49](https://github.com/loopflowstudio/etude/pull/49))
- S3's legacy us-east-1 address is normalized, so a cached object is reused whichever address form was used.

## Evaluation: Players, and an order that reuses checkpoints

Matches used to start in key order, which is effectively a hash order. Consecutive matches therefore seated unrelated checkpoints, and a runtime that keeps only a few policies loaded kept reloading them. `evaluate` now starts at most `workers` matches at a time, in the order `etude.eval.schedule.ordered` gives. That order prefers the match that loads the fewest new checkpoints, then the one that reuses the most, then the one whose checkpoints were used most recently. Ties keep the caller's order, so the order never changes a result. On a 20-checkpoint benchmark (690 matches) with a two-checkpoint LRU, loads fell from 806 to 21. `hold(share=(k, n))` now gives each share a contiguous part of this order, so each machine seats only some of the checkpoints. ([#53](https://github.com/loopflowstudio/etude/pull/53))

The seat vocabulary changed too. "Agent" hid the difference between a trained checkpoint and hand-written code, and it sounded like an LLM agent. ([#57](https://github.com/loopflowstudio/etude/pull/57))

```python
from etude.eval.records import Code, Inference

heuristic = Code(name="greedy", source=..., config=...)
inference = Inference(source=..., config=...)  # was Model(code=..., config=...)
model = inference.model(ref)  # or checkpoint.model(inference) in eval()
```

- `Agent`, `AgentKey` and `agent(...)` are gone. Both `Model` and `Code` are a `Player`, and each one's `key` keeps the old encoding (`checkpoint:…` / `code:…` plus its config).
- A seat's position is called `seat` everywhere: `match.players` (was `match.agents`), the `seat` column in rows that `play` returns (was `player`), and `seats.seat` in statsdb.
- In statsdb, the `agents` table is now `players`, with a `kind` column (`model` or `code`), joined on `player`.
- A model's checkpoint file is at `model.checkpoint.artifact.uri`.
- Records written by the published etude-sdk (`Agent` dicts, `agents` fields, Parquet `player` columns) are converted when they are read. Match keys and matchup definitions are unchanged, so existing eval runs still count as delivered.

The report page also gains a "Played at" commit picker. It appears when delivered matches were played at two or more commits. The eval database now keeps each eval run's rows, so each commit's view is accurate. ([#49](https://github.com/loopflowstudio/etude/pull/49))

## Launched experiments and final rental delivery

`etude launch ppo.py` now records experiment `ppo` on the jobs it starts, as `Job.experiment`, and the job sees it as `ETUDE_EXPERIMENT`. This applies to experiment files: those with a top-level `eval(state)` or `report()`. `track()` uses that experiment when none is given. A `track(experiment=...)` that names a different experiment is refused with `experiment_differs_from_launch`. Launching another experiment's `evaluation(...)` from an experiment file is refused before anything is rented. You no longer need `track(experiment=<stem>)`. Plain job files, and the digests of existing jobs, are unchanged. ([#49](https://github.com/loopflowstudio/etude/pull/49))

Before this release, a rented machine was deleted about 2 s after its last attempt finished, which lost `out/ended` and the end of `worker.log`. The launcher now asks an idle session to finish through its inbox. The courier then collects the console, delivers the last files and the log tail, and writes `out/ended` last. Only after that is the pod deleted. Shutdown and final delivery share one time allowance, capped by the rental deadline. A failed delivery is diagnosed and retried within that allowance. The launcher confirms the deletion when the receipt arrives, or at the deadline. `worker.log` is now delivered as appended pieces under `out/worker.log/OFFSET`, listed in `out/worker-log.json`.

## Operational notes

- The release lists no compatibility break, so older versions keep working against the same stores. Stored eval records from earlier etude-sdk versions load under the new names without being played again.
- **Code you wrote against the evaluation API needs updating.** Make these changes:
  - `match.agents` → `match.players`
  - Rows return `"seat"` instead of `"player"`.
  - `join agents using (agent)` → `join players using (player)`
  - `Model(code=..., config=...)` → `Inference(source=..., config=...)`
  - `agent(...)` → `Code(...)` or `Inference(...).model(ref)`

  A row that names `player` but neither `seat` nor `group` is refused with `row_needs_seat_or_group`.
- The launcher database schema version was bumped to record each attempt's `Prepared` result. A launcher database written by an older version is refused, as the prototype has no migrations.
- During an upgrade, a split `hold(share=...)` resumed by this version divides the matches differently from a share still running the old version. It may play a match the other share is playing at the same moment. Delivered matches are skipped, so the worst case is playing a match twice, never a wrong result.
- Old cache directories are not migrated to the shared cache. Expect some downloads to happen again once after upgrading. The cache is local to each computer. When content is evicted, or an artifact is too large to keep, it is downloaded again on the next read.
- Edge case when a rental finishes: a run placed on a session in the second or two after that session was asked to finish still runs, if it arrives before the session exits. If it arrives later, it is recorded as lost and retried like any lost attempt.
- Pins are now named per holder (`SHA256-SIZE.HOLDER.pin`). `place(..., pin=True)` and `unpin` are removed.

## Small changes

- The report's empty-evals message names the experiment file once, so a long path no longer fills the card. ([#50](https://github.com/loopflowstudio/etude/pull/50))
- When the scheduler refuses a manual rental that would outlast its storage grants, it now computes the grant limit only once. The behaviour and the wording are unchanged. ([#48](https://github.com/loopflowstudio/etude/pull/48))
- Contributor guidance now forbids intentionally causing CPU, memory, disk or network load in tests, debugging, benchmarks and demos. `TESTING.md` describes controlled alternatives. ([#52](https://github.com/loopflowstudio/etude/pull/52))
- `append_usage` is now the only writer of the usage journal. ([#54](https://github.com/loopflowstudio/etude/pull/54))