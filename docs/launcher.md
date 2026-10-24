# The launcher

```sh
etude pool add lab                                   # machines you own
etude machine add mini me@mini.local --pool lab      # cost $0, never expires
etude ondemand add runpod-cpu8 --provider runpod --vcpus 8 --memory-gb 16 \
    --max 2 --max-usd-per-hour 0.40                  # a template and a budget
etude availability --rent runpod-cpu8                # what it would rent, at what price
etude launch job.py --rent runpod-cpu8 --quote       # machines and maximum spend; rents nothing
etude launch job.py --pool lab --rent runpod-cpu8    # the Mini first, then rent
etude machines --all                                 # owned: cost, expiry; rented: hardware, cost
```

A launch says where its runs may go. A **pool** is a named set of machines you
own. An **on-demand** is a machine template and a budget to rent with: how
many machines at once and the most to pay per hour. `--pool P` uses only P's
machines, `--rent R` only rents from R, and both use a free machine of P
before renting from R. A run that cannot start yet waits.

| Command | Meaning |
| --- | --- |
| `etude queue FILE.py [--pool P] [--rent R]` | Add runs. They start when a machine is free or can be rented; lack of capacity leaves them waiting. |
| `etude launch FILE.py [--pool P] [--rent R]` | Acquire machines for the first runs (up to cohort parallelism), then record the runs. Failure records no runs. |
| `etude launch FILE.py ... --quote` | Print the machines the first wave would take and the most they could cost. Rents nothing. |
| `etude availability --rent R` | Ask R's provider what it would rent now, at what price. Rents nothing. |
| `etude rent --ondemand R --hours H` | Rent a machine for a window of time; it takes queued runs. |

Pools and on-demands share one namespace: a name refers to one of them.
Launch claims free machines and asks the provider to confirm any new machines
before recording runs. Each pool and on-demand has a placement lock with an
owner and a five-minute expiry; a launch holds the locks of everything it may
use, without holding a database transaction. Other pools, on-demands and
readers keep working. Machines are acquired with empty inboxes; only after all first-wave
machines are confirmed are runs and attempts recorded together and work delivered.
A refusal or unknown create reply is an error, with no runs recorded. Confirmed
machines made for the failed launch are deleted; unknown creates retain their
identity for lookup and cleanup. Machine acquisition and rental records and costs remain in history.
A crashed launch's lock expires, and the next scheduling pass cleans up its unused
machines. New machines also retain their independent deadline.

The guarantee is capacity at admission, not successful boot or training. Later
boot failure follows existing retry rules. Later waves use the same queue as
ordinary submissions. Launch checks the on-demand's machine limit,
availability, price ceiling and dollars for the requested hours before spending.

Queue submission only writes intent and never waits for a provider. The standing
`etude controller run` loop, `etude wait`, or another command's scheduling pass
matches waiting runs, visiting every pool before any on-demand, and rents
within each on-demand's limits. Keep that loop running
when the queue must move without further commands.
`etude continue` queues new runs that start from a parent's recorded
checkpoint, with absolute targets and the parent's pool and on-demand; lack of
capacity does not reject continuation.

| Record | The question it answers | Exists for |
| --- | --- | --- |
| **Pool** | Which machines do I own, and do they take new runs? | Owned machines |
| **OnDemand** | What do I rent, how many at once, and at what most per hour? | Rented machines |
| **Machine** | What is it and how did I get it: owned (what it cost, when it expires) or rented? | Every machine |
| **Rental** | How long is the paid window, at what price and cap, and what has it cost? | Rented machines only |
| **RunAttempt** | Which run is allocated to which machine, for what window, and how did it go? | Every machine |

Unfinished RunAttempts make a machine busy. A rental contains no run or startup
facts. Machine.kind is `local`, `ssh` or `rented`, with the provider account in
a separate field; an owned machine names its pool and a rented one names the
on-demand it came from. A Session is internal:
one start of Etude's process on a machine, and the journal folder it writes. A
rented machine has one session for its life; a machine you added has one per
start, one at a time.

An owned machine's cost is committed when it is added (`--cost-usd`, default
0) and it stops being yours at `--expires-at` (default never). Its cost never
limits placement: a free owned machine is always used before renting. At its
expiry it leaves its pool; an attempt still on it ends as lost with the machine
(`machine_lost`), and the run retries wherever it may go, which may mean renting.
Owned machines are never deleted. A machine bought for a window, such as SF
Compute's, is an owned machine with that window's price and end.

Python exposes `queue(job_or_run, pool, rent=...)` and
`queue_cohort(name, runs, pool, rent=..., ...)`, with the same cohort limits as
launch, plus `quote(...)` and `availability(rent)`. All use the configured SSH
controller. With neither a pool nor an on-demand named, a launch uses the
configured default, initially the pool `local`.

Spending is bounded by limits, not by a flag: the job's `max_usd` and
`max_hours`, the cohort's `max_usd`, the on-demand's price ceiling and its
limit on machines at once.

The `etude-sdk` package uses Python 3.12 and one shared dependency set for launching and tracking, installed with uv. It runs on this
computer, SSH hosts and RunPod machines, without any Etude account.

A **Job** specifies what runs, source commit, config, target and limits. It has
no seed or machine settings. A **Run** pairs a job with a seed and goes to a
pool, an on-demand or both. A **Cohort** launches runs together under shared limits. A **RunAttempt**
allocates a run to a machine for a bounded execution window. `status` shows the machine.

`OnDemand.template` is a **MachineTemplate** that chooses the provider, GPU,
size, image and data center; the on-demand holds the budget. A **Provider**
holds account settings; a **Storage** names a bucket and prefix. Credentials
stay outside these records.

## Start here

```python
# job.py
from etude.jobs import training
from etude.launch.records import Job

from train import TrainConfig, train  # train(config: TrainConfig) -> None


def runs() -> Job:
    return training(train, config=TrainConfig(lr=1e-3), max_hours=0.1)
```

A job always states `max_hours`. It needs `max_usd` only when it may rent,
that is, when it names an on-demand. Sending a job with no `max_usd` to an
on-demand is an error that says so.

```sh
etude launch job.py
etude status
etude wait
etude logs --run RUN
etude fetch RUN checkpoint.pt out
```

With neither `--pool` nor `--rent`, launch uses the configured default,
initially the pool `local`. Change it with `etude default --pool P --rent R`
(either or both). If the default pool is off, launch rents from the default
on-demand, or asks for an explicit one. The built-in machine is already added to `local`.
`etude pool off local` stops new placements; `etude pool on local` resumes them.

Add a host using the address accepted by OpenSSH:

```sh
etude machine add shed me@shed.local
etude machine add bench bench --pool lab --storage west
etude machine add window me@window --pool lab --cost-usd 120 --expires-at 2026-11-01T00:00:00
etude launch job.py --pool shed
```

An omitted `--pool` creates a pool bearing the machine's name. A named pool
must already exist (`etude pool add lab`). Hosts need Python 3.12, uv, an SSH server
and working key authentication. OpenSSH configuration supplies identity files,
ports and host aliases. Hosts retain normal host-key checking.
The launcher copies its program and the frozen source, starts `etude worker`
detached and reads the journal over SSH. The transferred private package and its locked public dependencies are installed
on first use into `~/.etude/programs/DIGEST/.venv`, reused for that exact code.
Etude is never fetched by name from an index. The job runs in its own environment;
Etude's dependencies do not constrain it. Records default to
`~/.etude/sessions/` on the host. Missing uv is an error naming uv.

A machine you own is reached by SSH. A machine you rent starts itself.
The standard-library guardian starts first, using bare Python; its deadline and
pod deletion do not depend on installing or importing client dependencies.

Owned hosts keep records on their disk unless `--storage` is supplied. A
storage is an S3 or a Cloudflare R2 bucket, with keys from the launcher's
environment:

```sh
etude storage add west --bucket BUCKET --prefix etude/launch --provider s3 --region us-west-2
etude storage add r2 --bucket BUCKET --prefix etude/launch --provider r2
```

```python
from etude.launch.records import Storage

Storage(name="r2", bucket="BUCKET", prefix="etude/launch", provider="r2")
```

| Provider | Launcher environment |
| --- | --- |
| `s3` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` |
| `r2` | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_API_TOKEN` |

Machines writing to a storage receive scoped links and a grant for each folder
they write, valid thirteen hours: their session's `out/` and the folder of
each run they are given, whose grant comes with its work. They never receive
the launcher's own account or bucket keys. A grant holds temporary keys that
can write, upload in parts and delete under that folder and nowhere else, so a
checkpoint of any size is sent in 64 MB parts without being held in memory,
and metric blocks that a larger block has replaced are deleted.

