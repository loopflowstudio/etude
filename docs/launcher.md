# Running on machines

This guide is for authors submitting work to local, SSH or rented machines;
[training](training.md) and [evaluation](evaluation.md) own the job functions.

```sh
uv run etude database upgrade
uv run etude check experiment.py
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

Inside a declaration, `state.run("base@0").state` gives the training run's
state. Its `status` holds the launcher's retained phase, allocation ID
(`attempt`), machine, clocks and cost. Its `allocation_phase` gives a more
specific phase when known, including setup and a confirmed stop while the
launcher still holds the slot. Its `last_write` gives the latest UTC
modification time among its stored files, including copies. Evaluation runs
in `state.eval_runs` carry the same fields. A file-only run has `status=None`.
A queued run with no files has `last_write=None`, as does a listing without
modification times. A training summary's `address` preserves the spelling
used by the launcher or the stored declaration.

`Job` is also available from `etude.spec` for arbitrary shell commands. Its
`inputs`, `environment`, `prepare`, `runtime`, `setup` and `cleanup`
control execution. Typed jobs derive their command from an importable Python
function and validated Pydantic config; do not specify another command. Their
`extras` select project dependency extras for that function's environment.

## Checking an experiment

```text
$ uv run etude check exp.py
game.py:6: error: play must be an async def function
  fix: async def play(match: Match[Rules], agents: Sequence[Forward]) -> Tables:
exp.py:12: error: seeds must be nonempty and distinct
  fix: seeds=(1, 2, 3)
models.py:3: warning: game module game imports torch via models
  fix: define play in a lightweight module and move the import to load's module
exp.py: 2 errors, 1 warning
$ echo $?
1
```

Once the reported lines are fixed, `check` prints the runs and matches a first
launch would plan:

```text
$ uv run etude check exp.py
exp.py: ok, as a first launch sees it (no stored runs)
training  3 runs, up to 3 machine-hours, $15.00
  exp/base@1  max_hours 1  max_usd 5.00
  exp/base@2  max_hours 1  max_usd 5.00
  exp/base@3  max_hours 1  max_usd 5.00
eval
  ladder   round robin  3 entrants     3 matches  3 games
  panel    benchmark    0 checkpoints  0 matches  (none stored yet)
next: etude launch exp.py --quote
```

`etude check FILE [--experiment NAME] [--source DIR] [--format text|json]`
runs offline on this computer—with no provider key, bucket, database, git or
SSH call—and shares the runtime's own validation rules across syntax and
renamed declarations, `training_jobs()`, `eval_jobs(state)`, `report()`, and
(for shared-inference eval jobs) a child-process check that the game module
does not import heavy model libraries. Pass any of the experiment file's own
flags after `FILE`. A plain file defining only `train(config)` checks its
signature and default config as a local recipe (`etude train FILE`; nothing to
launch).

- **Offline state.** By default `check` evaluates declarations as a first
  launch sees them (no stored runs, declared training runs queued). Pass
  `--source DIR` to read a local runs directory instead. Bucket URIs are
  refused: use `etude launch FILE --quote` to check against a bucket and price
  capacity.
- **Budgets and warnings.** Jobs without `max_usd` are noted as
  `no max_usd: owned pools only` (owned pools and local runs accept them;
  renting refuses them). Heavy game-module imports are reported as warnings and
  keep exit status `0`.
- **JSON and exit codes.** `--format json` prints one JSON object (`file`,
  `experiment`, `ok`, `issues`, `training`, `evals`, `source`) on stdout.
  Exit status is `0` when there are no errors, `1` when the file has errors,
  and `2` when nothing was checked (missing file, non-`.py` path, bucket
  `--source`, or invalid flag).
- **Same fixes at runtime.** `launch`, `queue`, `eval` and `smoke` print the
  same `FILE:LINE` location and `fix:` line when they refuse a declaration.

Run `uv run etude guide` for an offline summary of Etude's conventions, the
`check` → `launch --quote` → `launch` → `wait` workflow, a minimal experiment
file and the current command list.

## Pools and owned machines

```sh
uv run etude pool add lab
uv run etude machine add mini me@mini.local --pool lab
uv run etude launch experiment.py --pool lab
```

A pool contains machines you own. `--cost-usd` and `--expires-at` record a paid
owned window when needed; expiry never deletes an owned host. An SSH host needs
Python 3.12, uv, git and working SSH credentials. When `~/.ssh/config` leaves
`ConnectTimeout` unset (`none`) or `ServerAliveInterval` at `0`, Etude passes
`ConnectTimeout=30` and `ServerAliveInterval=20` on SSH calls and leaves
`ServerAliveCountMax` to your config (OpenSSH defaults to 3, so a silent host
drops as unreachable in about a minute). These detect a dead peer without
capping a live command; set positive values in `~/.ssh/config` to override them:

```text
Host mini.local
    ConnectTimeout 15
    ServerAliveInterval 30
    ServerAliveCountMax 4
