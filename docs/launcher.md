# Running on machines

This guide is for authors submitting work to local, SSH or rented machines;
[training](training.md) and [evaluation](evaluation.md) own the job functions.

```sh
uv run etude database upgrade
uv run etude launch experiment.py --smoke
uv run etude status
uv run etude logs --run experiment/base@0
uv run etude wait
```

With no machine options, launch uses the configured default, initially the
local pool. `--smoke` first runs the declared tiny training config, eval and
report locally. Give each `TrainingJob` a `smoke=` config. No smoke rents
compute, though your own game code can still call paid APIs.

## Typed jobs

An experiment file returns `training_jobs()` and `eval_jobs(state)`; each pass
returns everything that should exist by now. Launch submits only missing work.
Later passes see new checkpoints and submit their eval matches. Supply finite
`max_hours` and `max_usd` on jobs that can rent. Training seeds make independent
runs; eval seeds are indices in the match generator's panel.

`Job` is also available from `etude.spec` for arbitrary shell commands. Its
`inputs`, `environment`, `prepare`, `runtime`, `setup` and `cleanup`
control execution. Typed jobs derive their command from an importable Python
function and validated Pydantic config; do not specify another command. Their
`extras` select project dependency extras for that function's environment.

## Pools and owned machines

```sh
uv run etude pool add lab
uv run etude machine add mini me@mini.local --pool lab
uv run etude launch experiment.py --pool lab
```

A pool contains machines you own. `--cost-usd` and `--expires-at` record a paid
owned window when needed; expiry never deletes an owned host. An SSH host needs
Python 3.12, uv, git and working SSH credentials. The job uses a separate
Python environment from Etude's client. `--pool lab --rent gpu` uses available
owned capacity first, then rents. Machine and rental facts are separate from
scientific run identity.

## Rented machines

These commands configure one rental at a time. Substitute your bucket and
inject credentials into the launcher's environment before running them.

```sh
uv run etude storage add west --bucket YOUR_BUCKET --prefix etude/launch --provider s3 --region us-west-2
uv run etude provider set runpod --kind runpod --storage west
uv run etude ondemand add gpu --provider runpod --gpu "NVIDIA L4" \
    --vcpus 6 --memory-gb 48 --disk-gb 40 --max 1 --max-usd-per-hour 0.80
uv run etude availability gpu --timeout 30
uv run etude launch experiment.py --rent gpu --max-usd 2 --quote
uv run etude launch experiment.py --rent gpu --max-usd 2
```