- **R2** issues the keys through Cloudflare's API with `R2_API_TOKEN`; they can
  also read the folder. R2 has no upload forms, so a launch whose keys cannot
  be issued fails before anything is rented.
- **S3** issues them only when the launcher's keys belong to an IAM user
  allowed `sts:GetFederationToken`, so a grant also holds a write-only form.
  Without keys, or once they expire, the form alone is used: files up to 5 GB,
  read whole into memory, and nothing deleted.

RunPod supplies a separate self key for deletion. Job environments exclude all
of these keys; jobs currently run under the same account as the worker, so the
temporary keys are kept in the courier's memory and never written to disk.

## Typed jobs

```python
from decimal import Decimal

from etude.jobs import evaluation, training
from etude.launch.records import Input

job = training(train, config=TrainConfig(lr=1e-3), max_hours=6, max_usd=Decimal(30))
weights = Input(
    name="updates-500",
    key="etude/launch/RUN/checkpoints/updates-500",
    sha256="…",
    size_bytes=1048576,
)
check = evaluation(
    progress, experiment="ppo", play=play, checkpoint=weights, max_hours=0.5, max_usd=Decimal(1)
)
```

The job type decides the command. Both return a `Job` whose command imports
the function in the job's own environment and calls it with the config:
`uv run python -m etude.call --function MODULE:NAME` in the checkout, which is
then a uv project depending on `etude-sdk`, or `"$ETUDE_PYTHON" -m etude.call ...`
with a `runtime`, whose interpreter must already hold `etude-sdk` and the
project's dependencies. `etude.call` reads `ETUDE_CONFIG`, validates it into the
type of the function's one parameter and calls it in `ETUDE_SOURCE_DIR`. It
imports nothing else of Etude before the function's own module.

- **Functions** are named by import path, so they are defined at the top of a
  module: not in the job file, `__main__` or another function. The job's
  `repo` is the git checkout the function's file is in (for an evaluation,
  `play`'s) unless `repo` is given; `repo=None` names a function installed in
  the job's environment. The module is imported from the folder its name
  starts in, so a job file beside `train.py`, a package at the root and a
  `src/` layout all work.
- **Configs** are Pydantic models: the model's defaults are the recipe, and its
  JSON is the job's config, so it is part of the job's identity. A config of
  another type, or one that would not validate back into the parameter's
  type, raises `LauncherError` when the job is made, before any quote, launch
  or rental.
- **Every other `Job` field** (`max_hours`, `max_usd`, `until`, `inputs`,
  `runtime`, `setup`, `name`, ...) is a keyword of both (`JobOptions`).
- **The job file imports the function**, so `etude launch` must be able to
  import its module. When it imports training libraries at its top, launch
  from the project's environment: `uv run etude launch job.py`.