```

The job uses a separate Python environment from Etude's client. `--pool lab
--rent gpu` uses available owned capacity first, then rents. Machine and rental
facts are separate from scientific run identity.

If starting a session on an owned SSH host loses its reply, Etude checks the
host directly: an install or worker already running there is recovered and
tracked without starting a second copy, while an unclaimed session is fenced
and freed at once so queued work can use the machine even with no startup
cutoff. If claiming the session fails on the host (for example, on a full
disk), the host replies with a refusal, Etude settles the session as refused
immediately in that SSH call without leaving the machine `stopping`, and skips
that machine for 60 seconds so healthy machines in the same pool can take the
work.
Cancelling a run while its owned host is still setting up (installing,
or fetching and restoring inputs from storage) stops that setup and frees the
host once stopped, even if a full disk prevents writing the end marker. A
failed or killed install also frees the host. If the host is unreachable, Etude
waits until it can check. A launch that cannot reach an owned host fails without
recording the run; that host stays held until it answers, and the same run can
be launched on another pool at once.

On an SSH host with bucket storage, Etude tracks the courier from input fetch
through final upload. The host stays occupied while the courier delivers the
last files and logs, even after the job exits. If the courier dies, including
during the start receipt upload or a host reboot, Etude checks the host, stops
any remaining worker, and releases the machine for queued work.

After an attempt finishes on any host, Etude deletes its working trees (`src/`,
`prepared-source/`, `prepared-inputs/` and `inputs/`) while keeping its
outputs, logs and session journal. With bucket storage, Etude also removes an
attempt's local `outputs/` once delivery is confirmed and deletes the session's
`sources/`, `inputs/` and remaining attempt directories after confirmed final
delivery, keeping only small control files; unconfirmed delivery leaves local
files in place. On an SSH host without bucket storage, Etude removes the
session's staged `sources/` once its attempts finish, while keeping outputs and
logs on the host because its disk is the only copy.

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
limits. Credentials never go in those records. Set up bucket credentials
as described in [tracking](tracking.md#storage). R2 uses `--provider r2`.

| Provider | `--kind` | Launcher environment | `--gpu` example | Spot |
| --- | --- | --- | --- | --- |
| RunPod | `runpod` | `RUNPOD_API_KEY` | `"NVIDIA L4"` | no |
| Vast.ai | `vast` | `VAST_API_KEY` | `"RTX 4090"` | yes |
| Lium | `lium` | `LIUM_API_KEY`, `LIUM_SSH_PUBLIC_KEY` | `"RTX 5090"` | yes |
| AWS EC2 | `ec2` | the `AWS_*` keys your S3 storage uses | `L4` | yes |
| Lambda Cloud | `lambda` | `LAMBDA_API_KEY`, `LAMBDA_TERMINATE_KEY` | `"A10 (24 GB PCIe)"` | no |
| Google Colab | `colab` | `gcloud` ADC (`colaboratory` scope) or [Colab OAuth credentials](dev/providers.md#google-colab) | `L4` | no |

Each provider spells GPU names its own way, and its account needs some setup
before the first rental; the [provider reference](dev/providers.md) has both.
There is no cross-provider fallback.

### Spot machines

```text
$ uv run etude ondemand add l4 --provider aws --gpu L4 --max 1 --max-usd-per-hour 0.90
$ uv run etude ondemand add l4-spot --provider aws --gpu L4 --spot --max 1 --max-usd-per-hour 0.70
$ uv run etude availability l4
l4  g6.xlarge: L4 with at least 4 vCPUs, 16 GB at $0.8226/hour (ceiling $0.90)  available  0 of 1 in use  rentable now  asked 2026-11-17 19:02:11 UTC
$ uv run etude availability l4-spot
l4-spot  g6.xlarge: L4 with at least 4 vCPUs, 16 GB at $0.6510/hour, spot (ceiling $0.70)  available  0 of 1 in use  rentable now  asked 2026-11-17 19:02:14 UTC
$ uv run etude launch experiment.py --rent l4-spot --max-usd 2
$ uv run etude status experiment/base@0
experiment/base@0  base  running, steps 1400, ~$0.41, machine l4-spot-41840a01c3b1 (g6.xlarge: L4 with at least 4 vCPUs, 16 GB), 1 of 3 attempts used
```

`--spot` on `ondemand add` rents the provider's cheaper machines that it may
take back at any moment. Everything else is the same: the template, the
ceiling, `--quote`, the cost cap and cleanup. Budgets reserve the full quoted hourly rate.
On Vast, for example, a $0.333333/hour compute bid plus $0.011111/hour
for disk reserves $0.344444/hour. If Vast pauses the instance, Etude destroys
it to end storage billing.

When the provider takes a spot machine back, the run is retried on a new one
from its latest delivered save; work since that save is done again. Losing a
spot machine does not count toward the job's `max_attempts`, so the run's
`max_hours` and `max_usd` are what bound a run that keeps losing machines.
Save often enough that an attempt makes progress between losses.

A provider that rents no spot machines accepts the on-demand and answers in
`availability`; runs queued on it wait with that reason:

```text
$ uv run etude availability pod-spot
pod-spot  none available: Etude rents no spot machines from RunPod  0 of 1 in use  not rentable now  asked 2026-11-17 19:03:40 UTC
```

`--max-usd` bounds the submitted allocation; each job also has its own cap.
Quotes rent nothing. A rental has an independent deadline guardian, but a
provider outage can delay deletion and billing can exceed admission estimates.
Storage charges are separate. See [limits](limits.md).

`availability` reports the provider's stock signal; it reserves nothing. When
nothing can be rented now it says why in the provider's terms: no stock, an
account quota (`AWS quota 'All G and VT Spot Instance Requests' is 0 vCPUs;
g6.xlarge needs 4`), or a GPU name the provider does not use, followed by the
names it does. When
the provider then declines to make the machine, launch fails with the
provider's own words and records no run:

```text
$ uv run etude launch experiment.py --rent gpu --max-usd 2
etude: launch 215b6457f928 for experiment/base@0: cannot launch on 'gpu': provider refused: There are no longer any instances available with the requested specifications. Please refresh and try again.; etude queue would wait
```

Nothing was rented and nothing needs cleaning up: `etude machines --all` lists the
refused machine as `unavailable, nothing rented DATE TIME UTC`. Default listings and
`etude ondemands` omit it;
it does not count toward `--max`, and the same run can be launched again at once,
on this on-demand or another. A queued run
keeps the reason in `etude status` and its on-demand is asked again no sooner
than a minute later. Capacity refusals do not count toward the job's `max_attempts`.
While its hours and dollars remain, the run stays queued until capacity is
admitted or the run is cancelled. A reply that does not settle whether a machine
was made (a timeout, a dropped connection, a provider error) is not a refusal:
Etude looks that machine up before asking for another.

### Sharing a machine

```text
$ uv run etude ondemand add l4x3 --provider runpod --gpu "NVIDIA L4" \
    --vcpus 6 --memory-gb 48 --disk-gb 40 --max 1 --max-usd-per-hour 0.80 --slots 3
