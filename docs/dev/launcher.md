# Launcher ownership and recovery

This reference is for maintainers changing launcher ownership and recovery; authors start with [the guide](../launcher.md).

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
and it is the only communication channel for a rented machine. The scheduler
writes work and stop requests to the inbox; the worker reads it and uploads
logs, progress and outputs. No inbound connection is needed. This computer and
owned SSH hosts need no bucket; an owned host can optionally use one for records
that outlive its disk. Only Amazon S3 is supported.

## Retained launch evidence

```sh
etude logs --run sweep/baseline@7                     # retained evidence, from the database
etude logs --run sweep/baseline@7 --follow --timeout 60
etude logs --run sweep/baseline@7 --job-only     # the job's output alone
etude logs --machine MACHINE
```

A run log includes launch preparation, shared machine startup, every execution
attempt and cleanup. It keeps that meaning before admission and after machine
deletion. `--experiment NAME` and `--job NAME` select several runs, as for `etude runs`. Machine logs
contain operational output; run logs select only that run's job output and
attempt diagnostics. The worker's log says what changed, such as an inbox with
new work or a delivery that failed, not every poll: an inbox read that finds
nothing new leaves no line and no usage record, and every other transfer goes
to the session's usage journal (`etude usage`), not the log.

If storage stops responding, requests time out and the worker logs the failure.
Status reports delivery age from retained evidence. Training can keep writing
locally while bucket-backed reports and remote logs fall behind; an unchanged
metric is not proof that training stopped.

