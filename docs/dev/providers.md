# Provider acquisition

This reference is for maintainers changing provider acquisition; authors start with [the guide](../launcher.md).

## Rented machines

```sh
etude storage add west --bucket BUCKET --prefix etude/launch --provider s3 --region us-west-2
etude provider set runpod --kind runpod --storage west
etude ondemand add l4 --provider runpod --gpu "NVIDIA L4" \
    --vcpus 6 --memory-gb 48 --disk-gb 40 --max 1 --max-usd-per-hour 0.80
etude launch job.py --rent l4
```

For Vast.ai, inject `VAST_API_KEY` in the launcher's environment (RunPod uses
`RUNPOD_API_KEY`). Register an SSH public key in the Vast account before renting.
Use the same experiment file and storage, with an explicit provider:

```sh
etude provider set vast --kind vast --storage west
etude ondemand add rtx4090 --provider vast --gpu "RTX 4090" \
    --vcpus 4 --memory-gb 16 --disk-gb 40 --max 1 --max-usd-per-hour 0.80
etude availability rtx4090 --timeout 30
etude launch job.py --rent rtx4090 --quote
etude launch job.py --rent rtx4090
etude wait
etude fetch RUN checkpoint.pt out
```

The equivalent Python path uses the same scheduler and ordinary runs:

<!-- implementation-reference -->
```python
from decimal import Decimal

from etude.launch.client import default, launch, wait
from etude.spec import Job
from etude.launch.records import MachineTemplate, OnDemand, Provider

launcher = default()
launcher.set_provider(Provider(name="vast", kind="vast", registry="", storage="west"))
launcher.add_ondemand(
    OnDemand(
        name="rtx4090",
        template=MachineTemplate(provider="vast", gpu="RTX 4090", disk_gb=40),
        max_machines=1,
        max_usd_per_hour=Decimal("0.80"),
    )
)
job = Job(command="./train.sh", repo=".", max_hours=0.5, max_usd=Decimal("0.50"))
runs = launch(job.runs(1, 2), rent="rtx4090")
print(wait(*runs))
```

Vast selects verified, on-demand Linux amd64 offers with the requested GPU count
(`--gpu-count`, default 1) and
free upload **and** download. Its quoted hourly rate includes the requested disk
(default 40 GB); offers with metered transfer are excluded because an arbitrary
job's traffic cannot be bounded from its runtime alone. `--data-center US` means
a country code for Vast; RunPod uses its data center IDs. Public images are
supported; a custom image must contain Python 3.12. There is no cross-provider
fallback. No compatible offer leaves queued work waiting.

The scheduler retains the selected offer and instance receipt in the session
folder. Creation is sent once; uncertain replies reconcile by the session label
across inventory pages. Destruction requires that retained ownership and matching
provider identity. Vast's daily aggregates can change and supply no settled-through watermark.
They never become a `Bill`; rental cost remains estimated until authoritative
coverage is available. No unused charge-reporting endpoint is retained beside
that acquisition and billing path.

Vast's `CONTAINER_API_KEY` and `CONTAINER_ID` start a standard-library guardian
before any client download or install. The account key is never sent to the
instance. Completion, cancellation and bootstrap failure delete the instance;
caller loss leaves the guardian active until the rental deadline. Stopping an
instance alone does not stop storage billing. A host that never boots or a provider
API outage can delay deletion: the rental quote is not a provider-enforced total
cost guarantee and excludes charges from external storage services. Keep the
scheduler's loop running to reconcile uncertain creation and deletion. Inspect SSH via
Vast's instance **Connect** tab.

### Lium

```sh
# Inject LIUM_API_KEY into the launcher's environment through your secret store.
export LIUM_SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)"
etude provider set lium --kind lium --storage west
etude ondemand add rtx5090 --provider lium --gpu "RTX 5090" --gpu-count 1 \
    --vcpus 4 --memory-gb 16 --disk-gb 40 --max 1 --max-usd-per-hour 0.80
etude availability rtx5090 --timeout 30
etude launch job.py --rent rtx5090 --quote
etude launch job.py --rent rtx5090
etude wait
etude fetch RUN checkpoint.pt out
```