$ uv run etude launch sweep.py --rent l4x3 --quote
sweep/base@0  rent NVIDIA L4 with at least 6 vCPUs, 48 GB at $0.72/hour, 3 slots with sweep/base@1, sweep/base@2  planned rental cost $0.30
sweep/base@1  rent NVIDIA L4 with at least 6 vCPUs, 48 GB at $0.72/hour, 3 slots with sweep/base@0, sweep/base@2  planned rental cost $0.30
sweep/base@2  rent NVIDIA L4 with at least 6 vCPUs, 48 GB at $0.72/hour, 3 slots with sweep/base@0, sweep/base@1  planned rental cost $0.30
planned first-wave rental cost: $0.90; nothing rented
job rental budget: $0.90
Rental costs assume deletion on time. Provider delays can exceed the budget; external storage charges are excluded.
$ uv run etude launch sweep.py --rent l4x3
$ uv run etude status
sweep/base@0  base  running, steps 1200, shared by 3 runs, ~$0.09, machine l4x3-41840a01c3b1 (NVIDIA L4 with at least 6 vCPUs, 48 GB), 1 of 3 attempts used
...
```

`--slots N` on `ondemand add` or `pool add` runs up to N runs at once on each
machine (default 1). Runs share the machine's GPUs and CPUs whole; nothing is
partitioned, so pick N for what the job leaves idle. Each run keeps its own
outputs, log, record and delivery.

Runs placed together on a rental each fund only their share of it — split
equally among the runs actually placed there, not the configured `--slots`. In
the example above, three 1-hour seeds with `max_usd=Decimal("0.30")` each share
one $0.72/hour machine (1.25 hours with the default 15-minute setup window,
$0.90 total) and all three launch for their full hour. One seed alone on the
same 3-slot on-demand would need all $0.90 and is refused by `launch` and
`--quote` (`job dollars do not cover the hours`). When a full group fits and
another underfunded run waits behind it, the funded group launches for its full
window and the remaining run stays queued. Each quote line gives the placed
run’s share. The first-wave total also includes any extra rental time funded by
runs still waiting.

Each run placed on a rental holds a **place** on the machine until its
attempt's time limit, while at least one run is still active on the machine.
Occupied time is split equally among the runs holding a place then, so a peer
that fails, is cancelled or finishes early never raises what the remaining runs
pay. The trade-off: a run that leaves early keeps paying its share while peers
are still working on the machine, so its dollars in `etude status` keep rising
up to its planned share — including a short run with a loose `max_hours` (such
as an eval beside training). When the last active attempt finishes, all places
end. A later batch starts paying from its own placement.

Queued runs placed later by `wait` or a scheduling pass still size each
attempt's time limit at the machine's full hourly rate (getting a shorter
`over_budget` attempt when remaining dollars would not cover the machine
alone), while actual spend is split among the places held on the machine. Use
`etude launch` when each seed's `max_usd` covers only its shared slot. See
[Several runs on one machine](dev/providers.md#several-runs-on-one-machine) for
window sizing, provider rounding and retries.

## Queue, launch and wait

```text
$ uv run etude queue experiment.py --resume --job base --seed 0 --until updates=4000 --rent gpu
experiment: state from s3://YOUR_BUCKET/etude/launch: 1 training run, 0 eval runs
experiment/base@0  new, queued
$ uv run etude queue experiment.py --watch 60 --rent gpu
experiment: state from s3://YOUR_BUCKET/etude/launch: 1 training run, 0 eval runs
experiment/base@0  running
eval prediction: 100 new, 0 running, 0 failed, queued experiment/evals-a1b2c3d4e5f6
```

`launch` and `queue` accept the same experiment options (`--job`, `--seed`,
`--resume`, `--until`, `--watch`, `--max-usd`, `--source`, `--retry-failed`).
They differ only in whether capacity is required right away (`launch`, plus
`--quote`) or may wait in the queue (`queue`).

`--resume` gives one selected run another attempt on the current source; select
`--job` and `--seed` when the file declares several. `--until` requires
`--resume` and raises that run's absolute target. `--resume` cannot be combined
with `--watch` or `--quote`. `--watch SECONDS` repeats passes until Ctrl-C so
newly landed checkpoints queue or launch their eval matches automatically.

When `--source` is omitted on `launch`, `queue` or `etude eval`, Etude derives
the target's delivery storage and lists only this experiment's prefixes (plus
any other experiment named by a cross-experiment relative slot), avoiding a
full scan of historical experiments in a shared bucket. Passing `--source`
explicitly lists that whole prefix so experiment-folder sources
(`s3://YOUR_BUCKET/etude/launch/experiment`) and nested directories still work.

