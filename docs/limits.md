# Limits and sharp edges

This page is for authors deciding whether Etude fits their workload and what
failure recovery requires.

- Etude supplies execution, retention and reporting. Learning algorithms,
  games, search, sampling, model device selection and API clients are yours.
  Python 3.12 and uv are required; training/model dependencies are separate
  from Etude's machine-side client environment.
- A training attempt can run again. Register models, optimizers, counters and
  random state needed to resume. A lost machine loses work since the last
  delivered save. Etude cannot recover files that never left a destroyed disk.
- A spot machine can be taken back at any moment and gives no time to save.
  Each loss is retried without counting toward `max_attempts`; only the run's
  hours and dollars end a spot run that never reaches a save. Etude does not
  fall back from spot to the standard price or to another provider.
- Successful matches are reused by identity within one experiment and
  environment. A game/API call can execute again after process loss before
  its result was retained. External side effects need your own idempotency;
  match retention does not make a third-party API exactly-once.
- Code and environment source versions are provenance. Change identifying
  config or names when new behavior should be a new scientific condition.
  Keep published checkpoint contents fixed under their keys.
- Game functions (`play`, `play_many`), loaders, build hooks and wrapper records
  must be importable top-level definitions. Games run in spawned processes.
  Multiple `async def play` matches share one `asyncio` event loop per game
  process; blocking the loop without awaiting stalls the other games in that
  process until the run deadline. With a loader, a `play_many` phase runs
  direct inference in one game process with the host inference thread budget.
  A `play` phase uses a direct inference thread in each game process when no
  NVIDIA GPU is seen, or batches through a shared agent process on visible
  NVIDIA hardware. Your loader must honor the thread environment and choose
  its device. A batch allowance
  does not guarantee GPU memory residency. If a game process crashes—including
  a native crash in the model or engine under direct inference—every match it
  holds fails with retry while already-sealed matches survive.
- Direct inference isolates loaded models on the inference thread, but a game
  module that defines `load` (or imports the same stateful module) still shares
  Python module globals with it inside one interpreter. The in-flight read-only
  lock covers the `numpy.ndarray` object handed to `forward(...)`, not slices
  taken earlier from the same buffer or native pointers. In `play`, call-time
  locking covers `numpy.ndarray` objects immediately while mutable containers
  (`dict`, `list`, `bytearray`) snapshot when the zero-delay barrier flushes
  (versus on call in `play_many`).
- A match must return rows to count as done. In `play`, caught `ForwardError`s
  still fail the match; in `play_many`, yield `(match, error)` for failed slots
  or let the error escape to fail held matches. Ordinary game failures need
  `--retry-failed`; transient infrastructure failures can retry automatically.
- Logs, status and `etude view` show retained evidence, not live remote probes.
  Reads show freshness. Only explicit waits collect remote facts; expiry of a
  wait does not prove remote work was cancelled.
- The live view has no login. Its default bind address is `127.0.0.1`. Run
  consoles depend on launcher retention. Plain `etude train` writes output
  only to the terminal. Machine CPU, GPU and memory samples are unavailable.
  A file-only run with no outcome shows **no end recorded** once the view reads
  its files from a listing started at least 10 minutes after its last write.
  If a listed block disappears during the read, the view keeps that run’s
  previous observation across restarts. A later write can reopen the experiment.
- Time/dollar caps bound admitted rental allocations, not provider bills.
  Outages can delay deletion. External storage/transfer/API costs are excluded.
  An EC2 machine reading or writing a bucket outside its AWS region (any R2
  bucket) pays AWS for that transfer, outside the quote.
  Rental windows cannot outlast scoped storage grants (7 days on R2, 36 hours
  on S3). Queues require a scheduling loop to keep advancing.
- `availability` and `--quote` report a provider's stock signal, not a
  reservation. A provider can call hardware `rentable now` and refuse the
  create seconds later; the launch then fails with the provider's reason and
  rents nothing. Queued refusals retry once a minute without an attempt limit,
  retaining one machine, rental, session and attempt record per refusal
  (up to 1,440 asks and 5,760 records per on-demand per day).
- Logging uses a bounded queue. Check incomplete-row counters and warnings.
  Reports reading a bucket see only delivered files, and list the bucket on
  every build: a report from `--source s3://...` or `r2://...` needs the
  network and credentials even when its data is cached. A report from a local
  runs directory needs neither. A final delivery wait
  can expire while a network library still has active transfer threads.
- Released config panels and multi-game keys remain readable, alongside
  older player formats. Switching a stored tournament to indexed matches
  requires a fresh tournament name; old results are not silently reinterpreted.

Start with [the local prototype](getting-started.md), then read
[training](training.md), [evaluation](evaluation.md) or
[the launcher](launcher.md) for the relevant workflow. Maintainers changing
these guarantees use [the developer reference](dev/launcher.md).