A pool names owned capacity; an on-demand names a provider template and rental
limits. Credentials never go in those records. RunPod uses `RUNPOD_API_KEY`,
Vast uses `VAST_API_KEY` and Lium uses `LIUM_API_KEY`. Set up bucket credentials
as described in [tracking](tracking.md#storage). R2 uses `--provider r2`.

For Vast, use `provider set vast --kind vast --storage west`; register your
SSH public key in the provider account. For Lium, use `--kind lium` and a Lium
provider name. Availability and image conventions differ; the developer
[provider reference](dev/providers.md) covers those details. There is no
cross-provider fallback.

`--max-usd` bounds the submitted allocation; each job also has its own cap.
Quotes rent nothing. A rental has an independent deadline guardian, but a
provider outage can delay deletion and billing can exceed admission estimates.
Storage charges are separate. See [limits](limits.md).

`availability` reports the provider's stock signal; it reserves nothing. When
the provider then declines to make the machine, launch fails with the
provider's own words and records no run:

```text
$ uv run etude launch experiment.py --rent gpu --max-usd 2
etude: launch 215b6457f928 for experiment/base@0: cannot launch on 'gpu': provider refused: There are no longer any instances available with the requested specifications. Please refresh and try again.; etude queue would wait
```

Nothing was rented and nothing needs cleaning up: `etude machines` lists the
refused machine as `unavailable`, it does not count toward `--max`, and the same
run can be launched again at once, on this on-demand or another. A queued run
keeps the reason in `etude status` and its on-demand is asked again no sooner
than a minute later. Refusals count toward the job's `max_attempts`. A reply that does not settle whether a machine was made
(a timeout, a dropped connection, a provider error) is not a refusal: Etude
looks that machine up before asking for another.

## Queue, launch and wait

| Command | What it does |
| --- | --- |
| `queue FILE.py` | Records intent and returns ids; waits for no provider. |
| `launch FILE.py` | Acquires capacity, starts what fits and queues the rest; fails with no runs recorded if none fit. |
| `launch FILE.py --quote` | Plans without submitting or renting. |
| `status`, `machines`, `logs` | Reads retained evidence; prints freshness/incompleteness. |
| `wait` | Advances work until the selected runs finish and the machines rented for them are deleted. |
| `logs --run RUN --follow --timeout SECONDS` | Collects new logs and follows them. |
| `cancel RUN` | Requests cancellation and cleanup. |
| `fetch RUN FILE DIRECTORY` | Copies one file the run wrote into `DIRECTORY` and verifies its fingerprint. |

Queued work needs a scheduling loop: `wait`, a later scheduling command, or the
optional server. An unattended queue does not run itself without one.
`rent NAME --hours H --max-usd D` explicitly rents capacity;
`terminate MACHINE` explicitly ends an idle rental. Both affect billing.

When `wait` returns, each machine rented for its runs reads `deleted` in
`etude machines`, its final logs are collected, and the on-demand's `--max` is
free for the next launch, so `launch; wait; launch; wait` needs nothing in
between. A machine rented by hand with `etude rent` is kept and does not hold
`wait`. A launch issued while a machine is still finishing says which one:
`at its machine limit (finishing: gpu-41840a01c3b1)`; `etude wait` clears it.

## Fetching a run's files

```text
$ uv run etude fetch experiment/base@0 model.json fetched
fetched/model.json
$ uv run etude fetch experiment/base@0 nope.json fetched
etude: run experiment/base@0 has no output 'nope.json'; it has: model.json, summary.json
```

`FILE` names a file the run kept: one its latest attempt wrote to
`ETUDE_OUTPUT_DIR`, or one in `run.out`, by its path below that folder (the
prototype's `coin.json`, for example). A name in both is
the attempt's file. Bytes are checked against the SHA256 and length the run
recorded before the file appears in `DIRECTORY`. A wrong name lists up to 20
of the names that exist. A storage failure is one line and exit status 1.

## Inputs and continuation

Declare inputs on the job rather than fetching arbitrary paths from another
host. Fingerprints verify bytes; the producer run enables host relay.
`TrainingJob(start=checkpoint)` transfers its model; `continue --run RUN --until
updates=4000` creates a new run carrying the parent's retained state and absolute
coordinates. An interrupted run retries from its latest complete save.

Use `source RUN` and `runs` for retained provenance and run history.
Run source is a git snapshot, and restarting on edited source is an explicit
resume/continuation decision. The [source reference](dev/launcher.md#a-runs-source-in-git)
explains recovery and retention.

## The server

The optional SSH server keeps the scheduler running independently of this
terminal. Configure it with `etude server` commands; `etude server --help`
lists setup, selection and run operations. Client calls use the selected server;
tracking reads and reports still read their explicit local or bucket source.
The server needs the provider credentials; jobs receive only scoped grants.

## Upgrading Etude

Run `etude database upgrade` explicitly when installing a newer release.
Ordinary reads never migrate your operational `launcher.sqlite` or silently
select a backup. Mixed client/worker releases must meet the release's reader
floor. Keep the running server's version aligned with its database.
See [upgrade and migration rules](dev/launcher.md#upgrading-etude) for active
runs and development databases.

## Launching from Python

Import `launch`, `queue`, `status`, `wait`, `logs`, `cancel` and `quote` from
`etude.launch.client`. They use the same selected server as the CLI.
`etude.spec` defines `Job`, `RunRequest`, `Input`, `Variable`, `Until` and runtime
preparation records. Machine configuration records live in `etude.launch.records`.
`etude.storage.Storage` is the tracking transport; the launcher's `Storage` is
its named bucket configuration.

For module ownership and dependencies, use the
[developer reference](dev/launcher.md#layout-and-dependency-rules).

For a bounded wait from Python, call `etude.launch.client.wait(timeout=120)`;
the CLI `etude wait` waits until its selected runs finish and their rented
machines are deleted.