| Command | What it does |
| --- | --- |
| `check FILE.py` | Validates declarations offline and prints planned runs, matches or fixes. |
| `guide` | Prints conventions, workflow, a minimal experiment file and all commands. |
| `queue FILE.py` | Records intent and returns ids; waits for no provider (`--watch SECONDS` loops passes). |
| `launch FILE.py` | Acquires capacity, starts what fits and queues the rest; fails with no runs recorded if none fit. |
| `launch FILE.py --quote` | Plans without submitting or renting. |
| `status`, `machines`, `logs` | Reads retained evidence; prints freshness/incompleteness. |
| `wait` | Advances work until the selected runs finish and the machines rented for them are deleted. |
| `logs --run RUN --follow --timeout SECONDS` | Collects new logs and follows them. |
| `view [PATH]` | Shows run progress and the machines holding each run. |
| `cancel RUN` | Requests cancellation and cleanup. |
| `fetch RUN FILE DIRECTORY` | Copies one file the run wrote into `DIRECTORY` and verifies its fingerprint. |

Queued work needs a scheduling loop: `wait`, a later scheduling command, or the
optional server. An unattended queue does not run itself without one.
`rent NAME --hours H --max-usd D` explicitly rents capacity;
`terminate MACHINE` explicitly ends an idle rental. Both affect billing.
Any machine `etude machines` lists as rented can be terminated, including one
still `starting` whose provider details Etude could not read.

