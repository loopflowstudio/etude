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
- Logs and status are retained evidence, not live remote probes. Reads show
  freshness. Only explicit waits collect remote facts; expiry of a wait does
  not prove remote work was cancelled.
- Time/dollar caps bound admitted rental allocations, not provider bills.
  Outages can delay deletion. External storage/transfer/API costs are excluded.
  Rental windows cannot outlast scoped storage grants (7 days on R2, 36 hours
  on S3). Queues require a scheduling loop to keep advancing.
- Logging uses a bounded queue. Check incomplete-row counters and warnings.
  Reports reading a bucket see only delivered files. A final delivery wait
  can expire while a network library still has active transfer threads.
- Released config panels and multi-game keys remain readable, alongside
  older player formats. Switching a stored tournament to indexed matches
  requires a fresh tournament name; old results are not silently reinterpreted.

Start with [the local prototype](getting-started.md), then read
[training](training.md), [evaluation](evaluation.md) or
[the launcher](launcher.md) for the relevant workflow. Maintainers changing
these guarantees use [the developer reference](dev/launcher.md).