Delivery retries files retained on the machine when storage recovers, including
during final delivery within its remaining allowance. Keep that local copy until
delivery succeeds: a rental's deletion deadline is not extended by a storage
outage, and files that never left a deleted machine cannot be recovered from the
bucket. See [tracking recovery](tracking.md#storage-outages-and-recovery) to
deliver a retained copy later.

A run's id is its scientific identity: its job's name and seed
(`sweep/baseline@7`), or the `id` of a `RunRequest` built by hand. Failed admission
leaves independent launch evidence, without creating a run or RunAttempt or
consuming that identity. A corrected launch can reuse it. An already-admitted
identity is a conflict; an unresolved acquisition remains fenced until confirmed
refused or deleted. A new launch ID cannot bypass that fence. `LaunchError.launch`
and `.runs` identify the retained evidence when admission fails.

`etude logs` is a read: it shows what the database retains, with each stream's
collection time, and needs no provider key or network. Each scheduler pass
retains a running job's new output, at most every 30 seconds, and a stream's last
output when its session ends; each read takes at most the supply's `log_seconds`,
or 30 seconds without one. `--follow` is the wait: it collects new
evidence from machines and buckets until the snapshot is complete, with no time
limit unless `--timeout 30` bounds it; in Python, `logs(run=..., refresh=True,
timeout=30)` takes one such snapshot. Follow uses the same selection and absolute
positions in each sanitized producer, stops at its time limit, and never
schedules or retries work. It prints new evidence
without replaying already seen output. Job exit alone does not close startup or
cleanup producers. A queued run or a missing final stream is incomplete.

Headers distinguish output not received, a silent producer, retrieval failure and
retention truncation. A failed refresh returns the last received content with its
collection time. Startup snapshots and scheduler log caches retain at most 1 MiB
of text per stream: opening milestones and a recent tail, with omitted positions
marked. Worker observations retain producer timestamps separately from scheduler
observation times; clocks on different machines do not establish a total order.

Bootstrap uses a write-only capability for one log object, independent of reading
the bootstrap packet or installing dependencies. The bootstrap publishes it
at most once every 15 seconds and once at closure, under `log_seconds` on its pool or on-demand
(default `None`) and the remaining rental allowance. The worker's own log goes,
once it has read its bootstrap, to its session's `out/` in appended pieces like a
job's, below; a worker that never read it sends its log whole, once, at exit. The guardian starts first and remains independent of publication.
A storage outage or death before bootstrap runs can leave an explicit evidence
gap. Reading logs never needs a live machine after its bucket evidence was
published or its output cached by the scheduler.

A job's output reaches storage as appended pieces, each sent once and never
rewritten, and the attempt's record names them; a reader reads each piece once.
New output is sealed into a piece after 15 seconds at first, waiting longer as
the attempt ages, to at most 5 minutes, so a 12-hour attempt leaves about two
hundred pieces. The last piece goes with the attempt's ending.

<!-- implementation-reference -->
```python
from etude.launch.client import logs

snapshot = logs(run="sweep/baseline@7", refresh=True, timeout=30)
for stream in snapshot.streams:
    print(stream.producer, stream.attempt, stream.received, stream.complete)
    print(stream.text)
for event in snapshot.diagnostics:
    print(event.observed_at, event.operation, event.message)
```

Diagnostics read retained database facts without provider or
bucket calls. With a server selected, those facts live on the server.
Preparation intent is recorded there before the submitting client publishes its
source. A disconnected submitting client can leave a preparing launch; it is
reported as unresolved and does not authorize automatic resubmission.

## The server

The server holds the database and matches waiting runs to machines. No
separate server is required: without a setting, CLI commands use this
computer's database and match while they run.

```sh
etude server                         # selection, last loop time, waiting-work warning
etude server set user@host           # an SSH address or configured alias
etude launch job.py --rent l4
etude logs --run RUN
etude fetch RUN checkpoint.pt out
etude server unset                   # use this computer again
```

A **server** is another machine that holds the launcher's database and
provider keys and runs the scheduler's loop; this computer is then its
client. No server is required: without a setting, commands use this
computer's database and match waiting runs to machines while they run.

The setting is a small `server` file beside the local database, under
`ETUDE_HOME` (default `~/.etude`). Setting or clearing it needs no database and
moves no existing records. With a server selected, administrative commands and
continuation run the same CLI there through the user's `ssh`. Experiment files,
training, eval, smoke, reports, database maintenance and storage reads run where
invoked. Launch/queue submit frozen intent; source checkout and fetch copy results
here. These operations use the existing Python client for server evidence and
file transport. The remote database's built-in `local` pool
is renamed `server`, including the default selection if it named `local`.
It is never advertised as the client's local machine.

`etude server set` installs this
client's own package over SSH, where it and its environment are kept by digest under
`ETUDE_HOME/programs`, and every later command runs that exact version. The two
sides therefore cannot be different versions; after the client changes, the
next command sends the new code first. Commands never upgrade the server's
launcher file: when it needs migrations, they refuse and you run
`etude database upgrade` on the server itself ([Upgrading Etude](#upgrading-etude)).
The server needs Python 3.12, uv
and working SSH keys. If it cannot take the program, `server set` fails
with the reason and the setting is not changed.

Commands on a server, and on any host reached by SSH, run through that
account's login shell. A bare SSH command gets a minimal PATH, which on a Mac
hides Homebrew's Python; the login shell gives the PATH a terminal there has.
`ETUDE_REMOTE_PYTHON` names a Python other than `python3.12`.

Provider and storage credentials belong in the server's own environment;
the client sends none. How a server obtains them is not settled: a command
arriving over SSH does not see secrets that a terminal session there loads.

The functions in `etude.launch.client` (`queue`, `launch`, `status`, `wait`,
`cancel`, `logs` and `fetch`) use the selected server and return the same
Python values. An explicitly constructed `Scheduler` remains the owner of a
local database.

Launch reads the experiment file and config on the client, runs the
controller's pass there, freezes and publishes source there, and sends frozen
runs and one source archive per repository/commit. The server stores archives
under `ETUDE_HOME/sources` and can launch or continue without a repository
checkout. A pass asks the server once for where the experiment's runs stand;
with a server selected it needs `--source` to say where their stored state
is. Logs stream back over SSH. Fetch copies to the client's
chosen directory and checks the returned digest before replacing its output.
A lost SSH reply is an error; launch is not automatically retried.

Run the standing loop on the server under a process supervisor:

```sh
etude server run
```

It uses the same matching operation as `wait`, but remains alive through an
empty queue. It records the last completed pass in `server-heartbeat` beside
the database. `etude server` and `status` warn when work is waiting and no
pass has completed in 30 seconds. Slow provider operations may also cause this
warning. `wait` still exits when its runs finish. Workers read their own disk or
bucket inboxes; they never need a reachable address for the server.

## Upgrading Etude

```sh
uv sync --upgrade-package etude-sdk
etude database status
etude database upgrade
etude status
```

Package installation and database upgrade are separate actions. Reads and ordinary
library opens never initialize, migrate or select a different database. All
compatible clients and experiments share `$ETUDE_HOME/launcher.sqlite` (default
`~/.etude`). Workers and their deadline guardians keep running during an upgrade.

`database status` reads local schema evidence without credentials, network or
record changes. SQLite provides a consistent snapshot of committed data, including
WAL data while compatible clients write. Inspection uses normal SQLite sidecar
coordination and never copies the database.
`database upgrade [--timeout SECONDS]` is an explicit local wait; its default
allowance is unlimited. Python uses the same implementation:

<!-- implementation-reference -->
```python
from pathlib import Path

from etude.launch.database import inspect, upgrade

home = Path.home() / ".etude"
state = inspect(home)
result = upgrade(home, timeout=None)
```

`Schema` names the canonical file, applied and pending migrations and any
compatibility problem. `Upgrade` records backups, applied migrations, exports
and reader requirements. Existing files are backed up before SQL changes;
recovery snapshots are retained without automatic pruning. A failed migration
rolls back its transaction. Rerun the explicit upgrade after resolving the error.

A backup restores database facts, not provider state: stop scheduling and
reconcile live machines and attempts before restoring or resuming any queue.
Recovery snapshots are never scheduling inputs.

### Compatible clients and development

Additive expansion retains old columns and defaults, so an older compatible
client can continue using the same file. Breaking upgrades raise the reader
floor; fresh clients refuse, and stale writers and scheduling passes stop before
new actions. Requests already in flight can still complete and need reconciliation.
A managed server hands over only to a validated installed program; otherwise it
exits with a restart instruction. It never installs code during handover.

Merged migration names and checksums travel in wheels and server archives.
A merged-main build can explicitly upgrade an operational home before release.
Unmerged or unproven migrations cannot advance operational state. Test them with
`etude database upgrade --development` in an explicitly empty fixture home;
this is never an alternative home for real experiments or inherited work.
There are no automatically selected copies. Snapshots are validation inputs,
never independent queues to schedule.

### Writing a migration

Add one named SQL file under `src/etude/launch/migrations/` with dependencies:

```sql
-- after: run_resume_and_writers
CREATE INDEX runs_name ON runs(json_extract(spec, '$.name'), seq);
```

Merged migrations are immutable. `scripts/check_migrations.py` checks main's
files as well as released checksums. Release metadata adds version names and
reader floors. A `-- breaks: reason` header declares incompatible stored JSON
changes even when DDL is additive. Prefer compatible expansion; a destructive
contraction must name its preceding expansion and durably export superseded
facts before SQL commits. Preserve shipped worker journal decoding.

## Safeguards and bills

```text
$ etude ondemands
l4       L4 on aws; up to 1 at once under $0.90/hour; safeguards: machine timer, launcher cleanup; records in west
l4-spot  L4 on aws, spot; up to 1 at once under $0.70/hour; safeguards: machine timer, launcher cleanup; records in west
rtx5090  RTX 5090 on lium; up to 1 at once under $0.80/hour; safeguards: provider lifetime, launcher cleanup; records in west
```

`etude ondemands` lists each on-demand's safeguards: what ends its machines.

| Safeguard | What it is | Providers |
| --- | --- | --- |
| machine timer | The machine deletes itself at its deadline, with no launcher alive. | RunPod, Vast, EC2, Lambda |
| provider lifetime | The provider removes the machine at a time set when it is made. | Lium |
| launcher cleanup | The scheduler's loop deletes it; needs that loop running. | all |

RunPod's timer begins before client installation and ends the pod at the
deadline or when its dollar cap runs out at the actual price. Autorun uses the
same timer. A failed timer triggers deletion. The worker ends work early
enough to leave up to two minutes for final evidence delivery. EC2's and
Lambda's timers are armed by the host before anything is downloaded
([providers](providers.md#aws-ec2)).

A scheduling pass requests deletion if the worker has not started by the
rental deadline or an earlier configured `--startup-seconds` cutoff. The
default allows startup through the rental deadline. The machine timer runs
independently once armed. RunPod cannot run that timer while an image is pulling;
EC2 and Lambda arm theirs on the host, before the image is pulled.

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

## The run promise

```sh
etude launch ppo.py --rent runpod-cpu8   # attempt 1: its machine is lost at update 870
etude status RUN                         # running, updates 1400, ~$0.61: attempt 2, from update 800
etude status RUN                         # running, updates 2140, ~$1.02: attempt 3, after a planned stop
etude logs --run RUN                     # every attempt, and how each ended
etude launch ppo.py --resume         # after it ended unfinished: another attempt, on current code
```

A run ends when it reaches its target, fails, is cancelled, or uses up its hours
or dollars, or exhausts its attempts. A lost machine or a planned stop can
retry within those bounds. The job is called again as a new RunAttempt of the
same run, with out (`run.out`, `ETUDE_OUT`) as earlier attempts
left it, and `run.resume` loads the state `run.save()` kept there
([Training](../training.md#carrying-on)).

- **A lost machine** is the launcher's finding: the attempt ends `lost` with
  its cause.
- **A planned stop** is the job's own: `run.steps` saw the stop, the pass in
  progress saved, and the next raised `etude.stopping.Stopped`
  ([Training](../training.md#carrying-on)).
  The attempt ends `stopped`. A command job says the same by exiting 75
  (`EX_TEMPFAIL`, "try again later"), which is what a typed job's process
  exits with when its function raises `Stopped`.
- **A failure** is any other exception or nonzero exit: the attempt and the
  run end `failed`, and nothing is called again.

The run's hours, dollars and `max_attempts` span all attempts. Hours count code
execution; dollars include attributed setup and failed attempts.
Definite capacity refusals acquire no machine and spend none of these limits.

A spot machine lost under a started attempt ends that attempt
`lost (spot_machine_lost)`. It is retried like any lost machine and spends
hours and dollars, but not one of `max_attempts`:

```sh
etude launch ppo.py --rent l4-spot   # attempt 1: AWS takes the machine back at update 870
etude status RUN                     # running, updates 1400, ~$0.41: attempt 1, from update 800
etude logs --run RUN                 # both tries, the first ended lost (spot_machine_lost)
```

The cause does not claim the provider reclaimed the machine: from outside, a
crashed spot machine looks the same, and is treated the same. A spot machine
lost before its attempt started is `machine_lost_before_start`, as at the
standard price, and counts.
The default `max_attempts=3` includes planned stops: a healthy run needing a
fourth rental window ends `stopped` after its third planned stop. Increase
`max_attempts` on the job for a longer run.

`run.steps` also stops before the run's own hours limit, reserving
time to save. A planned stop there can trigger another rental for the remaining
allowance: stopping after 560 of 600 allowed seconds leaves a 40-second attempt
while attempts and dollars remain. If that attempt uses up its allowance, the
run ends `out_of_time`; if it stops early again, it can retry again. Repeated
planned stops remain bounded by `max_attempts` and the run's hours and dollars.

At a machine's deadline, an attempt that has not saved and stopped ends
`lost`. Exhausting the run's own hours or dollars ends it `out_of_time` or
`over_budget`; `etude cancel` ends it `cancelled`. Exhausting attempts leaves
the last outcome, `stopped` or `lost`. Without saved state,
the next attempt starts from the beginning. Each attempt's code starts at
most once; an unresolved start remains fenced and can end `lost` when its
execution cannot safely be repeated.

The launcher has no checkpoints of its own: out and checkpoints
are its tracked files. They are carried to the bucket as they are written, so
they survive pod deletion. A later attempt on the same machine finds the
directory where it was; if the directory is gone, as after a wiped disk, it is
restored like a fresh machine's. On a fresh machine with a bucket it is given back,
before the job starts, the run's records and its whole `out/` as last
carried: the save `out/state/current.json` names and every checkpoint
published under `out/checkpoints/`. A historical run's latest checkpoint stored under
`checkpoints/` is restored too. A checkpoint recorded
inside `out/` is restored only while it belongs to that directory. To an owned
host without a bucket, the scheduler relays the same from the bucket or the
host that last ran the run. A retry waits until its complete restoration is
available. Automatic recovery retains the run ID, seed and checkpoint
ordinals. `etude continue` instead goes further than the
target or branches from a completed run, making a new run with a parent.

A run that ended unfinished, because it failed, was cancelled, or used up its
hours, dollars or attempts, is given another attempt by name: `etude launch
job.py --resume`. The file is given because the attempt runs its current
commit, which is what a fixed bug needs; the attempt records that commit. The
run keeps its id, seed, config, out and checkpoint ordinals, and the job's
limits as the file now states them bound the whole run, earlier attempts
included, so raise `max_hours`, `max_usd` or `max_attempts` there to give it
more. `--resume --until updates=6400` gives the same run a later target,
and is how a finished run goes further without becoming a new one. A resume
is refused before anything is rented when the run finished and no later
target is given, when the file's config or the seed differs from the run's
(the error names the fields), or while an attempt of the run is still
allocated. A refusal changes nothing recorded about the run.

Run addresses print as `EXPERIMENT/JOB@SEED`, in full; a run stored under a shorter spelling keeps it. Commands accept that address;
`etude status RUN --experiment EXPERIMENT` and `etude continue --run RUN --experiment
EXPERIMENT --until clock=value` also accept the run id alone.
`etude launch FILE --resume` resumes the selection from its `--experiment`,
`--job` and `--seed` flags. `--job` and `--seed` select declarations from the
file; neither names a new job or seed. Use `--experiment` for a new variant.

A launch whose run id is taken in its experiment is refused the same way,
before renting: ids are never reused and a taken id is never resumed. The
run's folder is the authority. Its `run.json` is written create-only, so a
second claimant fails on any computer, including one whose launcher database
has never seen the run.

Delivery publishes changed `out.json` membership after its files succeed.
Each entry names a key, SHA256 digest and byte length.
Recovery uses membership to select files, checking current listing lengths
and recorded checkpoint digests. A file the job rewrites in place can be
newer than its membership if delivery stopped between the file and the
index; recovery keeps that newer file. Continuation instead uses the
published fingerprints and verifies them when delivering the child's inputs.
A declaration sees the same membership as a run's `out` in the experiment's
state: only keys `out.json` lists and the source still holds, never a file
that is not yet indexed
([Files from another run](../training.md#starting-from-another-run)).

State kept with `run.save()` never recovers as a mixture of two saves. Each
save is a folder of files that are written once and complete in itself
([Training](training.md#what-is-on-disk)). A delivery pass reads
`out/state/current.json`, sends the files of the save it names, then
`current.json` itself, the one file that is rewritten. A delivery interrupted
before that file leaves the stored one naming the save before, whose files
are untouched, and recovery restores that one.

A save is uploaded once and whole: a file with the same bytes as the save
before is sent again under the new folder. A save the job replaced before a
pass reached it is never sent, so saving more often than delivery can keep up
with adds no uploads.

Replaced saves are removed without the job's help. A save on the machine is
kept until storage has acknowledged a `current.json` naming it or a later
one; before that it may be the only copy. Removal happens on the pass after,
not at a set time, and may lag: nothing depends on a replaced save being
present or gone.

| Where | Removed | By | Until then |
| --- | --- | --- | --- |
| the machine | save folders numbered below the one storage last acknowledged in `current.json`; with no storage, below the one `current.json` names | the courier, or `run.save()` with no storage | the unsent saves since the last delivery |
| a bucket, grant can delete | save folders numbered below the stored `current.json`'s | the courier, after storing that `current.json` | two saves for about one pass |
| a bucket, upload-only grant | the same | the scheduler, with the launcher's own keys, each time its loop looks at the run | one replaced save for each save delivered since its loop last ran |

The last row is the one to know about. The courier cannot delete with an
upload-only grant (S3 without `sts:GetFederationToken`, or after a grant's
temporary keys expire), and without a server the loop runs only
while a command such as `etude launch` or `etude wait` does. A run saving
every five minutes then leaves twelve replaced saves an hour in the bucket
until the next such command. `etude server run` keeps the loop running;
R2 grants and S3 federation grants delete on the machine and do not wait
for it.

Recovery and continuation exclude removed files, even if their bytes remain
in storage. Recovery removes stale out files on a reused host. Deleted
out checkpoints remain available when selected explicitly. An empty `out`
can resume from the latest checkpoint copied into `checkpoints/`; it never
restores a removed out checkpoint or falls back to an older record.

`max_attempts=1` gives the job one counted attempt. A capacity refusal or the
loss of a running spot machine leaves that allowance available.

## Progress and continuation

Clocks are named counts chosen by the job: training steps, learner transitions
and optimizer updates can be distinct clocks. Targets such as `steps=63000`
are absolute, including after continuation. Hours and dollars bound execution.
Training code reads `ETUDE_OUT`, `ETUDE_OUTPUT_DIR`, `ETUDE_STOP_FILE`,
`ETUDE_DEADLINE`, `ETUDE_SEED`, `ETUDE_CONFIG`, `ETUDE_UNTIL_CLOCK` and
`ETUDE_UNTIL_VALUE`. A job's own `environment` variables are set too; Etude's
`ETUDE_*` names take precedence.

| Variable | What it is | Tracked code uses |
| --- | --- | --- |
| `ETUDE_OUT` | Out, the same on every attempt, carried to storage as it is written and restored before each attempt. `etude fetch` reaches its files by their path below it. | `run.resume`, `run.save`, `run.checkpoint`; `run.out` |
| `ETUDE_OUTPUT_DIR` | This attempt's output directory: files to keep under `attempts/ATTEMPT/`, recorded as the attempt's outputs for `etude fetch`. A later attempt does not see it. Etude's own `etude-result.json` is written here; it is not an output and is not stored. | nothing; tracking reports progress |
| `ETUDE_STOP_FILE` | Appears when the attempt should stop. | `run.steps`; `run.should_stop` |
| `ETUDE_DEADLINE` | When the attempt's window ends, as an ISO time. The process is killed then. | `run.steps`; `run.should_stop` |
| `ETUDE_UNTIL_CLOCK`, `ETUDE_UNTIL_VALUE` | The launch's absolute target on a named clock. | `run.steps("updates", to=config.updates)`; `run.until` |
| `ETUDE_SEED` | The run's seed. | `run.seed` |
| `ETUDE_INPUTS_DIR` | The files and checkpoint folders the run was given. | `run.input(NAME)` |

A job can be given files by name:

<!-- implementation-reference -->
```python
from etude.jobs import TrainingJob
from etude.spec import Input

policy = Input(
    name="policy.pt",
    key="etude/launch/EXPERIMENT/JOB@SEED/checkpoints/policy.pt",
    sha256="…",
    size_bytes=1048576,
)
job = TrainingJob(
    name="finetune",
    train=finetune,  # opens run.input("policy.pt")
    config=FinetuneConfig(),
    inputs=(policy,),
    max_hours=1,
    max_usd=Decimal(2),
)
```

An evaluation job's `checkpoint=` and a training job's `start=` accept another
run's `CheckpointRecord` (see [Typed jobs](../launcher.md#typed-jobs)). The latter is read
as `run.input("start")`: a checkpoint folder arrives whole, each member verified
against its digest in the record,
and the run records the checkpoint it started from
([Training](../training.md#starting-from-another-run)).

A declaration can make verified inputs from another run's stored out files,
using its membership and source
([Files from another run](../training.md#starting-from-another-run)):

<!-- implementation-reference -->
```python
packs = state.run("player@0").out.matching("packs/*.pack")
job = TrainingJob(
    name="belief", train=fit, config=FitConfig(), inputs=packs.inputs("source"), max_hours=2
)
```

Each member arrives at `ETUDE_INPUTS_DIR/source/PATH`, as one verified file.
It does not seed the consumer's `out`.

`key` names an object in the bucket of the storage the machine keeps its
records in (the on-demand's, or an owned host's `--storage`). The launcher gives
the worker a scoped read link; the worker downloads it with the same verified,
resumable download as source and checkpoints, into the machine's cache, which
keeps it for the session. Each attempt verifies its length and SHA256 again
before the job starts (`inputs: verified NAME (BYTES bytes, SECONDS s)` in its
log) and links it, read-only and without a copy, at `ETUDE_INPUTS_DIR/NAME`.
Bytes that do not match the digest are refused: the
job's command never runs and the attempt ends `lost (input_undelivered)` with the
reason in its log, and is not retried, since a retry would be given the same bytes.
A worker's cache may use what it holds plus half the disk
free when its session began. Inputs are part of the job's identity. A machine with no
storage can be given only a run's file (`Input.run`, as continuation does). Unless
the machine keeps records in the same storage as the machine that ran that run,
the launcher relays the file, verified, from that storage or that machine's disk.
The attempt waits while neither can be reached, and ends
`lost (input_undelivered)` when the file or its record is gone, or its bytes do not
match the record.

The job is also told who it is: `ETUDE_RUN` (the run's id), `ETUDE_JOB` (the
digest its job's runs share), `ETUDE_JOB_NAME` when the job has a name,
and `ETUDE_ATTEMPT`. It runs in
`ETUDE_SOURCE_DIR`, its commit's files without `.git`; [evaluation](evaluation.md)
hashes identity files relative to it. `ETUDE_EXPERIMENT` is the launch's
`--experiment NAME`, else the job's explicit experiment, else the launched
file's stem; files defining `eval_jobs(state)` or `report()` require their own
stem. The experiment is part of the job's
identity. [Tracking](tracking.md) reads these, so a launched recipe names
only its clocks. A tracked script
does not write `etude-result.json` itself: tracking reports its clocks.

A storage holds one folder per run, below its experiment's, shared by every
attempt on every machine, and the machines' own session files apart. Runs
stored before experiments had folders stay at `PREFIX/RUN/` and read the same:

```text
PREFIX/EXPERIMENT/JOB@SEED/           everything about one run
  attempts/ATTEMPT.log/OFFSET    what the job printed, in pieces named by first byte
  attempts/ATTEMPT.json          when it started, its progress, how it ended, its pieces
  attempts/ATTEMPT/NAME          files the job left in its output directory
  run.json  outcome.json  metrics/ the run's tracked files, as they are
  checkpoints/                   checkpoint files copied from outside out
  out/                           out (`run.out`), as the job keeps it
    state/current.json           names the save to load; stored after that save's files
    state/save-N/                one save by run.save(), whole and never changed; replaced ones are removed
    checkpoints/CLOCK-VALUE/     each published checkpoint, never changed
  out.json                       keys, SHA256 digests and byte lengths in the last carried out
PREFIX/sessions/SESSION/         one machine's instructions, inbox and notes
  out/worker.log/OFFSET          the worker's own log, in pieces named by first byte
  out/worker-log.json            where its pieces end
  out/ended                      the end receipt, sent after everything else
PREFIX/sources/SHA256.tar        a commit's source archive, named by its digest
```

The job is given `ETUDE_TRACK_DIR`, `track` beside the `sessions` directory
(on this computer `~/.etude/track`), and tracking keeps the run's files in
`ETUDE_TRACK_DIR/EXPERIMENT/JOB@SEED/`. The courier copies that folder into the run's folder
in storage while the job runs; the job still holds no keys. A second attempt,
on this machine or another, writes into the same run folder. The courier and
tracking's own `sync` follow one set of rules (`etude.carriage`): files ending
`.open` or `.partial` wait until they are whole, `.tail` files until the
attempt has ended, and anything under a folder starting with `.` stays; each
pass sends checkpoint files before their records; checkpoint bytes count as
`checkpoint` in usage; and an object under `checkpoints/` is sent once and
never replaced. `ETUDE_OUT` is that folder's `out/`: a file in it is hashed on
each pass and sent again when it has changed, except one a checkpoint record
names, which is sent once before its record like any checkpoint file. The
membership is published after all files in that pass succeed, including when
no files remain. A courier restart reconstructs membership from the local
directory. This also works with upload-only grants; absence does not depend
on deleting bucket objects. Under such a grant a replaced save's objects stay
in the bucket, outside membership and never restored, until the scheduler
deletes them ([The run promise](#the-run-promise)).

A job reports its progress by atomically replacing `etude-result.json` in the
output directory; `etude status` shows it:

```json
{"clocks":{"steps":26000,"learner_transitions":52000}}
```

A job that counts its work adds a `tally`, and `status` gives the fraction, the
recent pace and the time left. An eval run reports one by itself, counting
the matches of every tournament it plays:

```json
{"tally":{"unit":"matches","done":412,"total":1800,"failed":0,"per_hour":940.0}}
```

```text
running, 412/1800 matches, 940/hour, ~1.5 hours left, ~$0.31
```

The courier publishes how the job ended as soon as it knows, then final outputs,
then the completion note. A machine that ends before the note arrives leaves the
run with the outcome the job reported, and a diagnostic that its files were not
confirmed stored, rather than a lost attempt.

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

```sh
etude continue --run RUN --until updates=6400 --source s3://bucket/etude/launch
etude continue --run RUN --until steps=63000 --ordinal 4 --source s3://bucket/etude/launch

etude cancel RUN
```

Continuation starts a new run of the parent's job and seed from the parent's
out: the child's `out` begins as a copy of the parent's, as last
carried, so `run.resume` loads the parent's last save (weights, optimizer,
anything else registered) and `run.checkpoints()` lists its checkpoints. The parent and its directory
are unchanged. `--until` is the child's absolute target and must exceed its
starting position on the named clock. `run.steps("updates", to=config.updates)`
counts to it; `--until` alone extends the
run, and its config stays unchanged. In Python,
`continue_from(parent, "updates=6400", source=...)`.

To change other recipe settings too, a typed job accepts `--config
'{"rate":0.001}'`, a JSON object validated against the job's config type before
anything is queued. Unknown top-level fields are refused by name unless the
author's model allows extras; validation problems print one line per field.
Only those config fields change. Python callers pass their
complete validated model with `continue_from(..., config=changed)`.
The launch target stays separate from config fields and takes precedence for
its clock; Etude does not infer a config field from a clock name.

Alongside out, a continuation inherits the parent's latest checkpoint whenever
its file is still available: a member of out or copied into `checkpoints/`.
`run.checkpoints()` lists it, in the child's out or delivered inputs.
A removed out checkpoint is never restored implicitly, and no older checkpoint
is chosen in its place. Populated out uses the parent's recorded position for
lineage and target validation; empty out with a copied checkpoint uses that
checkpoint's position, even if the parent logged later metrics.
Empty out with no available latest checkpoint is refused before queueing:
there is no saved state to continue. The error distinguishes a parent that
recorded no checkpoint from one whose latest out checkpoint was removed;
use `--ordinal N` to select a recorded checkpoint explicitly. Populated out
can continue without a checkpoint.

`--ordinal N` starts from one recorded checkpoint instead. A historical parent
without out also uses its latest checkpoint. In these cases the child's `out`
starts empty. A checkpoint delivered as an input names its parent run
(`Input(run=PARENT, key=RECORDED_FILE)`); its bytes are verified by digest.
That key may be under `out/` when the checkpoint was recorded there by reference.

`etude.jobs` reads the parent's records and `out.json` from `--source`.
Continuation uses those fingerprints without downloading out files; inputs
are verified when delivered. Sources without membership, such as a local
run, are hashed while creating the continuation. The source is where the
parent's tracked folder is (its bucket, or a runs directory). Without
`--source`, it reads this computer's runs directory, else the records on the
owned host that last ran the parent. The copy comes from the bucket where the
parent's folder was carried, or, without one, from that host's folder for the
parent. To an owned host without a bucket the scheduler relays it before the
run starts, from the parent's bucket or host, and the run waits while neither
can be read. Tracking is told the parent and where it stood in
`ETUDE_CONTINUES`, and the child declares that lineage itself. The `continue`
command retains the parent's pool and on-demand. With a bucket, the courier
stores the child's initial directory before allowing the job to start, so an
immediate machine loss can restore it. Each copied file is an input verified
against its recorded digest and length. Keep the parent still until the child
starts; if those bytes change before delivery, the child waits
or reports an undelivered input instead of starting with different state.

## Ownership and failure behavior

The code has four ownership boundaries. Kubernetes calls the experiment pass a
controller; Slurm and Ray use scheduler for placement; Karpenter and SkyPilot
use provisioner for machine acquisition. The worker executes delivered packets.
These boundaries are not a four-step import chain: the scheduler composes
provisioning and execution, while provisioners and workers remain independent.

| Level | Modules | States it owns |
| --- | --- | --- |
| Controller | `etude/controller/`; declarations and state types stay in `etude/jobs.py` and `etude/experiment.py` | experiment decisions from `queued`, `running`, `stopped`, `finished`, `failed` |
| Scheduler | `etude/launch/scheduler/`, composed with execution and persistence modules | `Phase`, `Outcome`, `Cause`, `Status` |
| Provisioner | `etude/launch/provisioner/`, including `providers/` and `images.py` | `Machine.state`: `starting`, `available`, `unavailable`, `deleted` |
| Worker | `etude/launch/worker/` | packet execution, local evidence and delivery |

- **Controller.** `controller/advance.py` reads declarations, observes state
  through `client.standing(experiment)` and storage, and calls `launch` or
  `queue` for new work. It holds the experiment's `--max-usd` and nothing else
  between passes. Declaration reading, smoke and local composition live beside it.
- **Scheduler.** `scheduler/loop.py` owns database writes, locks, runs, queue,
  placement and job limits. `costs.py` holds dollar arithmetic and `placement.py`
  holds placement values; neither owns lifecycle state. `executor.py` composes
  startup (`startup.py`, `ssh.py`) with directory, SSH or bucket journals.
  `server.py` forwards CLI commands and runs the standing loop; `source.py`
  freezes and publishes git source. These modules read no experiment file and
  hold no limit spanning runs.
- **Provisioner.** `provisioner/machines.py` defines allocations, requests,
  safeguards and definite capacity refusal. Provider implementations obtain
  machines, supply stock-image boot, read prices/bills and terminate exact
  identities. `Machines.find` answers with the provider's id alone;
  `Machines.allocation` reads the facts and may fail without hiding the
  machine. `OwnedMachine` represents an existing address. Provisioning knows
  nothing of jobs or runs. Rented machines start themselves; owned hosts use
  their configured connection.
- **Worker.** `worker/attempts.py` executes packets; `worker/courier.py` moves
  inboxes and evidence. Neither places runs nor acquires machines. The
  standard-library guardian enforces the rental deadline independently of
  dependency installation and job execution.

The SQLite file is `$ETUDE_HOME/launcher.sqlite` (default `~/.etude`), carried
across releases by migrations; see [Upgrading Etude](#upgrading-etude). Versioned
writes claim each session once. A rented machine is known by the provider's id
for it, which is all that recording, terminating or deleting it at a deadline
needs. The worker's first receipt (`started_at`) makes it `available`; a provider
id alone leaves it `starting`. Its price, hardware and address are a separate
read: while that read fails, each pass retains a `machine readiness` diagnostic for
the session, the machine stays `starting`, and it is deleted at its startup
deadline or by `terminate`. A lost create reply is looked up, never repeated;
if the provider still has no such machine at the rental's deadline, or when it
is terminated, it is recorded as never acquired: it costs nothing and
no longer counts toward the on-demand's machine limit. Lium settles this an hour
after the deadline, once [its own removal timer](providers.md#a-pod-etude-made-is-always-deletable)
has passed; Vast keeps looking, because its inventory can lag. A 4xx reply to a
create (except 408) is an answer, not a lost reply: nothing was made, so the
session is recorded refused at once with the provider's reason (`no reason given`
when it gave none), its attempts fail before start as `capacity_refused`, and
nothing is left `starting` or fenced. The reason is the reply's `detail` or
`title` field when the body is JSON, else its text; at most 2 KiB of an error
body is read and at most 300 characters kept, through the shared sanitizer. A
launch reports it as `provider refused: REASON`. A queued run's status reads
the reason from its latest attempt's session, never from diagnostics, so it
survives scheduler restarts and the retry pause. Queued work does not ask that
on-demand again within 60 seconds of a provider's refusal; an explicit `launch`
is not paused, and releasing a claim that never reached the provider pauses
nothing. Attempts ending as `capacity_refused` remain in the history, with their
original numbers, but do not count toward `max_attempts` or the status attempt
count. Scheduling, status and explicit resume use that same budget rule.
Timeouts, no reply, 408, 5xx and unreadable or mismatched replies stay unknown.
A remote start has an exclusive claim; a lost SSH reply cannot start another
process. An unreachable machine is unknown. A lost attempt requires evidence that its process or
provider machine is gone. Infrastructure losses retry up to `max_attempts`,
including confirmed loss after code started; a spot machine lost after start
(`spot_machine_lost`) retries outside that count, as `capacity_refused` does. If startup intent reached storage
but an attempt claim did not, its start is uncertain and it is not retried.

Tables are `machines`, `sessions`, `rentals` (one per rented machine, keyed by
machine) and `attempts`. What the provider said about making and deleting a
rented machine is stored on that machine's one session and read as Machine.
A record refers to another by its name or id as a plain string; the field
name says what it refers to.

A session on a machine you added has no deadline; every run still enforces its hour limit.

`etude wait` returns when its runs are done and no session that ran them has
been told to finish without its end confirmed; without names, when queued work
is finished and no such session remains. Confirming an end records the
session's `ended_at` and the machine's deletion together and collects its final
logs, so `etude machines` and `etude logs` are settled when `wait` returns. A
kept rental (`etude rent`) is never told to finish and does not hold `wait`. A
`wait` that times out there names the machines still finishing. `launch` and
`--quote` confirm ends whose receipt has arrived before they count an
on-demand's machines; they run no other part of a scheduling pass and rent for
no other run. A machine still finishing counts toward the limit, and the refusal
names it: `at its machine limit (finishing: demo-cpu-d96e90953e46)`. Started
workers execute their delivered work independently; a launched machine with
an inbox is cleaned up on the next scheduling pass or at its own deadline.
`launch` first acquires machines for the runs it can place. Each machine
then takes waiting work that fits its remaining window. `etude server run` keeps matching new work
after commands exit. Sweeps remain open.

## Layout and dependency rules

The names follow established usage: Python's SQLite interface calls its owner a
database; concurrent.futures calls an execution owner an executor; Pydantic calls
serialized message shapes schemas. RunPod supplies machines; S3 stores evidence.
SSH remains the connection to a host, including its disk.

```text
etude/
  jobs.py  experiment.py
  controller/  advance.py  declarations.py  smoke.py  local.py
  train/  eval/
  launch/
    client.py  server.py  cli.py  commands.py
    records.py  schemas.py  database.py  executor.py  source.py
    ssh.py  startup.py  environment.py  errors.py
    scheduler/    loop.py  costs.py  placement.py
    provisioner/  machines.py  images.py  providers/
    worker/       main.py  attempts.py  courier.py  remote.py
    storage/      disk.py  bucket.py  s3.py
    migrations/
```

`tests/launch/test_layout.py` enforces the boundaries:

- Workers cannot reach the scheduler, database, client, CLI or server through
  imports. Workers execute packets and return evidence, without placing runs.
- The provisioner imports no scheduler, executor, database, client, server, CLI,
  worker, `Job` or `RunRequest`. No launcher module imports the controller,
  `etude.jobs` or `etude.experiment`.
- The scheduler imports no client, server, CLI, controller or concrete provider.
  Its costs and placement modules cannot import the loop, database, execution,
  client or providers.
- Concrete providers are selected in `client.py`; generic cleanup and package
  manifests keep provider choices out of workers and SSH.
- Guardian imports, including `provisioner/providers/runpod/pod.py`, use only
  the standard library. `environment.py` is also independent of dependencies.
- Storage cannot reach machine connections or startup through imports.
- Imports use full module paths and all package initializers stay empty.

Shared records and bootstrap facts stay outside level folders so their consumers
need no reverse dependency. `launch/records.py` owns launcher records;
`launch/schemas.py` owns packet, result, bootstrap and inbox models.
`executor.py` owns `Executor` and its `Execution` interface.

The stock-image bootstrap and guardian live in
`provisioner/providers/runpod/pod.py`; `provisioner/providers/package.py` builds
the archive and its bare-Python subset. SSH's pre-install loader includes the
same bootstrap dependencies. Startup follows the machine's kind. A session's
packet, journal folder and bucket keys are named by its session id.

## Command expectations

Each command declares `Kind.read`, `Kind.ask` or `Kind.wait` beside its definition
in `cli.py`. Declarations do not change execution; every read is pinned at zero
provider, bucket, SSH and git calls with no provider key set, and other current
deviations remain pinned in counting tests. `status` answers as of the scheduler
loop's last pass and prints when that was (`last loop: ...`); run `etude wait` or
`etude server run` to move work along.

| Kind | Commands | Allowed dependency | Expected result |
| --- | --- | --- | --- |
| Read | `status`, `runs`, `logs`, `pools`, `ondemands`, `machines`, `providers`, `storages`, `server`, `usage` | database only | immediate answer with freshness |
| Ask | `queue`, `cancel`, `terminate`, `rent`, `pool` and `ondemand` mutations, `default`, `machine add`, `provider set`, `storage add` | database only | recorded intent and an id where applicable |
| Wait | `wait`, `launch` (with or without `--quote` or `--resume`), `availability`, `fetch`, `logs --follow`, `source`, `continue` | other machines with a time limit | completion or the limit |

Server selection mutations are asks. `worker` and `server run` are waits
because they deliberately execute long-lived loops; they retain their existing
lifetime rules. An experiment file's `training_jobs()` and `eval_jobs(state)` are input
functions, not launcher commands. `queue` stays an ask for a file that takes no state;
for one whose declarations take `state` it reads the source once before recording. `eval`
is a wait: it reads the source and plays on this computer, and needs no provider key or
launcher database.
`smoke` is a wait on this computer alone: it runs the file tiny in a temporary
home and touches neither the database nor another machine. `train` is the same
kind: it calls a file's `train` here, writes the run to this computer's runs
directory, and needs no provider key, launcher database or network; `--seconds`
is its optional limit. `queue --smoke` and
`launch --smoke` run it first, on this computer.

The full expectation also gives external work to the scheduler loop, makes asks
safe to repeat with caller-chosen identities, restricts waits to watching, bounds
lists, states answer freshness and prevents one supply's trouble delaying another.
Commands require only dependencies implied by their kind: reads work without
provider keys or a network. The rest are documented expectations for the follow-up;
this foundation does not enforce them, add ids or deadlines, or change server
forwarding.

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
one scheduler pass, a three-run git-backed launch, and five Etude modules loaded
by help. The help check excludes platform-dependent standard-library module totals.
The physical transport test checks provider, bucket and SSH counts with local replies.
Zero external effects for queue are a target; wait advancing work, repeated SQL
scans and repeated git preparation across runs are known debt.

Scale cases are excluded by default. Run them explicitly:

```sh
uv run pytest tests/launch/test_measurement.py -m scale -s
```

Records-pass baseline re-measured on 2026-11-11, with delivery-age lookups and
the same number of active workers and runs; no paid machines or network. Counts at 1 and
10 are measured; 100 and 1000 use the pinned coefficients and await the opt-in
scale check at gate:

| Workers | Operation | Provider calls | Journal calls | SSH | Git | SQL statements |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 10 | status | 0 | 0 | 0 | 0 | 501 |
| 10 | runs | 0 | 0 | 0 | 0 | 171 |
| 10 | scheduler pass | 20 | 10 | 0 | 0 | 263 |
| 100 | status | 0 | 0 | 0 | 0 | 5001 |
| 100 | runs | 0 | 0 | 0 | 0 | 1701 |
| 100 | scheduler pass | 200 | 100 | 0 | 0 | 2513 |
| 1000 | status | 0 | 0 | 0 | 0 | 50001 |
| 1000 | runs | 0 | 0 | 0 | 0 | 17001 |
| 1000 | scheduler pass | 2000 | 1000 | 0 | 0 | 25013 |

Queueing one source-free run costs 23 SQL statements and no external calls.
A wait with a zero timeout still advances one pass: 4 provider calls, 1 journal
call and 78 SQL statements: packet construction reads its rental once for the
deadline, cap and reuse policy. Queueing three git-backed runs costs 57 git commands
and 38 SQL statements. These are fixed fixture baselines, not physical HTTP request
estimates or desired performance budgets.

Stock-image creation adds the following physical HTTP baseline: each create
uses 2 provider calls (availability and create), with 3 bucket calls on first
use (package read, package put, receipt put), 1 on reuse in the same command,
and 2 in a new command when the digest already exists. Every case uses zero SSH
and git calls. Package and bootstrap reads at pod boot are separate worker-side
requests. Signed-link generation makes no HTTP request.

Looking up a retained machine receipt uses 1 bucket read and no provider call
or receipt write. Recovering a lost create reply uses 1 bucket read, the provider
inventory (1 call per page), and 1 receipt write when found. Terminating a Lium
machine whose id is retained uses 1 delete and 1 status read, with no inventory
read and no SSH; without a retained id it adds the inventory. No path sends a
second create for a session. Health checks remain separate from receipt
lookup; no path waits for a rented machine's SSH address.

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
are transport caches, not the source record. With a server, the
submitting client snapshots and pushes, then sends the frozen metadata and tar.
The server does not pull launch source itself in this build.

A run displayed by `etude status` includes a source line, for example:

```text
  source: 79e54361b230 on distributed, main@3f2a1c9 "Add replay buffer sharding", v0.3.0 + 14, 2 commits ahead, plus uncommitted changes
```

`status` defaults to unfinished launched runs. `runs` lists every launched run,
finished ones included, and replaces `status --all`. Runs started with
`etude train` are files outside the launcher database; read them with
`etude report FILE` or `etude checkpoints`.
Filters combine with AND; `--job` accepts a
job digest or display name.

```sh
etude runs
etude runs --experiment capacity
etude runs --job JOB_ID
etude source RUN_ID recovered-source
etude source RUN_ID                     # creates etude-source-RUN_ID
```

`source` fetches the recorded commit from origin and checks it out detached in
a new directory on the requesting client, including when a server is set.
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
status adds 27 statements per active worker, runs adds 6 and a scheduler pass
adds 9. Remote counts are unchanged.


### Startup allowance

<!-- implementation-reference -->
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
phase requires the scheduler's loop running and a confirmed provider identity.

Server installation also accepts `startup_seconds` in Python and
`etude server set ADDRESS --startup-seconds SECONDS`. The default is no
timeout; a configured allowance governs both the local wait and remote install.
A timed-out wait is not proof the remote installation stopped. A log read retains
its own explicitly chosen deadline. Downloads, SSH key generation, sshd startup
and guardian readiness inherit this budget without separate 30/60-second caps.


### Prepare a framework image before renting

<!-- implementation-reference -->
```python
from etude.jobs import TrainingJob
from etude.launch.provisioner.images import resolve_image
from etude.launch.records import MachineTemplate
from etude.spec import Runtime

image = resolve_image("pytorch/pytorch:2.10.0-cuda12.8-cudnn9-runtime")
template = MachineTemplate(provider="runpod", gpu="NVIDIA L4", image=image)
job = TrainingJob(
    name="base",
    train=train,
    config=TrainConfig(),
    runtime=Runtime(python="python", packages={"torch": "2.10.0+cu128"}, cuda="12.8"),
    setup='"$ETUDE_PYTHON" -m pip install etude-sdk',  # the stock image has no Etude
    max_hours=1,
)
# Custom public or private image; uses Docker's configured registry credentials.
custom = resolve_image("registry.example/team/training:release", timeout=30)
```

```sh
etude ondemand image gpu pytorch/pytorch:2.10.0-cuda12.8-cudnn9-runtime
etude machines --all
```

Preparation waits for registry metadata with `timeout=None` by default. A positive
`timeout` bounds the whole operation; CLI `--timeout 0` disables the cutoff. It needs Docker
CLI with Buildx and uses `docker login` credentials; no Docker daemon, layer pull,
image build or machine rental is performed. The default platform is `linux/amd64`.
The output records the requested reference, immutable platform manifest and
verified OS/architecture. Unsupported or ambiguous platforms fail before rental.

Pair the image with an explicit `Runtime` naming its Python/Torch/CUDA
requirements. The job probes `python` in that image and checks the exact Torch
CUDA distribution before user code. It installs nothing.
Version combinations must exist in the upstream registry. Resolution establishes
artifact identity and platform, not framework ABI compatibility or a speedup.

Each acquired machine retains its selection across on-demand edits and scheduler
restarts. Bootstrap
logs include the selection. `ondemand add --image` and existing digest strings
remain available when resolution has already happened; their platform is unchecked.
Typed Python callers can construct `Image(requested=..., resolved=...)` for these
pre-pinned artifacts. Private provider pulls still require the provider's registry
credential reference; Docker login only authenticates preparation.

The bootstrap overrides vendor entrypoints. Choose an explicit job interpreter to
reuse the image environment; selecting a PyTorch image alone does not stop a job
command from installing Torch again. Cold image pulls can still dominate startup.


### Reuse a preinstalled job runtime

<!-- implementation-reference -->
```python
from etude.jobs import TrainingJob
from etude.spec import Runtime

job = TrainingJob(
    name="base",
    train=train,
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
Without `runtime` or a preparation's interpreter, a typed job runs `uv run python`
in the checkout and a command runs in the shell's environment.

### Prepare a job environment once per machine

<!-- implementation-reference -->
```python
from etude.spec import Preparation, Runtime

job = TrainingJob(
    name="base",
    train=train,
    config=TrainConfig(),
    max_hours=1,
    inputs=(engine_wheel, *weight_shards),
    prepare=Preparation(
        command=(
            'UV_PROJECT_ENVIRONMENT="$ETUDE_PREPARED_DIR/.venv"'
            " uv sync --frozen --no-install-project"
            ' && uv pip install --python "$ETUDE_PREPARED_DIR/.venv/bin/python"'
            ' --no-deps "$ETUDE_PREPARED_INPUTS/engine.whl"'
        ),
        paths=("pyproject.toml", "uv.lock"),
        inputs=("engine.whl",),
        python=".venv/bin/python",
    ),
    runtime=Runtime(packages={"engine": "1.4.0"}),
)
```

After the attempt is claimed, the command runs and writes `$ETUDE_PREPARED_DIR`.
It sees exactly what its key covers: it runs in a copy of the source holding only
`paths`, and finds only the inputs it names, at `$ETUDE_PREPARED_INPUTS/NAME`.
Reading anything else fails the build rather than reusing a stale result, so
declare every file the command reads, such as native source it compiles. The directory is kept under
`ETUDE_HOME/prepared/` with a receipt listing the size and SHA256 of every file
it wrote. A later attempt on the same machine whose key is unchanged (the
content of `paths`, the command, the named inputs' digests, the job environment
and the machine's platform) verifies the directory and reuses it without running
the command. The attempt log says which happened:

```text
prepare: built 3f9c0e21d4b7 (41.2 s, 1203841024 bytes)
prepare: reused 3f9c0e21d4b7 (verified 2.1 s)
prepare: rejected 3f9c0e21d4b7 (.venv/lib/python3.12/site-packages/engine/x.so digest mismatch)
```

A rejected directory is deleted and built again; one left by an interrupted
build has no receipt and is deleted before the next build. A failed command fails
the attempt. `python` names the job's interpreter inside the prepared directory;
it becomes `ETUDE_PYTHON`. A `runtime` given beside it only pins versions, and the
runtime check runs after preparation, so a reused environment is still checked
against its pins. Name the interpreter once: in `runtime.python` for a
preinstalled one, or in `prepare.python`. Least recently used directories are deleted
beyond half the disk available to them; one an attempt is using is kept.
Everything kept ends with a rental.

A new rental builds again. To keep that build short, install a prebuilt native
package as a named input, as above, rather than compiling it, and let the input
arrive through the cache's concurrent download. Carrying uv's cache to the next
rental through storage was measured and dropped: on a RunPod L4 a 7 GB cache of
wheels took 124 s to restore, against 45–150 s for a fresh `uv sync` from PyPI,
and every new lock file uploaded another 7 GB.

The pinned official PyTorch example has a repeatable CPU check:
`uv run python scripts/test_pytorch_image.py`. It pulls the image, then checks
runtime versions, a CPU tensor operation and guardian imports with site packages
disabled inside network-disabled containers. The Prepared image workflow runs it
on Linux; no GPU or provider account is needed. This is not a full bootstrap or
CUDA execution test.


### Setup, training and cleanup

<!-- implementation-reference -->
```python
job = TrainingJob(
    name="base",
    train=train,
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
prepared state. Setup runs again on every attempt, including one that
infrastructure recovery or a planned stop allocates; work to keep and reuse
belongs in `prepare`, or in out. A nonzero setup or training
exit is terminal and is not replayed automatically, except training's planned
stop (exit 75, [The run promise](#the-run-promise)).

When hooks are configured, logs label setup, run and cleanup, including exit or
stop reason. `ETUDE_PHASE` identifies the current phase. Cleanup runs after user
code has begun, including setup failure, cancellation and the attempt time limit.
Its `cleanup_seconds` defaults to `None`; a positive finite value limits this
phase, capped by the remaining session/rental deadline. On owned machines without
a session deadline, unlimited cleanup can remain in progress until it exits. Its `ETUDE_DEADLINE` reflects that cap. Cleanup
ignores the cancellation request so it can collect diagnostics; `ETUDE_STOP_FILE`
remains present. An expired paid window cannot be extended for cleanup. A killed
machine or failed preflight cannot promise cleanup.

Cancellation writes `ETUDE_STOP_FILE`, then requests TERM for the process
group, so a stop gives time to save only to a job that handles TERM
itself. `Job.stop_seconds` optionally
limits the graceful stop before KILL; its default is `None`, still capped by the
phase deadline. At an exhausted phase deadline Etude sends KILL immediately and
reaps the process before closing its output. Cleanup
failure is logged and makes an otherwise successful attempt fail; it never replaces
a setup/training failure, cancellation or time-limit outcome. Cleanup is best-effort
and must not be the only owner of an essential external action.

Storage GETs retry transport failures and HTTP 429/500/502/503/504 at most three
times, with 0.1/0.2-second backoff. Connection and read timeouts follow this same
retry policy; an exhausted storage request raises `StorageUnavailable`, not an
absent-object result. All attempts share the caller's optional deadline,
including backoff. Retry diagnostics include attempt and failure category,
never signed URLs. Without a retained log destination, diagnostics go to stderr
so command results stay readable on stdout. Missing objects and permanent/authentication errors are not
retried. Bootstrap waits for a not-yet-published object, but an exhausted or
permanent read failure ends startup instead of starting another retry loop.
Writes, launch admission, provider creation and user commands gain no
automatic replay from this policy.



### Timeout ownership

<!-- implementation-reference -->
```python
from etude.deadline import limit_time

# An optional total allowance for synchronous remote operations in this block.
with limit_time(900):
    snapshot = scheduler.logs(run="baseline", refresh=True, timeout=None)
```

Startup and log retrieval default to no total timeout. `None` adds no operation deadline; nested
operations still obey an existing caller deadline and rental limits. Positive
values are seconds; CLI `--timeout 0` and `--startup-seconds 0` select no timeout.
A finite allowance is shared across requests and is never reset by a retry.
SSH connection options can be set explicitly in the customer's SSH configuration;
Etude supplies no hidden SSH connection timeout. Provider HTTP uses the enclosing
operation allowance. Storage HTTP also has finite socket limits so a dead
connection cannot hold delivery indefinitely, even without an operation deadline.

Set storage socket limits in the environment of the process doing delivery:

```sh
export ETUDE_STORAGE_TIMEOUT_SECONDS=120
export ETUDE_STORAGE_BYTES_PER_SECOND=524288
```

Both values must be positive and finite. The defaults are 60 seconds and
1,048,576 bytes/second (1 MiB/s). The socket timeout covers connection setup,
waiting for response headers and each response read. For urllib's whole-body
uploads it adds body bytes divided by the configured rate: a 64 MiB part gets
124 seconds with the defaults. Response reads reset the base socket allowance
as bytes arrive, so a progressing large download has no short total limit.
Every socket allowance is capped by an enclosing operation deadline. Set these
variables in the launcher's environment before launching; rentals and SSH
workers receive its explicit settings in their bootstrap environment. Job
variables alone do not configure the parent uploader. Bootstrap log publication
uses the same policy. For SDK clients, see [tracking](tracking.md#storage-outages-and-recovery).

Status's delivery age comes from the latest retained successful write for the
current attempt, or its allocation time before any write is observed. Failed
requests and successful reads do not reset it. The scheduler may simply have
stopped collecting evidence; use the worker log to distinguish that from a
storage outage. Completed and local-only attempts have no delivery-age warning.

Finite HTTP operations also bound waiting for the complete response, even when
a connection keeps streaming bytes. Expiry leaves the in-flight outcome unknown:
Etude does not infer absence or replay writes. The scheduler's persisted startup
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

<!-- implementation-reference -->
```python
from pathlib import Path

from etude.launch.client import fetch, usage

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
```

`fetch(run, name, directory)` and `etude fetch RUN NAME DIRECTORY` resolve
`name` in one place, the session's journal: first among the latest attempt's
recorded outputs (`RUN/attempts/ATTEMPT/NAME`), then among the run's out
members (`RUN/out/NAME`, fingerprinted in `RUN/out.json`). Both download
through the same verified path; an out member costs one membership read and
one object read. An unknown name raises with the names that exist, outputs
then out members, at most 20 followed by `and N more`. The CLI prints
`StorageUnavailable` as one line, here and for `report --source`.

`etude usage --run RUN` reads the same retained evidence. `observed_at` is the
latest included event timestamp, not a claim that remote evidence is current.
The worker's ordinary synchronization/tick retains session events; the read
never refreshes them. With a server selected, the public call still uses
the existing SSH connection to that server; the database read itself contacts
no worker, provider or artifact store. Failed downloads and bootstrap runtime reads remain in
session evidence when publication succeeds. Hard kills, missing journals and
truncated operational logs leave unknown intervals.

Run totals include that run's events. Shared source/runtime traffic appears in
`shared_events`, outside run totals; it has no fabricated per-run allocation.
A session's files fetched for one run count as that run's. After its totals the
command prints one line per purpose, so an input this machine already held reads
apart from one it downloaded:

```text
input: received 0 bytes, reused 19300000000 bytes
```
Use `usage()` or `usage(machine=...)` for totals that include shared and
unattributed launcher traffic once. Deduplicate exports by `Transfer.id` when
combining scopes. Upload counts are body bytes supplied to urllib, including
multipart form framing, not confirmed socket bytes. Response counts are actual
body reads, including partial responses and failures; object length is separate.

Downloads use Etude's one cache, `$ETUDE_HOME/cache` (default `~/.etude/cache`,
`etude.cache.Cache`), keyed by transport SHA256 and length, with a 20 GiB payload
allowance. Completed-output reads, worker source/checkpoint reads and tracking's
bucket reads (`S3Storage`) share it, so bytes one of them read are reused by the
others. The first cold acquisition still transfers every byte. A range of an artifact with a recorded SHA256 and length
is no exception: a whole-file digest cannot authenticate a range, so the whole
artifact is acquired and verified, streamed to disk even beyond the allowance,
before any range is returned, and held bytes are verified again before a range
is read from them. Immutable objects without a recorded digest fetch and keep
individual ranges. For cache-admitted artifacts, verified warm reuse transfers no
payload bytes, including from a new process or to another destination; control
metadata reads still cost requests and bytes. 256 MiB ranges retain
completed chunks across interruption and require exact ranges and a stable
strong ETag. A presigned download reads each range as 16 MiB parts on up to 32
connections and reads the next range ahead: one connection from a RunPod L4 to R2
measured 4–12 MB/s, and this reads 19.3 GB at 48–81 MB/s. Invalid ranges, authentication failures, changed identity or digest
mismatch fail explicitly. Files larger than the allowance, or acquired while
other digests occupy it, stream through destination-side staging. Oversized and destination-staged downloads do not retain a reusable cache copy.
Only verified bytes replace the destination. Downloads add no total timeout by default;
they share an existing caller deadline, and worker inputs remain bounded by the
rental deadline. Each storage request still has connection and read timeouts.
Local digest verification and copying check the total allowance
between chunks and before atomic publication; expiry preserves an existing
destination. Two transport retries, including HTTP 429/500/502/503/504, share
one budget across all ranges and the caller's deadline. Python callers can
set an allowance with `download(Download(...), destination, timeout=..., retries=...)`
from `etude.launch.storage.download`.

A local HTTP fixture completes two synthetic runs on one kept machine. It
installs a fixture runtime, retries a denied source download, shares a 2,097,150
byte source and 1 MiB checkpoint input, publishes two identical 24 MiB
checkpoints, collects worker journals through scheduler ticks, and fetches each
result twice through `Scheduler.fetch`. Worker and launcher caches are separate.

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
78,643,198 response bytes. Control bytes are measured from their
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

Adoption requires the SDK revision containing these APIs on both launcher
and worker; generated archives include the same revision automatically.
Use a fresh `ETUDE_HOME` for offline integration proof and install the reviewed
PR commit with `uv add 'etude-sdk @ git+https://github.com/loopflowstudio/etude@COMMIT'`.

Replaced remote training saves are removed by the scheduler when worker grants
only permit uploads. Keep `etude server run` or `etude wait` running for
prompt cleanup; with neither, they remain until the next command that observes
the worker journal. No hidden background process starts to remove them.