When `wait` returns, each machine rented for its runs reads `deleted` in
`etude machines`, its final logs are collected, and the on-demand's `--max` is
free for the next launch, so `launch; wait; launch; wait` needs nothing in
between. A machine rented by hand with `etude rent` is kept and does not hold
`wait`. A launch issued while a machine is still finishing says which one:
`at its machine limit (finishing: gpu-41840a01c3b1)`; `etude wait` clears it.
`etude wait` and the server loop check every second after a run or machine
changes state, then back off to every 10 seconds while active state is steady,
so a finished or cancelled run can take up to 10 seconds to notice; an idle
server loop caps its backoff at 4 seconds so newly queued runs start promptly.

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
tracking and reports read the chosen directory or bucket. A local `etude view`
polls the selected server for run placement, scheduler heartbeat and requested
consoles at most once every 30 seconds. Pass `--source` to read the corresponding
run files. For a selected server, `/machines` names the host and points to
`etude machines`. With a local launcher, that page shows machine windows and
costs and links each occupied slot to its run. See the [live view guide](tracking.md#live-view).

The server holds the provider credentials. Jobs receive only scoped grants.

## Upgrading Etude

Run `etude database upgrade` explicitly when installing a newer release.
The live machine inventory adds indexes to the launcher database. Run the
upgrade on the computer holding that database, including a selected server.
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
it uses the same 1–10 second backoff by default (`every=None`), or pass a
positive `every=` in seconds for a fixed polling interval. The CLI `etude wait`
waits until its selected runs finish and their rented machines are deleted.
