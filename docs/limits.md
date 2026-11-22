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
- Game functions and wrapper records must be importable top-level definitions.
  Games run in spawned processes. CPU games use one model-library thread;
  GPU inference batches through a shared process. Your loader must honor
  the thread environment and choose its device. A batch allowance does not
  guarantee GPU memory residency.
- A match must return rows to count as done. Caught forward failures still
  propagate to retention. Ordinary game failures need `--retry-failed`;
  transient infrastructure failures can retry automatically.
- Logs, status and `etude view` show retained evidence, not live remote probes.
  Reads show freshness. Only explicit waits collect remote facts; expiry of a
  wait does not prove remote work was cancelled. `etude view` binds `127.0.0.1`
  with no login, records no console for plain `etude train`, and does not yet
  record machine system metrics (GPU, CPU, memory).
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