- **An evaluation** holds a [tournament](evaluation.md#tournaments) as one eval
  run, which is the launched run. A benchmark's `checkpoint` is an `Input`: it
  reaches the machine verified by digest and fills every model seat, where
  `play` finds the file at the agent's `uri`. A round robin takes `enter=`
  agents. Holding nothing new (batch) is refused, since a launched job reads no
  other eval run. `workers=` plays matches in parallel processes. The job's
  environment needs `etude-sdk[eval]`.
- **Commands are for work that is not Python**:
  `Job(command="./simulate --games 1000", max_hours=1)` runs by `sh` in the
  checkout with the `ETUDE_*` environment below. Python work is a typed job,
  so there is one way to run it.

Not built yet: evaluating as training goes,
`training(..., evaluations=[evaluation(progress, play=play, ...)], every=5)`,
where the controller watches the training run's checkpoints and launches the
evaluation for every fifth one and the last.

## Rented machines

```sh
etude storage add west --bucket BUCKET --prefix etude/launch --provider s3 --region us-west-2
etude provider set runpod --kind runpod --storage west
etude ondemand add l4 --provider runpod --gpu "NVIDIA L4" \
    --vcpus 6 --memory-gb 48 --disk-gb 40 --max 1 --max-usd-per-hour 0.80
etude launch job.py --rent l4
```

A template without `--gpu` rents RunPod CPU pods: the cheapest secure-cloud CPU
flavor with at least `--vcpus` (rounded up to a power of two, RunPod's sizes)
and `--memory-gb`, priced per vCPU and capped like a GPU pod. CPU pods boot the
same default image. Their disk defaults to 40 GB, or 10 GB per vCPU when smaller.
On a CPU pod `os.cpu_count()` reports the host's CPUs; size worker pools from
`len(os.sched_getaffinity(0))`.

```sh
etude ondemand add cpu --provider runpod --vcpus 8 --memory-gb 16 --max 2 --max-usd-per-hour 0.40
etude availability --rent cpu
etude launch job.py --rent cpu --cohort evals --parallel 2 --max-usd 1 --quote
etude launch job.py --rent cpu --cohort evals --parallel 2 --max-usd 1
```

`availability` asks the provider, so it is a wait: it prints what would be
rented, the price and ceiling, how many of the on-demand's machines are in use
and when the provider answered; `--timeout` bounds it. `--quote` places the
first wave as launch would, asking the provider for prices, and prints one line
per run: an owned or already rented machine it would use, or the hardware it
would rent and the most that window could cost. It then prints the maximum
spend of those machines and the job and cohort caps on the whole launch. It
records nothing and rents nothing; without capacity it fails as launch would.

Free existing machines are used before renting another. With `--pool` and
`--rent` together, runs spill onto rented machines when owned ones are busy.
Change automatic renting with `etude ondemand max l4 2`; `etude ondemand max l4 0`
allows only machines rented by hand.
A zero limit still permits an explicit `rent`; a positive limit bounds
concurrent rented windows. Lowering the limit leaves current windows intact.
The limit and ceiling belong to the on-demand, so every launch that names it shares them.
The default RunPod image is pinned stock
`runpod/pytorch@sha256:4d1721e62b56d345c83b4fd6090664be6daf9312caab5b2e76f23d8231941851`.
It needs Python 3.12 on PATH. uv must also be available; when absent, boot
fetches uv 0.8.22 for Linux x86_64 and verifies its SHA-256
`741ff1f5742c5a4a25d2f829e8395355e43f7a5ae2ebc6368e9ae2df0efb69cf`
before installing it. The guardian starts before either download.

The launcher stores its private package in the on-demand's storage under
`PREFIX/programs/DIGEST.zip`, once per digest. Boot fetches it by a scoped link,
checks its digest, installs with `uv sync --frozen` into
`~/.etude/programs/DIGEST/.venv`, then runs `etude worker --bootstrap URL`.
Etude is never fetched by package name from an index. The client and each job
use separate environments. The provider start command contains only a small
standard-library bootstrap; no Etude files need to be present in the image.

An on-demand's `--image` or `etude ondemand image` selects a user's own pinned image for
training dependencies installed ahead of time. It must meet the same Python
and uv requirements and needs nothing of Etude. `--registry` still supplies a
provider pull credential for a user's private image. The stock image needs no
registry credential.

To inspect a new rental over SSH, add your public key (for example, the contents
of `~/.ssh/id_ed25519.pub`) to your RunPod account's SSH keys before renting.
Open the pod's **Connect** tab and copy its **SSH over exposed TCP** command:

```sh
ssh root@POD_IP -p MAPPED_PORT -i ~/.ssh/id_ed25519
```

Etude requests `startSsh` and exposes `22/tcp`; RunPod supplies the account's
public keys as `PUBLIC_KEY`, using the account context of `RUNPOD_API_KEY`.
For a team, register each person's public key in that team's RunPod credentials;
each person keeps their private key. No separate Etude key setting is needed. Boot starts
key-only OpenSSH after the guardian and before downloading the client, so SSH
can help inspect installation problems. Custom images need `ssh-keygen` and
`/usr/sbin/sshd` installed. Missing keys or SSH startup failures are reported in
the pod logs and do not stop worker startup. Launch and journal access continue
to work independently of SSH. Etude configures SSH at boot; this change does not
modify existing pods or synchronize later key changes. SSH access does not extend
a rental's deadline.

Owned machines and existing rental windows come first. A rented machine
always needs storage. A launch with `--pool` and `--rent` can still use its
owned hosts when the on-demand has no storage; waiting runs report missing
storage when renting is needed.
A rental made by hand starts itself with an empty inbox:

```sh
etude rent --ondemand l4 --hours 0.5 --max-usd 0.40
etude launch job.py --rent l4
etude machines
etude terminate --machine MACHINE
```

`etude machines` is the one list. Each owned machine shows its pool, its
committed cost and its expiry (or that it never expires). Each rented machine
shows its on-demand, the hardware the provider was asked for (a GPU type with the minimum vCPUs
and memory its host must have, or a CPU flavor and its exact vCPUs), the end of its window and its cost; `etude status` names it beside the
run's machine. `etude machines --all` includes rented
machines whose window has ended, so late bills stay visible. `terminate` takes
the machine's name. `etude pools` shows each pool's machines and `etude ondemands` each
on-demand's template, budget, machines and safeguards; both mark the default.

A rental made by hand boots with no runs, then takes work from its bucket
inbox. There is one rental record and execution path. A window made by hand stays
until its deadline or termination; one rented for queued work ends when no work is left.
Every rented machine takes waiting runs that fit its remaining time. The
controller delivers each next run; keep its loop running to drain the queue.
There is no run-count packing setting. A machine rented for queued work ends when no
waiting run fits; a manually rented one waits until its deadline or termination. Terminate refuses
while it still has unfinished work.

The launcher needs `RUNPOD_API_KEY`, `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` from the configured secret manager. No secrets enter
SQLite. An on-demand takes its provider's storage when added and keeps it. A storage
cannot change; add another name. `--data-center` selects a RunPod data center;
omitting it lets RunPod place the machine. `ondemand image` affects future machines.

## The worker and its bucket

The worker is the one command a machine runs, whether started locally, over
SSH, at boot, or by someone on that machine:

```sh
etude worker DIRECTORY
etude worker --bootstrap URL --root /path/to/sessions
```

`DIRECTORY` already contains the rental packet and source files. A
bootstrap link supplies the packet, source links, inbox link and the grants
for its folders. The worker opens no launcher database. A bootstrap worker carries records
to the bucket; on RunPod it deletes its own pod when delivery finishes, with an
independent guardian bounding its lifetime. Supplying links for machines Etude
did not make is outside this build.

The bucket serves two purposes: results survive a deleted on-demand machine,
and it is the only communication channel for a rented machine. The controller
writes work and stop requests to the inbox; the worker reads it and uploads
logs, progress and outputs. No inbound connection is needed. This computer and
owned SSH hosts need no bucket; an owned host can optionally use one for records
that outlive its disk. Only Amazon S3 is supported.

## Retained launch evidence

```sh
etude logs --run baseline-seed-7
etude logs --run baseline-seed-7 --follow --timeout 60
etude logs --run baseline-seed-7 --job-only     # the job's output alone
etude logs --machine MACHINE
etude status baseline-seed-7 --phases
```

A run log includes launch preparation, shared machine startup, every execution
attempt and cleanup. It keeps that meaning before admission and after machine
deletion. `--cohort NAME` selects the admitted members of a cohort. Machine logs
contain operational output; run logs select only that run's job output and
attempt diagnostics. The worker's log says what changed, such as an inbox with
new work or a delivery that failed, not every poll: an inbox read that finds
nothing new leaves no line and no usage record, and every other transfer goes
to the session's usage journal (`etude usage`), not the log.

`Run(id="baseline-seed-7", job=job)` chooses a scientific identity. Failed admission
leaves independent launch evidence, without creating a Run or RunAttempt or
consuming that identity. A corrected launch can reuse it. An already-admitted
identity is a conflict; an unresolved acquisition remains fenced until confirmed
refused or deleted. A new launch ID cannot bypass that fence. `LaunchError.launch`
and `.runs` identify the retained evidence when admission fails. Cohort admission
remains atomic.

Snapshots refresh journals with no timeout by default; pass `timeout=30` in Python
or `--timeout 30` on the CLI to bound the complete read. Follow
uses the same selection and absolute positions in each sanitized producer, stops
at its time limit, and never schedules or retries work. It prints new evidence
without replaying already seen output. Job exit alone does not close startup or
cleanup producers. A queued run or a missing final stream is incomplete.

Headers distinguish output not received, a silent producer, retrieval failure and
retention truncation. A failed refresh returns the last received content with its
collection time. Startup snapshots and controller log caches retain at most 1 MiB
of text per stream: opening milestones and a recent tail, with omitted positions
marked. Worker observations retain producer timestamps separately from controller
observation times; clocks on different machines do not establish a total order.

Bootstrap uses a write-only capability for one log object, independent of reading
the bootstrap packet or installing dependencies. Each startup producer publishes
at most once every 15 seconds and once at closure, under `log_seconds` on its pool or on-demand
(default `None`) and the remaining rental allowance. The guardian starts first and remains independent of publication.
A storage outage or death before bootstrap runs can leave an explicit evidence
gap. Reading logs never needs a live machine after its bucket evidence was
published or its output cached by the controller.

```python
from etude.launch.client import diagnostics, logs

snapshot = logs(run="baseline-seed-7", timeout=30)
for stream in snapshot.streams:
    print(stream.producer, stream.attempt, stream.received, stream.complete)
    print(stream.text)
for event in diagnostics(run="baseline-seed-7"):
    print(event.observed_at, event.operation, event.message)
```

Diagnostics and `status --phases` read retained database facts without provider or
bucket calls. With an SSH controller selected, those facts live on the controller.
Preparation intent is recorded there before the submitting client publishes its
source. A disconnected submitting client can leave a preparing launch; it is
reported as unresolved and does not authorize automatic resubmission.

This prototype schema is version 2. As with other launcher schema changes, an
older database is refused; there is no migration path in the prototype.

## The controller

The controller holds the database and matches waiting runs to machines. No
separate controller is required: without a setting, CLI commands use this
computer's database and match while they run.

```sh
etude controller                     # selection, last loop time, waiting-work warning
etude controller set user@host       # an SSH address or configured alias
etude launch job.py --rent l4
etude logs --run RUN
etude fetch RUN checkpoint.pt out
etude controller unset               # use this computer again
```

The setting is a small `controller` file beside the local database, under
`ETUDE_HOME` (default `~/.etude`). Setting or clearing it needs no database and
moves no existing records. With a controller selected, CLI commands run there
through the user's `ssh`, except selection commands, `worker` and source
checkout, which run on the machine where invoked. The remote database's built-in `local` pool
is renamed `controller`, including the default selection if it named `local`.
It is never advertised as the client's local machine.

`etude controller set` installs this
client's own package over SSH, where it and its environment are kept by digest under
`ETUDE_HOME/programs`, and every later command runs that exact version. The two
sides therefore cannot be different versions; after the client changes, the
next command sends the new code first. The controller needs Python 3.12, uv
and working SSH keys. If it cannot take the program, `controller set` fails
with the reason and the setting is not changed.

Commands on a controller, and on any host reached by SSH, run through that
account's login shell. A bare SSH command gets a minimal PATH, which on a Mac
hides Homebrew's Python; the login shell gives the PATH a terminal there has.
`ETUDE_REMOTE_PYTHON` names a Python other than `python3.12`.

Provider and storage credentials belong in the controller's own environment;
the client sends none. How a controller obtains them is not settled: a command
arriving over SSH does not see secrets that a terminal session there loads.

The functions in `etude.launch.client` (`queue`, `launch`, their cohort forms,
`status`, `wait`, `cancel`, `logs` and `fetch`) use the
selected controller and return the same Python values. An explicitly
constructed `Scheduler` remains the owner of a local database.

Launch reads the job file and config on the client, freezes and publishes source there, and
sends frozen runs and one source archive per repository/commit. The controller
stores archives under `ETUDE_HOME/sources` and can launch or continue without a
repository checkout. Logs stream back over SSH. Fetch copies to the client's
chosen directory and checks the returned digest before replacing its output.
A lost SSH reply is an error; launch is not automatically retried.

Run the standing loop on the controller under a process supervisor:

```sh
etude controller run
```

It uses the same matching operation as `wait`, but remains alive through an
empty queue. It records the last completed pass in `controller-heartbeat` beside
the database. `controller` and `status` warn when work is waiting and no pass
has completed in 30 seconds. Slow provider operations may also cause this
warning. `wait` still exits when its runs finish. Workers read their own disk or
bucket inboxes; they never need a reachable address for the controller.

## Safeguards and bills

`etude ondemands` lists each on-demand's safeguards. Providers declare lifetime
and startup-command support. RunPod supports a startup timer, but no provider
lifetime setting. Its timer begins before client installation and ends the pod at the
deadline or when its dollar cap runs out at the actual price. Autorun uses the
same timer. A failed timer triggers deletion. The worker ends work early
enough to leave up to two minutes for final evidence delivery.

Scheduling passes check recorded machines; queue submission itself does not. An on-demand machine with no started
worker five minutes after the create claim is deleted (earlier at its
deadline). This check needs a command to run; the machine timer runs independently
once its image starts. RunPod cannot run that timer while an image is pulling.

The charged hourly price replaces the quote after creation. Machine acquisition and rental records
hold billed dollars and the end of their last contiguous billed clock hour.
Spend combines those bills with an estimate for uncovered time. `~` marks a
figure containing an estimate. `machines` shows the billed and estimated
parts, and warns if a bill exceeds the rental cap. Run shares follow time
occupied; idle time on a kept rental belongs to the rental.

Limits use estimates because bills arrive after each hour. Late bills correct
evidence without changing admission decisions. Commands fetch missing bills;
a provider request times out after fifteen seconds and leaves estimates intact.
Storage charges are excluded.

## Imports

Every `__init__.py` is empty. Every name is imported from the module that
defines it, by its full path, and relative imports are a lint error:

| Import | What it is |
| --- | --- |
| `from etude.jobs import training, evaluation` | typed jobs: Etude decides the command |
| `from etude.launch.records import Job, Run` | what to run |
| `from etude.launch.client import queue, launch, status, wait, ...` | the calls a script makes; they go to the controller when one is set |
| `from etude.launch.database import Database` | the SQLite records in `launcher.db` |
| `from etude.launch.schemas import Packet, Work, Result` | frozen worker messages |
| `from etude.launch.executor import Executor` | machine acquisition, startup and journal composition |
| `from etude.launch.scheduler import Scheduler` | the owner of one database: queueing, admission, matching runs to machines |

Records are frozen Pydantic models, constructed with named fields:
`Job(command="./simulate", max_hours=2)` and `Run(job=job, seed=1)`.
Use `model_dump_json()` for JSON and `model_validate_json()` to load it. Imports sit at the top of each module, so every command, including `--help`,
loads the whole launcher: 33 modules and Pydantic, about 70 ms measured on
2026-10-13. A test pins the module count. Each remote command imports client dependencies once
in its installed environment.

## Cohorts and Python

```python
from decimal import Decimal
from pathlib import Path

from etude.jobs import continue_from, training
from etude.launch.client import default, fetch, logs, queue, queue_cohort, wait
from train import TrainConfig, train

job = training(
    train,
    config=TrainConfig(hidden=1024),
    until="steps=26000",
    max_hours=6,
    max_usd=Decimal(5),
)
runs = queue_cohort("capacity", job.runs(1, 2, 3), rent="l4", parallel=1, max_usd=Decimal(15))
wait(*runs)
logs(run=runs[0])
fetch(runs[0], "weights.pt", Path("out"))  # a file the job left in its output directory
parent = default().record(runs[0])
continued = queue(
    continue_from(parent, "steps=63000", source="s3://bucket/etude/launch"), pool="shed"
)
```

For the command line, a Python file defines a function `runs()` that returns a
Job, a Run, or a nonempty list of Runs. The function describes work without
sending it. The command calls it once, on the machine the command is typed on,
before anything goes to a controller:

```python
# capacity.py
from decimal import Decimal

from etude.jobs import training
from etude.launch.client import launch_cohort
from etude.launch.records import Run
from train import TrainConfig, train


def runs(hidden: int = 256, seeds: int = 3) -> list[Run]:
    job = training(
        train,
        config=TrainConfig(hidden=hidden),
        max_hours=2,
        max_usd=Decimal(1),
        until="steps=26000",
        name=f"hidden{hidden}",
    )
    return job.runs(*range(seeds))


if __name__ == "__main__":
    launch_cohort("capacity", runs(hidden=512), rent="l4", parallel=2)
```

```sh
etude queue capacity.py --rent l4 --cohort capacity --parallel 2 --max-usd 20
etude launch capacity.py --hidden 512 --seeds 5 --pool lab --rent l4 --cohort big
python capacity.py          # whatever its __main__ block does
```

Job parameters are ordinary flags parsed by Typer from `runs()`'s signature.
Required parameters become required options; annotated types and defaults govern
conversion. Boolean flags use `--fast` / `--no-fast`. Launcher flags and job flags
may be mixed after FILE; a bare `--` before job flags is accepted but not needed.
`etude launch capacity.py --help` shows launcher options, then job parameter types,
defaults and the function's docstring, without invoking `runs()`.
A parameter named like a launcher option is rejected when the file loads.
The old `NAME=VALUE` parameter syntax is removed; targets still use `steps=26000`
as a single clock value. The command does not run the file's `__main__` block.

The arguments used, defaults included, are stored on each run as `params`
(`record.params` in Python). They are for reading. A job's identity comes from
what it runs: its command, config, source and target. Two calls with different
arguments that produce the same four things are the same job. A function that
builds runs for several settings can record its own with
`job.runs(0, 1, params={"hidden": 64})`; the command leaves those alone.

A sweep is Python that calls the function:

```python
# sweep.py
import capacity
from etude.launch.records import Run


def runs() -> list[Run]:
    return [run for hidden in (64, 256, 1024) for run in capacity.runs(hidden=hidden)]
```

Several runs require `--cohort`. Cohort limits belong to command flags, not the
file. A job file's folder is on the import path, as for `python job.py`, so it
imports its neighbours. A relative `repo` is relative to the directory the
command is run from, exactly as when Python calls the launcher directly. A
config is a Pydantic model in Python, not a file beside the job: a named
setting is a function or class method returning one, such as
`TrainConfig.large()`.

TOML job files are no longer accepted. A script may also call `queue` or `launch` from
`etude.launch.client` directly. Where commands accept run IDs, a cohort name selects
its runs.

## The run promise

A run ends when it reaches its target, fails, is cancelled, or uses up its hours
or dollars. Losing a machine does not end a run. It is tried again on another
machine as a new RunAttempt of the same run, which resumes from the latest
checkpoint its [tracking](tracking.md) recorded.

The run's hours and dollars span all attempts. Hours count code execution;
dollars include its attributed setup and failed attempts. A machine's window
ending also starts another attempt if the run has limits and attempts left.
A run with no checkpoint starts again from the beginning. Each attempt's code
starts at most once; uncertain starts remain fenced. `lost` denotes exhausted
attempts or an unresolved start whose execution cannot safely be repeated.

The launcher has no checkpoints of its own: a run's checkpoints are its tracked
files. They are carried to the bucket as they are recorded, so they survive pod
deletion, and a later attempt on a fresh machine with a bucket is given back
the run's records and its latest checkpoint file before the job starts, and
nothing else. Between owned hosts without a bucket, the controller relays the
same from the host that last ran the run; while that host cannot be reached,
the retry waits. Either way its ordinals continue. Automatic recovery retains the run ID and seed. `etude continue` instead goes further than the target
or branches from a completed run, making a new run with a parent.

There is no `retry` flag; `max_attempts=1` bounds a job to one attempt.

## Progress and continuation

Clocks are named counts chosen by the job: training steps, learner transitions
and optimizer updates can be distinct clocks. Targets such as `steps=63000`
are absolute, including after continuation. Hours and dollars bound execution.
Training code reads `ETUDE_OUTPUT_DIR`, `ETUDE_STOP_FILE`, `ETUDE_DEADLINE`,
`ETUDE_SEED`, `ETUDE_CONFIG`, `ETUDE_UNTIL_CLOCK` and `ETUDE_UNTIL_VALUE`.
A job's own `environment` variables are set too; Etude's `ETUDE_*` names take
precedence.

A job can be given files by name:

```python
from etude.jobs import training
from etude.launch.records import Input

policy = Input(
    name="policy.pt", key="etude/launch/RUN/checkpoints/policy.pt", sha256="…", size_bytes=1048576
)
job = training(
    finetune,  # opens Path(os.environ["ETUDE_INPUTS_DIR"]) / "policy.pt"
    config=FinetuneConfig(),
    inputs=(policy,),
    max_hours=1,
    max_usd=Decimal(2),
)
```

An evaluation job's `checkpoint` is an input like this one.

`key` names an object in the bucket of the storage the machine keeps its
records in (the on-demand's, or an owned host's `--storage`). The launcher gives
the worker a scoped read link; the worker downloads it with the same verified,
resumable download as source and checkpoints, before the job starts, and puts it
at `ETUDE_INPUTS_DIR/NAME`. Bytes that do not match the digest are refused: the
job's command never runs and the attempt ends `lost (setup_failed)` with the
reason in its log. Inputs are part of the job's identity. A machine with no
storage can be given only a run's file (`Input.run`, as continuation does).

The job is also told who it is: `ETUDE_RUN` (the run's id), `ETUDE_JOB` (the
digest its job's runs share), `ETUDE_JOB_NAME` when the job has a name,
`ETUDE_ATTEMPT`, and `ETUDE_COHORT` when the run has one. It runs in
`ETUDE_SOURCE_DIR`, its commit's files without `.git`; [evaluation](evaluation.md)
hashes identity files relative to it. [Tracking](tracking.md) reads these, so a launched job names only
its experiment and clocks. The launcher has no experiment; the script names it. A tracked
script does not write `etude-result.json` itself: tracking reports its clocks.

A storage holds one folder per run, shared by every attempt on every machine,
and the machines' own working files apart:

```text
PREFIX/RUN/                      everything about one run
  attempts/ATTEMPT.log           what the job printed
  attempts/ATTEMPT.json          when it started, its progress, how it ended
  attempts/ATTEMPT/NAME          files the job left in its output directory
  run.json  state.json  metrics/ the run's tracked files, as they are,
  checkpoints/                     every checkpoint tracking recorded among them
PREFIX/sessions/SESSION/         one machine's instructions, inbox and notes
PREFIX/sources/SHA256.tar        a commit's source archive, named by its digest
```

The job is given `ETUDE_TRACK_DIR`, `track` beside the `sessions` directory
(on this computer `~/.etude/track`), and tracking keeps the run's files in
`ETUDE_TRACK_DIR/RUN/`. The courier copies that folder into the run's folder
in storage while the job runs; the job still holds no keys. A second attempt,
on this machine or another, writes into the same run folder. The courier and
tracking's own `sync` follow one set of rules (`etude.carriage`): files ending
`.open` or `.partial` wait until they are whole, `.tail` files until the
attempt has ended, and anything under a folder starting with `.` stays; each
pass sends checkpoint files before their records; checkpoint bytes count as
`checkpoint` in usage; and an object under `checkpoints/` is sent once and
never replaced.

A job reports its progress by atomically replacing `etude-result.json` in the
output directory; `etude status` shows it:

```json
{"clocks":{"steps":26000,"learner_transitions":52000}}
```

The courier uploads final outputs before the completion note.

The other job-written handoff is any file ending `.removed` in the run's
tracking folder, such as `metrics/0123456789ab.removed`, listing paths
relative to that folder:

```text
metrics/1-00000000-00000007-0123456789ab.blk
```

Tracking appends one relative path per line only after its replacement block
and manifest exist, then removes the old local files. The courier snapshots
complete lines before listing files, uploads the pass's files (including the
note), then deletes listed paths it has itself uploaded for that run. It forgets
successful deletions; an interrupted or failed pass retries. Without delete
credentials, old copies remain readable. A restarted courier conservatively
leaves old copies it did not upload. The launcher knows no block format or
coverage rules, and neither Python package imports the other.

Continuation starts a new run of the parent's job and seed from one of the
parent's recorded checkpoints, its latest unless `--ordinal` says. `etude.jobs`
reads the checkpoint's record from `--source`, where the parent's tracked
folder is (its bucket, or a runs directory). Without `--source`, it reads this
computer's runs directory, else the records on the owned host that last ran
the parent. The file reaches the machine as an input of the parent run
(`Input(run=PARENT, key="checkpoints/NAME")`), verified by digest: from the
bucket where the parent's folder was carried, or, without one, from that
host's folder for the parent. Between owned hosts the controller relays just
that file to the new run's host before it starts, and the run waits while the
parent's host cannot be reached. Tracking is handed the record in
`ETUDE_CONTINUES`. The `continue` command retains the parent's pool and
on-demand. Its target must exceed the checkpoint's position on the named
clock.

```sh
etude continue --run RUN --until steps=63000 --source s3://bucket/etude/launch
etude continue --run COHORT --until steps=63000 --cohort NEXT --source s3://bucket/etude/launch
etude cancel RUN
```

## Ownership and failure behavior

The code has three layers:

1. `machines.py` defines allocations, requests and safeguards. `providers/runpod/machines.py`
   obtains machines, supplies stock-image boot, reads prices/bills and deletes
   exact provider identities. `OwnedMachine` represents an existing address.
2. `executor.py` composes startup (`startup.py`, `ssh.py`) with directory,
   SSH or bucket journals. `worker/attempts.py` executes packets without knowing
   jobs or providers; `worker/courier.py` moves inboxes and evidence. Rented
   machines start themselves; owned hosts use their configured connection.
3. `controller.py` forwards CLI commands and runs the standing loop; `source.py`
   freezes and publishes git source, reads its history and caches transport archives.
   `scheduler.py` owns jobs, runs, cohorts, queue placement and limits in `database.py`. Lower layers are public Python types and can be used without
   the run scheduler.

The SQLite file is `$ETUDE_HOME/launcher.db` (default `~/.etude`). Versioned
writes claim each session once. A lost create reply is looked up, never
repeated. A remote start has an exclusive claim; a lost SSH reply cannot start
another process. An unreachable machine is unknown. A lost attempt requires
evidence that its process or provider machine is gone. Infrastructure losses
retry up to `max_attempts`, including confirmed loss after code started. If startup intent reached storage but an attempt claim did not,
its start is uncertain and it is not retried.

The database has no migrations while the launcher is a prototype. The file
carries a schema version; a file written by another version is refused with a
message naming it, and is left untouched. Move or delete it to start again.
Tables are `machines`, `sessions`, `rentals` (one per rented machine, keyed by
machine) and `attempts`. What the provider said about making and deleting a
rented machine is stored on that machine's one session and read as Machine.
A record refers to another by its name or id as a plain string; the field
name says what it refers to.

A session on a machine you added has no deadline; every run still enforces its hour limit.

`etude wait` without names keeps looking until queued work finishes. Started
workers execute their delivered work independently; a launched machine with
an inbox is cleaned up on the next scheduling pass or at its own deadline.
`launch_cohort` first acquires one machine per first-wave run. Each machine
then takes waiting work that fits its remaining window. `etude controller run` keeps matching new work
after commands exit. Sweeps, spot recovery and additional providers remain open.

## Layout and dependency rules

The names follow established usage: Python's SQLite interface calls its owner a
database; concurrent.futures calls an execution owner an executor; Pydantic calls
serialized message shapes schemas. RunPod supplies machines; S3 stores evidence.
SSH remains the connection to a host, including its disk.

```text
etude/launch/
  records.py  schemas.py  client.py  cli.py  commands.py
  scheduler.py  executor.py  database.py  source.py  controller.py
  ssh.py  startup.py  machines.py  environment.py  errors.py  measurement.py
  storage/    disk.py  bucket.py  s3.py
  worker/     main.py  attempts.py  courier.py  remote.py
  providers/  cleanup.py  package.py  runpod/ api.py  machines.py  pod.py  http.py
```

One AST test, `tests/launch/test_layout.py`, enforces the rules:

- Workers cannot reach the scheduler, database, client, CLI or controller through
  imports. Workers execute packets and return evidence, without placing runs.
- Provider implementations are named only inside `providers/`, with `client.py`
  as the single composition point. Generic cleanup and package manifests keep
  machine-specific choices out of workers and SSH.
- `providers/runpod/pod.py` and its complete import graph use only the standard
  library. `environment.py` also remains independent of client dependencies.
- Storage cannot import machine connections or startup, directly or transitively.
- Imports use full module paths and all package initializers stay empty.

`Record` lives in `records.py`; `schemas.py` owns packet, result, bootstrap and
inbox models. `executor.py` owns `Executor`, its `Execution` interface,
and definite capacity refusal. There is one representation of each record.

The stock-image bootstrap and guardian live in `providers/runpod/pod.py`;
`providers/package.py` builds the immutable archive and its bare-Python subset.
Startup follows the machine's kind. A session's packet, journal folder and
bucket keys are named by its session id.

## Command expectations

Each command declares `Kind.read`, `Kind.ask` or `Kind.wait` beside its definition
in `cli.py`. These are the intended expectations; declarations do not change
execution. Normal `status` still advances the loop, and other current deviations
remain pinned in counting tests.

| Kind | Commands | Allowed dependency | Expected result |
| --- | --- | --- | --- |
| Read | `status`, `runs`, `pools`, `ondemands`, `machines`, `providers`, `storages`, `controller` | database only | immediate answer with freshness |
| Ask | `queue`, `cancel`, `terminate`, `rent`, `pool` and `ondemand` mutations, `default`, `machine add`, `provider set`, `storage add` | database only | recorded intent and an id where applicable |
| Wait | `wait`, `launch` (with or without `--quote`), `availability`, `fetch`, `logs`, `source`, `continue` | other machines with a time limit | completion or the limit |

Controller selection mutations are asks. `worker` and `controller run` are waits
because they deliberately execute long-lived loops; they retain their existing
lifetime rules. Job-file `runs()` is an input function, not a launcher command.

The full expectation also gives external work to the controller loop, makes asks
safe to repeat with caller-chosen identities, restricts waits to watching, bounds
lists, states answer freshness and prevents one supply's trouble delaying another.
Commands require only dependencies implied by their kind: reads must work without
provider keys or a network. These are documented expectations for the follow-up;
this foundation does not enforce them, add ids or deadlines, remove refreshes,
or change controller forwarding.

## Measurement and profiling

Normal smoke tests count work, never elapsed time. The in-memory provider supports
any worker count, maintains an adjustable scientific clock and counts provider
operations and journal operations (the stand-in's bucket boundary). Its optional
`delay_seconds` applies a fixed delay per call and defaults to zero.
`tests/launch/counting.py` also wraps physical provider/bucket sends, counts actual
git and SSH subprocess launches, and uses SQLite's trace callback to count all SQL
statements, including transactions. Fixture construction is outside measurement.
Counts retain no SQL text, process arguments, payloads or credentials.

The normal suite pins read/ask/wait costs, live-worker scaling at 1 and 10 workers,
one controller pass, a three-run git-backed cohort, and five Etude modules loaded
by help. The help check excludes platform-dependent standard-library module totals.
The physical transport test checks provider, bucket and SSH counts with local replies.
Zero external effects for queue and the phase read are targets; normal status,
wait advancing work, repeated SQL scans and repeated cohort git preparation are
known debt.

Scale cases are excluded by default. Run them explicitly:

```sh
uv run pytest tests/launch/test_measurement.py -m scale -s
```

Records-pass baseline on 2026-10-13, same active workers and runs; no paid
machines or network. Counts at 1 and 10 are measured; 100 and 1000 use the pinned
coefficients and await the opt-in scale check at gate:

| Workers | Operation | Provider calls | Journal calls | SSH | Git | SQL statements |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 10 | status | 20 | 10 | 0 | 0 | 653 |
| 10 | runs | 0 | 0 | 0 | 0 | 131 |
| 10 | controller pass | 20 | 10 | 0 | 0 | 272 |
| 100 | status | 200 | 100 | 0 | 0 | 6413 |
| 100 | runs | 0 | 0 | 0 | 0 | 1301 |
| 100 | controller pass | 200 | 100 | 0 | 0 | 2612 |
| 1000 | status | 2000 | 1000 | 0 | 0 | 64013 |
| 1000 | runs | 0 | 0 | 0 | 0 | 13001 |
| 1000 | controller pass | 2000 | 1000 | 0 | 0 | 26012 |

Queueing one source-free run costs 5 SQL statements and no external calls.
A wait with a zero timeout still advances one pass: 4 provider calls, 1 journal
call and 70 SQL statements: packet construction reads its rental once for the
deadline, cap and reuse policy. Queueing three git-backed runs costs 57 git commands
and 12 SQL statements. These are fixed fixture baselines, not physical HTTP request
estimates or desired performance budgets.

Stock-image creation adds the following physical HTTP baseline: each create
uses 2 provider calls (availability and create), with 3 bucket calls on first
use (package read, package put, receipt put), 1 on reuse in the same command,
and 2 in a new command when the digest already exists. Every case uses zero SSH
and git calls. Package and bootstrap reads at pod boot are separate worker-side
requests. Signed-link generation makes no HTTP request.

Looking up a retained machine receipt uses 1 bucket read and no provider call
or receipt write. Recovering a lost create reply uses 1 bucket read, the provider
inventory (1 call per page), and 1 receipt write when found. Health checks remain
separate from receipt lookup; no path waits for a rented machine's SSH address.

Profile any invocation, including help, into a standard cProfile file:

```sh
etude --profile /tmp/status.pstats status
uv run python -m pstats /tmp/status.pstats
ETUDE_SPANS=/tmp/etude-spans.log etude status
```

`--profile FILE` precedes the command and profiles this process, including controller
forwarding when selected. It does not activate profiling in a remote worker.
`ETUDE_SPANS` enables named elapsed-second lines around provider HTTP, bucket calls,
SSH (including controller commands) and git waits in the process where it is set.
Nested spans may overlap; do not sum them as total runtime. Names are fixed and
exclude arguments and payloads. Unset means no span file or timing calls. A failed
span write never changes execution. Timing is diagnostic evidence, not a test
threshold; machine load makes duration assertions unreliable.

Read a run's retained start milestones:

```sh
etude status RUN_ID --phases
```

This opt-in view reads the database without a scheduling pass or machine refresh.
It shows queued, machine asked for, machine confirmed, worker up, code started and
finished, for every retained attempt. Missing timestamps say `not recorded`.
The existing database has a provider receipt but no timestamp for its confirmation;
the confirmation phase therefore has no timestamp and says `receipt retained`
when that evidence exists. It never borrows another phase's
clock. Times are UTC observations retained by the controller, with their existing
precision. They are historical evidence, not a claim of current machine freshness.

## Checks

```sh
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
```

CI runs the tests with the outside network denied:
`uv run python scripts/test_network.py uv run pytest`.

Scoped storage links cannot be revoked early, and machine credentials last
thirteen hours without renewal.

## A run's source in git

Git is the source record. When a job has `repo`, the submitting client freezes
its source and records its position in history: commit, origin address, branch,
the common ancestor with origin's default branch (date and subject included),
commits ahead, nearest release from `git describe --tags`, and whether it is a
snapshot. These facts stay as observed at submission even when branches move.
Older runs without these facts still show their original commit.

Before anything is rented for a run, its source archive is put in the
on-demand's storage (once per archive digest), so a rental's window starts only
after the upload: no paid machine waits on it. `git archive` honours
`export-ignore` in the commit's `.gitattributes`; mark large files the job does
not need that way to keep the archive small.

Omit `revision` (or use `None`) to include final working files, including
nonignored untracked files. Etude makes a deterministic commit parented by HEAD:
the same tree and parent give the same commit. It changes no branch, index or
working file. A clean tree uses HEAD. An explicit revision, including
`revision="HEAD"`, uses exactly that commit and does not inspect working changes.

If the commit is not already reachable on origin, Etude pushes it under
`refs/etude/runs/RUN_ID` before recording the run. Failed publication means no
run is recorded. Launchers use the user's git credentials; credentials are not
stored in run records. Put credentials in git's credential helper, not origin's
URL. A repository without origin is allowed and marked **local-only**; Etude
keeps a local run ref, and recovery needs that repository. Local refs also keep
origin-backed source available while runs wait to be sent to workers.

Workers receive tars and never need git keys. Tars under `ETUDE_HOME/sources`
are transport caches, not the source record. With an SSH controller, the
submitting client snapshots and pushes, then sends the frozen metadata and tar.
The controller does not pull launch source itself in this build.

A run displayed by `etude status` includes a source line, for example:

```text
  source: 79e54361b230 on distributed, main@3f2a1c9 "Add replay buffer sharding", v0.3.0 + 14, 2 commits ahead, plus uncommitted changes
```

`status` defaults to unfinished runs. `runs` lists all runs, including finished
ones, and replaces `status --all`. Filters combine with AND; `--job` accepts a
job digest or display name. An unknown revision is an error.

```sh
etude runs
etude runs --contains v0.3.0 --cohort capacity --rent l4
etude runs --job JOB_ID
etude source RUN_ID recovered-source
etude source RUN_ID                     # creates etude-source-RUN_ID
```

`source` fetches the recorded commit from origin and checks it out detached in
a new directory on the requesting client, including when a controller is set.
It refuses an existing destination and does not use the tar cache. Local-only
runs fetch from their original local repository. Both commands inspect history
without advancing the queue. Ancestry needs git history: it uses the original
repository when available, otherwise origin; unavailable local-only history is
an error. Retain origin refs for as long as their runs need to be recoverable.

Two limits apply. Ancestry says the code was present, not that a run's config
turned a feature on. A change merged into the main line as a squashed commit is
not contained by a run sent earlier from its branch, even if its code is the same.

The records pass increases local SQL counts because paid windows and machine
acquisition are now explicit reads rather than fields joined into every Session:
status adds 27 statements per active worker, runs adds 6 and a controller pass
adds 9. Remote counts are unchanged.


### Startup allowance

```python
OnDemand(name="gpu", template=template, startup_seconds=600)
OnDemand(name="slow-image", template=template, startup_seconds=None)
Pool(name="lab", startup_seconds=120)
```

`startup_seconds` defaults to `None` (no startup cutoff). A session snapshots its deadline when creation
is claimed. Package delivery, image acquisition and client installation share that
allowance until the worker's start receipt. Changing a pool or on-demand affects future sessions.
`None` removes this cutoff; rental time and dollar limits still apply. Arbitrary
setup inside `Job.command` is part of job execution, outside this startup allowance.
`setup_seconds` remains the extra rental time reserved for startup, not a timeout.

```sh
etude ondemand add gpu --provider runpod --gpu "NVIDIA L4" --startup-seconds 900
etude ondemand startup gpu --seconds 0
etude logs --run baseline --follow
```

CLI zero means unlimited startup. Install output is retained while it runs; logs
identify the effective deadline when it expires. SSH startup detaches installation
and returns pending; a lost reply never authorizes a second installer or worker.
Image pull occurs before the in-machine guardian starts, so cleanup during that
phase requires an active controller and a confirmed provider identity.

Explicit controller installation also accepts `startup_seconds` in Python and
`etude controller set ADDRESS --startup-seconds SECONDS`. The default is no
timeout; a configured allowance governs both the local wait and remote install.
A timed-out wait is not proof the remote installation stopped. A log read retains
its own explicitly chosen deadline. Downloads, SSH key generation, sshd startup
and guardian readiness inherit this budget without separate 30/60-second caps.

The prototype database schema is version 8; older database files are refused.


### Prepare a framework image before renting

```python
from etude.jobs import training
from etude.launch.images import prepare_pytorch, resolve_image
from etude.launch.records import MachineTemplate

prepared = prepare_pytorch(torch="2.10.0", cuda="12.8")
template = MachineTemplate(provider="runpod", gpu="NVIDIA L4", image=prepared.image)
job = training(
    train,
    config=TrainConfig(),
    runtime=prepared.runtime,
    setup='"$ETUDE_PYTHON" -m pip install etude-sdk',  # the stock image has no Etude
    max_hours=1,
)
# Custom public or private image; uses Docker's configured registry credentials.
custom = resolve_image("registry.example/team/training:release", timeout=30)
```

```sh
etude image prepare pytorch/pytorch:2.10.0-cuda12.8-cudnn9-runtime
etude ondemand image gpu pytorch/pytorch:2.10.0-cuda12.8-cudnn9-runtime
etude machines --all --images
```

Preparation waits for registry metadata with `timeout=None` by default. A positive
`timeout` bounds the whole operation; CLI `--timeout 0` disables the cutoff. It needs Docker
CLI with Buildx and uses `docker login` credentials; no Docker daemon, layer pull,
image build or machine rental is performed. The default platform is `linux/amd64`.
The output records the requested reference, immutable platform manifest and
verified OS/architecture. Unsupported or ambiguous platforms fail before rental.

`prepare_pytorch(kind="devel", ...)` selects the official development variant for
jobs that compile CUDA extensions; the default runtime variant omits that toolkit.
The recipe returns a `PreparedRuntime` containing the immutable image and matching
Python/Torch/CUDA requirements. The job probes `python` in that image and checks
the exact Torch CUDA distribution before user code. It installs nothing. Custom
images use `resolve_image` plus an explicit `Runtime`.
Version combinations must exist in the upstream registry. Resolution establishes
artifact identity and platform, not framework ABI compatibility or a speedup.

Each acquired machine retains its selection across on-demand edits and controller
restarts; `machines --images` reads those facts without registry access. Bootstrap
logs include the same selection. `ondemand add --image` and existing digest strings
remain available when resolution has already happened; their platform is unchecked.
Typed Python callers can construct `Image(requested=..., resolved=...)` for these
pre-pinned artifacts. Private provider pulls still require the provider's registry
credential reference; Docker login only authenticates preparation.

The bootstrap overrides vendor entrypoints. Choose an explicit job interpreter to
reuse the image environment; selecting a PyTorch image alone does not stop a job
command from installing Torch again. Cold image pulls can still dominate startup.


### Reuse a preinstalled job runtime

```python
from etude.jobs import training
from etude.launch.records import Runtime

job = training(
    train,
    config=TrainConfig(),
    max_hours=1,
    runtime=Runtime(
        python="python3.12",
        python_version="3.12",
        packages={"torch": "2.10.0+cu128"},
        cuda="12.8",
    ),
)
```

Use the interpreter and exact installed distribution versions from your image.
Before user code starts, Etude probes that interpreter in isolation, checks the
requested Python prefix, exact package versions and optional PyTorch CUDA build,
and records the observed values in the attempt log. The probe shares the attempt's
deadline and cancellation. A mismatch fails the attempt without starting or
retrying training. CUDA build validation does not prove host-driver compatibility
or GPU execution.

`ETUDE_PYTHON` names the verified executable; its directory leads `PATH` and an
actual virtual environment also sets `VIRTUAL_ENV`. These settings apply only to
the job. Etude refuses its own client environment and never installs or upgrades
job dependencies. Missing project dependencies belong in a prepared image or your
explicit setup command. Vendor entrypoint scripts are not activated implicitly.
Without `runtime`, a typed job runs `uv run python` in the checkout and a
command runs in the shell's environment.

The pinned official PyTorch example has a repeatable CPU check:
`uv run python scripts/test_pytorch_image.py`. It pulls the image, then checks
runtime versions, a CPU tensor operation and guardian imports with site packages
disabled inside network-disabled containers. The Prepared image workflow runs it
on Linux; no GPU or provider account is needed. This is not a full bootstrap or
CUDA execution test.


### Setup, training and cleanup

```python
job = training(
    train,
    config=TrainConfig(),
    setup='"$ETUDE_PYTHON" prepare_data.py',
    cleanup='"$ETUDE_PYTHON" collect_diagnostics.py',
    cleanup_seconds=30,
    runtime=Runtime(python="python3.12"),
    max_hours=1,
)
```

Setup runs once for each attempt, after source preparation and runtime validation.
It shares the attempt's hours with training. Both run in the same source directory
with the same selected runtime and outputs directory, in separate shell processes;
shell exports and `cd` in setup do not alter the training shell. Use files for
prepared state. Setup is not a cache: infrastructure recovery may allocate another
attempt and run it again. A nonzero setup or training exit is terminal and is not
replayed automatically.

When hooks are configured, logs label setup, run and cleanup, including exit or
stop reason. `ETUDE_PHASE` identifies the current phase. Cleanup runs after user
code has begun, including setup failure, cancellation and the attempt time limit.
Its `cleanup_seconds` defaults to `None`; a positive finite value limits this
phase, capped by the remaining session/rental deadline. On owned machines without
a session deadline, unlimited cleanup can remain in progress until it exits. Its `ETUDE_DEADLINE` reflects that cap. Cleanup
ignores the cancellation request so it can collect diagnostics; `ETUDE_STOP_FILE`
remains present. An expired paid window cannot be extended for cleanup. A killed
machine or failed preflight cannot promise cleanup.

Cancellation requests TERM for the process group. `Job.stop_seconds` optionally
limits the graceful stop before KILL; its default is `None`, still capped by the
phase deadline. At an exhausted phase deadline Etude sends KILL immediately and
reaps the process before closing its output. Cleanup
failure is logged and makes an otherwise successful attempt fail; it never replaces
a setup/training failure, cancellation or time-limit outcome. Cleanup is best-effort
and must not be the only owner of an essential external action.

Storage GETs retry transport failures and HTTP 429/500/502/503/504 at most three
times, with 0.1/0.2-second backoff. All attempts share the caller's optional deadline, including backoff; none adds
a separate 60-second ceiling. Retry diagnostics include attempt and failure category,
never signed URLs. Without a retained log destination, diagnostics go to stderr
so command results stay readable on stdout. Missing objects and permanent/authentication errors are not
retried. Bootstrap waits for a not-yet-published object, but an exhausted or
permanent read failure ends startup instead of starting another retry loop.
Writes, launch admission, provider creation and user commands gain no
automatic replay from this policy.



### Timeout ownership

```python
from etude.launch.deadline import limit_time

# An optional total allowance for synchronous remote operations in this block.
with limit_time(900):
    snapshot = scheduler.logs(run="baseline", timeout=None)
```

Startup and log retrieval default to no timeout. `None` adds no deadline; nested
operations still obey an existing caller deadline and rental limits. Positive
values are seconds; CLI `--timeout 0` and `--startup-seconds 0` select no timeout.
A finite allowance is shared across requests and is never reset by a retry.
SSH connection options can be set explicitly in the customer's SSH configuration;
Etude supplies no hidden connection timeout. Provider HTTP and storage transports
use the enclosing operation allowance without a separate fixed socket limit.

Finite HTTP operations also bound waiting for the complete response, even when
a connection keeps streaming bytes. Expiry leaves the in-flight outcome unknown:
Etude does not infer absence or replay writes. The controller's persisted startup
deadline and the independent rental guardian remain the lifecycle authorities. A successful transport call does not
prove that remote work completed or was cancelled.


`delivery_seconds=None` on a pool or on-demand controls the courier's shared session-stop and final
transfer allowance. A finite value also reserves that much of the paid window
for final delivery; no implicit two-minute/quarter-window reserve remains.
The provider deadline always caps it. Final delivery makes at most 40 attempts;
this is an attempt count, not a hidden elapsed-time deadline.
`log_seconds=None` controls each background/final diagnostic publication,
including its lock wait. An explicit finite value may leave evidence incomplete;
publication never replaces the original failure. Both settings are available on
`pool add` and `ondemand add` as `--delivery-seconds` and `--log-seconds` (zero means no added cutoff).
The bootstrap receives the latter as `ETUDE_LOG_SECONDS`; direct `publish` callers
can supply `timeout` themselves. The independent guardian can terminate the rental
while either operation is blocked.

A caller timeout means it stopped waiting. A lifecycle timeout requests remote
termination; only a receipt/provider observation confirms that termination. Logs
retain the phase and available evidence through these transitions. Inspect the
same run again after a read timeout; do not relaunch to get its logs.

The local database's lock-contention wait is separately explicit:
`Database(path, timeout=30)` retains a finite default so a competing writer does
not indefinitely hold a synchronous local command. Override it in seconds; zero
fails immediately on contention. This is a SQLite lock wait, not a startup or job
limit. Tracking's separate `Run.finish(timeout=10)` remains independently configurable.

## Transfer usage and verified retrieval

```python
from pathlib import Path

from etude.launch.client import fetch, usage
from etude.launch.usage import R2, S3_US_WEST_2, Usage

# Wait: fetch verifies the retained transport SHA256 and length before publication.
first = fetch(run, "checkpoint.pt", Path("results/first"))
second = fetch(run, "checkpoint.pt", Path("results/second"))

# Read: retained database evidence only; no bucket, provider or worker refresh.
report = usage(run=run)
print(
    report.observed_at,
    report.received_bytes,
    report.requests,
    report.reused_bytes,
    report.retransferred_bytes,
)
print(report.coverage)
for event in (*report.events, *report.shared_events):
    print(
        event.session,
        event.attempt,
        event.purpose,
        event.status,
        event.request_id,
        event.received_bytes,
    )

# A download/request scenario at dated list prices, not an invoice or a rental cap.
outputs = Usage(
    observed_at=report.observed_at,
    events=tuple(e for e in report.events if e.purpose == "result"),
)
print(outputs.estimate(S3_US_WEST_2), outputs.estimate(R2))
```

`etude usage --run RUN` reads the same retained evidence. `observed_at` is the
latest included event timestamp, not a claim that remote evidence is current.
The worker's ordinary synchronization/tick retains session events; the read
never refreshes them. With a selected SSH controller, the public call still uses
the existing SSH connection to that controller; the database read itself contacts
no worker, provider or artifact store. Failed downloads and bootstrap runtime reads remain in
session evidence when publication succeeds. Hard kills, missing journals and
truncated operational logs leave unknown intervals.

Run totals include that run's events. Shared source/runtime traffic appears in
`shared_events`, outside run totals; it has no fabricated per-run allocation.
Use `usage()` or `usage(machine=...)` for totals that include shared and
unattributed controller traffic once. Deduplicate exports by `Transfer.id` when
combining scopes. Upload counts are body bytes supplied to urllib, including
multipart form framing, not confirmed socket bytes. Response counts are actual
body reads, including partial responses and failures; object length is separate.

The persistent cache is `$ETUDE_HOME/downloads` (default `~/.etude/downloads`),
keyed by transport SHA256 and length, with a 20 GiB payload allowance. Both
completed-output reads and worker source/checkpoint reads use it. The first
cold acquisition still transfers every byte. For cache-admitted artifacts, verified warm reuse transfers no
payload bytes, including from a new process or to another destination; control
metadata reads still cost requests and bytes. Sequential 8 MiB ranges retain
completed chunks across interruption and require exact ranges and a stable
strong ETag. Invalid ranges, authentication failures, changed identity or digest
mismatch fail explicitly. Files larger than the allowance, or acquired while
other digests occupy it, stream through destination-side staging. Oversized and destination-staged downloads do not retain a reusable cache copy.
Only verified bytes replace the destination. Downloads add no timeout by default;
they share an existing caller deadline, and worker inputs remain bounded by the
rental deadline. Local digest verification and copying check that allowance
between chunks and before atomic publication; expiry preserves an existing
destination. Two transport retries, including HTTP 429/500/502/503/504, share
one budget across all ranges and the caller's deadline. Python callers can
set an allowance with `Cache.fetch(Download(...), destination, timeout=..., retries=...)`
from `etude.launch.storage.download`.

A local HTTP fixture completes two synthetic runs on one kept machine. It
installs a fixture runtime, retries a denied source download, shares a 2,097,150
byte source and 1 MiB checkpoint input, publishes two identical 24 MiB
checkpoints, collects worker journals through scheduler ticks, and fetches each
result twice through `Scheduler.fetch`. Worker and controller caches are separate.

| Public result fetches (both runs) | First pair | Warm pair |
| --- | ---: | ---: |
| Payload response bytes | 25,165,824 | 0 |
| Control response bytes | 3,300 | 3,300 |
| Payload GET requests | 3 | 0 |
| Control GET requests | 4 | 4 |
| Verified reused bytes | 25,165,824 | 50,331,648 |

The first pair downloads the shared result once in three ranges. A fresh rental
still needs every cold input byte. A separate interrupted 12-byte fixture
observes 6 bytes before failure, then 8 on recovery, reusing the completed
first 4-byte range.

For the combined workload, the fixture also measures full GETs for the same
logical input/result reads without reuse. This counterfactual keeps the measured
control/upload trace fixed; it does not execute an old SDK. Including the one
failed request, runtime, source/checkpoint inputs, checkpoint uploads and 47
control requests, the comparison has 59 requests before and 56 after, saves
78,643,198 response bytes, and reduces the illustrative download/request estimate
by USD 0.00707908782 at the rates above. Control bytes are measured from their
actual bodies, including journal JSON and multipart framing; they are not assumed
free. Upload body counts remain supplied bytes, separate from response bytes.

Every fixture GET matches retained usage, replayed worker events count once,
and run-owned bytes plus deduplicated shared bytes conserve the combined total.
Reopening the database with no provider bindings returns the same evidence with
zero provider, SSH, git or artifact calls.

This is a scoped estimate through completed-result retrieval. Two final
usage-journal uploads cannot include their own completion in the bodies being
published; their unretained requests remain unknown. Dependency installation,
tracking boto3/report reads, other HTTP outside observed collection/fetch paths,
SSH, machine cleanup, rental, persistent storage and private billing are outside
the projection. Do not add it to a rental total and label it a full experiment
cost. Tiny objects can cost more in request charges when chunked unusually small;
include requests as well as bytes when choosing a route.

For provider reconciliation, project private access logs into frozen
`Access(backend, request_id, key, response_bytes, status)` records from
`etude.launch.usage`, then call `reconcile(report, rows)`. Use the event's
backend hostname and decoded absolute object path. Duplicate request IDs count
once; conflicting rows fail. `matched`, `unmatched_provider` and
`unmatched_client` remain separate. Keep request URIs, signatures and credentials
out of exports. Provider log delivery may omit or duplicate requests, and a GET
is not by itself proof of billable internet egress. Reconciliation does not
change observed transport totals or turn them into billed amounts.

Adoption requires the SDK revision containing these APIs on both controller
and worker; generated archives include the same revision automatically. The
controller database schema is 6 and deliberately refuses older prototype
files. Preserve the old database and retained journals before choosing a fresh
controller home; this change does not migrate or modify live experiment files.
Use a fresh `ETUDE_HOME` for offline integration proof and install the reviewed
PR commit with `uv add 'etude-sdk @ git+https://github.com/loopflowstudio/etude@COMMIT'`.