Use a funded Lium account with `read` and `rent` permissions. Keep the private SSH
key in the launcher's OpenSSH agent/configuration, and configure host trust
there. Etude keeps normal host-key checking enabled. Configuration and retained
status reads work without keys or network; availability and rental operations
need the launcher's credentials.

<!-- implementation-reference -->
```python
from decimal import Decimal

from etude.launch.client import default
from etude.launch.records import MachineTemplate, OnDemand, Provider

launcher = default()
launcher.set_provider(Provider(name="lium", kind="lium", registry="", storage="west"))
launcher.add_ondemand(
    OnDemand(
        name="rtx5090",
        template=MachineTemplate(provider="lium", gpu="RTX 5090", gpu_count=1),
        max_machines=1,
        max_usd_per_hour=Decimal("0.80"),
    )
)
```

Lium selects **secure** nodes only, respects the node's minimum rental count,
and quotes CPU, RAM and disk allocated to that GPU count. Public feed resources
describe the whole node; Etude uses the [allocation quote](https://lium.io/api/openapi.json)
instead. `--data-center US` selects a country code. Missing allocation facts are
an unavailable observation, not a report of empty stock. Backups are explicitly
disabled and no separate volume is created, so there is no ongoing Lium storage
or volume-egress charge in this path. Journal bucket charges remain separate.

Hourly admission includes Lium's one-cent-per-GPU price-change tolerance. Rental
budgets reserve whole hours for the initial `termination_hours` timer, even for
a shorter job. This is a reserve, not Lium's billing minimum: billing is per
second. A five-minute rental at a quoted $0.76/hour requires a $0.76 reservation.
The scheduler rechecks stock, allocation and price before sending one create;
lost replies reconcile by session name without creating another pod.

Etude supplies its public pinned image through a Dockerfile, then bootstraps over
SSH after RUNNING and acknowledged exact scheduled removal. Custom public images
need Python 3.12 and OpenSSH support. Signed bootstrap links travel over SSH;
`LIUM_API_KEY` never goes to the pod. The client and job keep separate environments.

Lium shutdown is **best effort**. Creation includes a whole-hour timer from boot;
the scheduler immediately schedules the absolute rental deadline and verifies
readback. A local standard-library watchdog starts before client installation and
stops bootstrap/work at the deadline. Unlike RunPod/Vast's self-scoped guardians,
it cannot delete the pod or end billing: Lium exposes no documented pod-scoped
self-deletion key. Completion, failure and cancellation rely on the scheduler's deletion;
without its loop, on the provider timer. Boot delays, the gap before exact
scheduling, and undocumented removal processing delays can exceed the reservation.
Keep the scheduler's loop running for reconciliation and cleanup retries. Optional
per-key spending caps are operator-managed additional protection; Etude never
changes account settings.

Deletion is complete only after confirmed absence. The provider's ledger statement
survives pod deletion; Etude reports actual costs only through its validated
`billed_until` watermark. A settled final removal also establishes zero compute
charges afterward. Missing or contradictory accounting stays estimated.
Offline fixtures verify this integration; no paid Lium lifecycle is claimed here.

A template without `--gpu` rents RunPod CPU pods: the cheapest secure-cloud CPU
flavor with at least `--vcpus` (rounded up to a power of two, RunPod's sizes)
and `--memory-gb`, priced per vCPU and capped like a GPU pod. CPU pods boot the
same default image. Their disk defaults to 40 GB, or 10 GB per vCPU when smaller.
On a CPU pod `os.cpu_count()` reports the host's CPUs; size worker pools from
`len(os.sched_getaffinity(0))`.

```sh
etude ondemand add cpu --provider runpod --vcpus 8 --memory-gb 16 --max 2 --max-usd-per-hour 0.40
etude availability cpu
etude launch job.py --rent cpu --max-usd 1 --quote
etude launch job.py --rent cpu --max-usd 1     # rents up to the on-demand's 2 machines

```

`availability` asks the provider, so it is a wait: it prints what would be
rented, the price and ceiling, how many of the on-demand's machines are in use
and when the provider answered; `--timeout` bounds it. `rentable now` is the
provider's stock signal, not a reservation: the create can still be refused,
and is then
[a refusal](launcher.md#ownership-and-failure-behavior) carrying the provider's reason. `--quote` places the
first wave as launch would, asking the provider for prices, and prints one line
per run: an owned or already rented machine it would use, or the hardware it
would rent and that window's planned rental cost. It then prints the planned
first-wave rental cost and the jobs' rental budget for the whole launch.
These amounts assume timely deletion at the quoted rate, not a guaranteed
total bill; provider delays can exceed the budget and external storage is excluded. It
records nothing and rents nothing; without capacity it fails as launch would.

Free existing machines are used before renting another. With `--pool` and
`--rent` together, runs spill onto rented machines when owned ones are busy.
Change automatic renting with `etude ondemand max l4 2`; `etude ondemand max l4 0`
allows only machines rented by hand.
A zero limit still permits an explicit `rent`; a positive limit bounds
concurrent rented windows. Lowering the limit leaves current windows intact.
The limit and ceiling belong to the on-demand, so every launch that names it shares them.
All three providers default to the pinned stock image
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
use separate environments. RunPod/Vast start commands and Lium's SSH start carry
a small standard-library bootstrap; no Etude files need to be present in the image.

An on-demand's `--image` or `etude ondemand image` selects a user's own pinned image for
training dependencies installed ahead of time. It must meet the same Python
and uv requirements and needs nothing of Etude. For RunPod, `--registry` supplies a
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
etude rent l4 --hours 0.5 --max-usd 0.40
etude launch job.py --rent l4
etude machines
etude terminate MACHINE
```

`etude machines` is the one list. Each machine shows its state: `starting`,
`available`, `unavailable` (refused, expired, or its pool off) or `deleted`.
Each owned machine shows its pool, its
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
scheduler delivers each next run; keep its loop running to drain the queue.
A machine rented for queued work ends when no waiting run fits any free slot; a
manually rented one waits until its deadline or termination. Terminate refuses
while it still has unfinished work. Ending a machine rented for queued work is asked of it
through its inbox: its worker delivers its last files, the tail of its log and its end
receipt, then deletes the machine, and the launcher confirms the deletion once the receipt
arrives, or at the rental's deadline. `etude wait` returns after that confirmation.

The launcher needs `RUNPOD_API_KEY`, `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` from the configured secret manager. No secrets enter
SQLite. An on-demand takes its provider's storage when added and keeps it. A storage
cannot change; add another name. `--data-center` selects a RunPod data center;
omitting it lets RunPod place the machine. `ondemand image` affects future machines.

### Several runs on one machine

```sh
etude ondemand add l4x2 --provider runpod --gpu "NVIDIA L4" --vcpus 6 --memory-gb 48 \
    --max 1 --max-usd-per-hour 0.80 --slots 2
etude launch sweep.py --rent l4x2 --quote                    # sweep/base@1  rent ... , 2 slots with sweep/base@2  ...
etude status                                               # running, steps 1200, shared by 2 runs, ~$0.03
```

`--slots N` on `ondemand add` or `pool add` lets each of its machines run up to N
runs at once (default 1: one after another). The runs share the machine's GPUs
and CPUs whole; nothing is partitioned, so pick N for what the job leaves idle.
Each run keeps its own attempt folder, outputs, log, record and delivery, and
cancelling or failing one leaves the others running.

A new machine is given up to N waiting runs at once, and takes more as slots
free while its window has time for them. Its window covers the longest run it
was given, or its share of the hours of the runs waiting on its on-demand when that is
longer (their total, divided among the machines the on-demand may still rent
and their slots), within the runs' dollars. A launch fills a machine's slots
before taking another; the on-demand's `--max` counts machines.

Cost is split by placed time: each moment of a rental is shared equally among
the runs on it then, and run budgets and an experiment's `--max-usd` charge that share.
Admission and a run's own time limit still price it as if it had the machine
alone, so a run never outspends its `max_usd`. `status` says `shared by K runs`
when K ran together, and `--quote` lists each run's machine, its slots and the
runs placed with it, splitting a new rental's planned cost among them.

A pass's eval jobs are one run and take one slot, whatever their number. A
free slot on a machine training has rented is room for them, so with
`--slots 2` evals start beside training without another rental. Each eval
run using shared GPU inference starts its own agent process: two eval runs
sharing a machine do not share one. CPU game processes cache forwards directly.
