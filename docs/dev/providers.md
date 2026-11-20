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

Vast selects verified Linux amd64 offers with the requested GPU count
(`--gpu-count`, default 1) and
free upload **and** download: Vast's on-demand offers, or its interruptible
ones for a [`--spot`](#spot) on-demand. Its quoted hourly rate includes the requested disk
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

Lium selects **secure** nodes (**spot** nodes for a [`--spot`](#spot)
on-demand, never a mix), respects the node's minimum rental count,
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
The scheduler rechecks stock, allocation and price before sending one create.

Etude supplies its public pinned image through a Dockerfile, then bootstraps over
SSH after RUNNING and acknowledged exact scheduled removal. Custom public images
need Python 3.12 and OpenSSH support. Signed bootstrap links travel over SSH;
`LIUM_API_KEY` never goes to the pod. The client and job keep separate environments.

Lium shutdown is **best effort**. Creation includes a whole-hour timer from boot;
the scheduler immediately schedules the absolute rental deadline and verifies
readback. A local standard-library watchdog starts before client installation and
stops bootstrap/work at the deadline. Unlike the machine timers of RunPod, Vast,
EC2 and Lambda, it cannot delete the pod or end billing: Lium exposes no documented pod-scoped
self-deletion key. Completion, failure and cancellation rely on the scheduler's deletion;
without its loop, on the provider timer. Boot delays, the gap before exact
scheduling, and undocumented removal processing delays can exceed the reservation.
Keep the scheduler's loop running for reconciliation and cleanup retries. Optional
per-key spending caps are operator-managed additional protection; Etude never
changes account settings.

Deletion is complete only after confirmed absence. The provider's ledger statement
survives pod deletion; Etude reports actual costs only through its validated
`billed_until` watermark. A settled final removal also establishes zero compute
charges afterward. Missing or contradictory accounting stays estimated. A pod
removed before any charge has no statement, so its cost stays an estimate.
Lium sends timestamps without a zone; Etude reads them as UTC.
A live check on 2026-11-17 completed start → GPU job → explicit terminate →
confirmed absent on an RTX 4090. The job's retained `etude logs` output showed
`nvidia-smi`, driver 595.84, CUDA 13.2 and 24,564 MiB GPU memory. The machine
listed as `deleted` at about $0.10 (part billed, part estimated), and the final
account inventory was empty. This node needed about fourteen minutes to reach
RUNNING; the check used a twenty-minute startup allowance and configured SSH
host trust before bootstrap. An earlier node stayed `PENDING` without an SSH
address for ten minutes and was deleted at its startup deadline, at about
$0.07 estimated cost.

#### A pod Etude made is always deletable

A pod whose description Etude cannot read is still a machine it holds, and
`terminate` deletes it:

```text
$ etude machines
rtx4090-7b0c01e641d4  rtx4090  starting, rented until 2026-11-17 17:01 UTC, ~$0.01
$ etude terminate rtx4090-7b0c01e641d4
$ etude machines --all
rtx4090-7b0c01e641d4  rtx4090  deleted, rented, ended 2026-11-17 16:04 UTC, ~$0.01
```

Left alone, the same machine is deleted by the scheduler's loop at its startup
deadline: it cannot bootstrap without an address, and its runs end `lost` and
retry elsewhere. Until then each pass retains a `machine readiness` diagnostic
naming the session, `Lium reply is missing valid required fields`.

Etude keeps two things about a pod, and only the first is needed to own it:

| | Read from | Needed for |
| --- | --- | --- |
| Identity: the pod id | the create reply, else the pod named for the session in `GET pods` | recording the machine, `terminate`, deletion at a deadline |
| Facts: address, price, GPU count, node, creation time | `GET pods/{id}` | bootstrap, verifying what was rented, the bill |

The id is written to the session's `receipt.json` as soon as it is known,
before any facts are read; the facts join it when they first parse. Deleting a
pod sends one `DELETE` and reads its status until Lium answers that it is
absent. Neither step reads the pod's facts, and with a kept id neither lists
the account's pods. A pod on the wrong node, GPU count or price is deleted
without starting work.

A create whose reply was lost is looked up by session name and never sent
again. While no pod of that name exists, nothing is assumed, and `terminate`
says when the question closes:

```text
$ etude terminate rtx4090-7b0c01e641d4
etude: Lium create has no pod yet; settles as nothing rented after 2026-11-17 18:01 UTC
```

That time is the rental's deadline plus the whole hour of Lium's own removal
timer: past it, Lium has removed anything the create could have made. The next
pass or `terminate` then records that nothing was rented, the rental stops
accruing, and `etude machines --all` shows
`unavailable, nothing rented 2026-11-17 18:01 UTC`. A pod that does appear by
name before then is recorded and deleted like any other.

### AWS EC2

```text
$ etude storage add west --bucket BUCKET --prefix etude/launch --provider s3 --region us-west-2
$ etude provider set aws --kind ec2 --storage west
$ etude ondemand add l4 --provider aws --gpu L4 --max 1 --max-usd-per-hour 0.90
$ etude availability l4 --timeout 60
l4  g6.xlarge: L4 with at least 4 vCPUs, 16 GB at $0.8226/hour (ceiling $0.90)  available  0 of 1 in use  rentable now  asked 2026-11-17 19:02:11 UTC
$ etude launch job.py --rent l4 --max-usd 2
$ etude wait
$ etude machines --all
l4-41840a01c3b1  l4  deleted, g6.xlarge: L4 with at least 4 vCPUs, 16 GB, rented, ended 2026-11-17 19:31 UTC, ~$0.31
```

EC2 uses the keys S3 storage already reads: `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and, for a temporary role, `AWS_SESSION_TOKEN`. One
identity then needs the bucket and these actions:

| Action | Used for |
| --- | --- |
| `ec2:DescribeInstanceTypes`, `pricing:GetProducts`, `ec2:DescribeSpotPriceHistory` | what would be rented, and its price |
| `servicequotas:GetServiceQuota` | whether the account may run it |
| `ssm:GetParameter` on `/aws/service/deeplearning/ami/*` | the host image |
| `ec2:DescribeInstanceTypeOfferings` | types offered in a requested zone |
| `ec2:RunInstances`, `ec2:CreateTags` | renting |
| `ec2:DescribeInstances`, `ec2:DescribeVolumes`, `ec2:TerminateInstances` | finding and deleting what was rented |

EC2 uses the service-linked role `AWSServiceRoleForEC2Spot`. If the role
is absent, the first spot request needs permission to create it. The bootstrap links are signed with the launcher's keys, so
keys from a temporary role must outlive the rental: an SSO session that expires
mid-rental breaks the machine's access to storage, though not its deletion.

`--gpu` is AWS's GPU name (`L4`, `A10G`, `T4`, `L40S`). You do not name an
instance type: Etude rents the cheapest x86_64 type with exactly `--gpu-count`
of that GPU and at least `--vcpus` and `--memory-gb`, and `availability` shows
which. `--data-center` is a region (`us-west-2`) or one zone (`us-west-2b`);
omitted, it is the region of the on-demand's S3 storage, where transfer to the
bucket is free. An on-demand on R2 storage must name it. Only GPU templates
rent from EC2.

The quote is read from AWS, never from a table in Etude: the instance, its
disk (75 GB for the host image plus `--disk-gb`, default 40) and its public
address. It excludes transfer to a bucket outside the region. EC2 bills arrive
a day late, so `machines` shows the estimate at the quote (`~`).

`not rentable now` carries AWS's reason. New accounts often have a zero quota:

```text
$ etude availability l4-spot
l4-spot  none available: AWS quota 'All G and VT Spot Instance Requests' is 0 vCPUs; g6.xlarge needs 4  0 of 1 in use  not rentable now  asked 2026-11-17 19:02:14 UTC
```

Raise it in the AWS console under Service Quotas; the standard-price quota is
`Running On-Demand G and VT instances`.

The instance boots AWS's GPU base image (NVIDIA driver and Docker) in the
default VPC using its existing default security group. Etude supplies no key
pair or instance role and opens no ports. The host runs
the on-demand's image as a container with every GPU. `--image` therefore means
what it means on the other providers. There is no SSH access; read `etude logs`.

**Deletion.** The instance is made so that powering off terminates it and
deletes its disk. The launcher confirms termination and the removal of tagged
volumes before reporting deletion. If the launch receipt is more than five
minutes old, empty session-tag listings for both instances and volumes confirm
deletion. A fresh or missing receipt leaves an empty listing unconfirmed before
the rental deadline. When the launcher notices a spot reclaim late, its cost
estimate uses the quoted rate through the time it noticed the loss.

Before downloading the signed, digest-checked
session boot script, the host schedules that
power-off for the rental's deadline; it also powers off when the worker exits
for any reason. No credential that can delete anything is on the machine, and
neither path needs the launcher. The scheduler's loop terminates by the
`etude-session` tag as the third path, and `RunInstances` carries the session
id (with the zone, for a request that names one) as its idempotency token, so a
lost reply never rents a second instance.
To check an account by hand:

```sh
aws ec2 describe-instances --filters Name=tag-key,Values=etude-session \
    Name=instance-state-name,Values=pending,running,stopping,stopped
```

### Lambda Cloud

```text
$ etude provider set lambda --kind lambda --storage west
$ etude ondemand add a10 --provider lambda --gpu "A10 (24 GB PCIe)" --max 1 --max-usd-per-hour 1.50
$ etude availability a10 --timeout 60
a10  gpu_1x_a10: A10 (24 GB PCIe) with at least 30 vCPUs, 200 GB at $1.29/hour (ceiling $1.50)  available  0 of 1 in use  rentable now  asked 2026-11-17 19:10:52 UTC
$ etude launch job.py --rent a10 --max-usd 2
```

The launcher needs two keys, and refuses to ask or rent while either is unset:

| Variable | What it is | Where it goes |
| --- | --- | --- |
| `LAMBDA_API_KEY` | your account key | the launcher only |
| `LAMBDA_TERMINATE_KEY` | a key that can read and terminate instances, and launch nothing | every rented machine |

Lambda has no machine lifetime setting, so each machine terminates itself
through Lambda's API, and needs a key to do it. Make that key once. These
calls change your Lambda account; Etude never does.

```sh
api=https://cloud.lambda.com/api/v1
auth="Authorization: Bearer $LAMBDA_API_KEY"
json="Content-Type: application/json"

curl -s -H "$auth" $api/roles        # note the ids of instance-terminate and instance-reader
curl -s -H "$auth" $api/workspaces   # note your workspace's id

curl -s -H "$auth" -H "$json" $api/identities \
    -d '{"identity_type": "service", "display_name": "etude-guardian"}'   # note its id
curl -fsS -H "$auth" -H "$json" $api/workspaces/WORKSPACE_ID/memberships \
    -d '{"member_id":"IDENTITY_ID","role_ids":["TERMINATE_ROLE_ID","READER_ROLE_ID"]}'
# Pipe the one-time secret directly into Doppler.
set -o pipefail
curl -fsS -H "$auth" -H "$json" $api/identities/IDENTITY_ID/api-keys \
    -d '{"name":"etude-guardian"}' | jq -er .data.api_key | \
    doppler secrets set LAMBDA_TERMINATE_KEY --silent
```

Supply both role IDs when creating the membership. Omitting them assigns
Workspace Member, which can launch machines. If the key expires, its expiry
must follow the rental deadline.

That key reaches the machine, where the job is your own code. It can read and
terminate every instance in that workspace, so an account shared with other
work should give Etude a workspace of its own.

`--gpu` is Lambda's description of the GPU, quoted, as its instance types list
it. A name Lambda does not use is answered with the ones it does:

```text
$ etude availability a10
a10  none available: Lambda has no GPU named 'A10'; it names: A10 (24 GB PCIe), A100 (40 GB SXM4), H100 (80 GB SXM5), ...  0 of 1 in use  not rentable now  asked 2026-11-17 19:10:40 UTC
```

Etude rents the cheapest instance type with that GPU, `--gpu-count` of them
and at least `--vcpus` and `--memory-gb`. `--data-center` is a Lambda region
(`us-west-1`); omitted, any region that reports capacity. Lambda's price is per
instance type and includes its disk, so `--disk-gb` is not applied. Lambda rents at one
price: a `--spot` on-demand is never rentable. Only GPU templates rent from Lambda.

The instance boots Lambda Stack and runs the on-demand's image as a container
with every GPU. The launch names one SSH key because Lambda requires it:
`LAMBDA_SSH_KEY_NAME`, or the account's first key by name. Add one to the
account before renting; `ssh ubuntu@IP` then reaches the host.

On the A10 proof host, `nvidia-smi` reported driver 570.148.08. A job using
PyTorch with CUDA 13.2 needed NVIDIA's `cuda-compat-13-2` libraries on its
`LD_LIBRARY_PATH`. Choose the compatibility package for the job's CUDA
runtime and the host driver. [NVIDIA compatibility matrix](https://docs.nvidia.com/deploy/cuda-compatibility/latest/forward-compatibility.html)

**Deletion.** Before downloading the container, the host arms a systemd timer
for the rental deadline. The timer reads the host's cloud-init instance UUID,
removes its hyphens to obtain the Lambda API ID, and calls the terminate
endpoint with `LAMBDA_TERMINATE_KEY`. It makes the same call when
the worker exits, retrying until Lambda accepts it. The scheduler's loop
terminates by the instance ID, recovering it by name (`etude-SESSION`) if
the launch reply was lost. A
terminate key that is wrong or expired leaves only that third path. Lambda's
launch call has no idempotency token: a lost reply is looked up by name and
never sent twice. Lambda reports no per-machine bill, so cost stays the
estimate at the listed price.

### Spot

```sh
etude ondemand add l4-spot --provider aws --gpu L4 --spot --max 1 --max-usd-per-hour 0.70
etude ondemand add rtx4090-spot --provider vast --gpu "RTX 4090" --spot --max 1 --max-usd-per-hour 0.30
etude availability l4-spot       # ... at $0.6510/hour, spot (ceiling $0.70) ...
etude launch job.py --rent l4-spot
```

`--spot` (`MachineTemplate(spot=True)`) is a fact of the on-demand; listings
mark it `spot`. To offer both prices, add two on-demands. Without it an
on-demand rents at the provider's standard price.

| Provider | A `--spot` on-demand rents | Quote, the most it can cost per hour | When the machine is taken back |
| --- | --- | --- | --- |
| EC2 | a one-time spot instance | the cheapest permitted zone's spot price plus 10%, never above the standard price; plus disk and address | terminated by AWS |
| Vast | an interruptible instance at the offer's minimum bid | that bid plus storage | paused by Vast, still billing storage; Etude destroys it |
| Lium | a `spot` node | its listed price plus Lium's tolerance | removed by Lium |
| RunPod | nothing: `Etude rents no spot machines from RunPod` | | |
| Lambda | nothing: `Etude rents no spot machines from Lambda` | | |

On EC2 the compute portion of the quote is sent as the instance's maximum
price. When a zone refuses for capacity, Etude asks the next zone whose current
spot price that maximum covers, cheapest first, before reporting the refusal. If the spot market rises past it, AWS takes the machine back. The 10% is
headroom for that: over a week of us-west-2 L4 prices the spot price moved
about four times a day, within that range. The on-demand's `--max-usd-per-hour`
applies to the quote as always. Spot has its own AWS quota (`All G and VT Spot
Instance Requests`), and thin spot stock is refused at create like any other
[capacity refusal](launcher.md#ownership-and-failure-behavior).

A spot machine lost under a running attempt is retried on a new machine from
the latest delivered save, as any lost machine is, and
[does not spend `max_attempts`](launcher.md#the-run-promise). EC2 gives two
minutes' notice before taking a machine back; Etude does not act on it. There
is no fallback from spot to the standard price.

### CPU machines

A template without `--gpu` rents RunPod CPU pods: the cheapest secure-cloud CPU
flavor with at least `--vcpus` (rounded up to a power of two, RunPod's sizes)
and `--memory-gb`, priced per vCPU and capped like a GPU pod. CPU pods boot the
same default image. Their disk defaults to 40 GB, or 10 GB per vCPU when smaller.
On a CPU pod `os.cpu_count()` reports the host's CPUs; size worker pools from
`len(os.sched_getaffinity(0))`.

### Availability, quotes and limits

```sh
etude ondemand add cpu --provider runpod --vcpus 8 --memory-gb 16 --max 2 --max-usd-per-hour 0.40
etude availability cpu
etude launch job.py --rent cpu --max-usd 1 --quote
etude launch job.py --rent cpu --max-usd 1     # rents up to the on-demand's 2 machines

```

`availability` asks the provider, so it is a wait: it prints what would be
rented, the price and ceiling, how many of the on-demand's machines are in use
and when the provider answered; `--timeout` bounds it. When the provider has nothing to rent, the line
reads `none available: REASON` in the provider's terms (stock, quota, no spot
machines, or a GPU name it does not use) and names no price. `rentable now` is the
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
Every provider defaults to the pinned stock image
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
use separate environments. RunPod/Vast start commands, Lium's SSH start and the
EC2/Lambda host script carry or fetch a small standard-library bootstrap; no
Etude files need to be present in the image.

An on-demand's `--image` or `etude ondemand image` selects a user's own pinned image for
training dependencies installed ahead of time. It must meet the same Python
and uv requirements and needs nothing of Etude. For RunPod, `--registry` supplies a
provider pull credential for a user's private image. The stock image needs no
registry credential.

To inspect a new RunPod rental over SSH, add your public key (for example, the contents
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

`etude machines` is the one list of machines held now. Each shows its state:
`starting`, `available`, `unavailable` (expired or its pool off) or `deleted`.
Each owned machine shows its pool, its
committed cost and its expiry (or that it never expires). Each rented machine
shows its on-demand, the hardware the provider was asked for (a GPU type with the minimum vCPUs
and memory its host must have, or a CPU flavor and its exact vCPUs), the end of its window and its cost; `etude status` names it beside the
run's machine. `etude machines --all` adds rented
machines whose window has ended, so late bills stay visible, and machines that were
asked for and never rented (`unavailable, nothing rented DATE TIME UTC`). `terminate` takes
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

The launcher needs its provider's keys and the storage's `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` from the configured secret manager. No secrets enter
SQLite. An on-demand takes its provider's storage when added and keeps it. A storage
cannot change; add another name. `ondemand image` affects future machines.

`--data-center` is the provider's own name for a place:

| Provider | `--data-center` | Omitted |
| --- | --- | --- |
| RunPod | a data center id | RunPod places the machine |
| Vast, Lium | a country code (`US`) | anywhere |
| EC2 | a region or zone (`us-west-2`, `us-west-2b`) | the S3 storage's region |
| Lambda | a region (`us-west-1`) | any region with capacity |

### Several runs on one machine

```sh
etude ondemand add l4x3 --provider runpod --gpu "NVIDIA L4" --vcpus 6 --memory-gb 48 \
    --max 1 --max-usd-per-hour 0.80 --slots 3
etude launch sweep.py --rent l4x3 --quote                    # sweep/base@0  rent ... , 3 slots with sweep/base@1, sweep/base@2  planned rental cost $0.30
etude status                                                 # running, steps 1200, shared by 3 runs, ~$0.09
```

Each machine takes up to `--slots` runs concurrently (default 1). The runs
share its CPUs and GPUs. Each has its own attempt folder, outputs, log and
delivery. Etude fills free slots before renting another machine. `--max`
counts machines.

**Admission.** A new rental covers `setup_seconds` plus the longest placed
attempt. Providers such as Lium round the bill to whole hours. Etude spreads
that bill over the base window and splits each interval among the runs holding
a place. Each run must afford its share. If one falls short, Etude replaces
the run with the largest shortfall with the next waiting run and prices the
group again. If no group fits, launch reports `job dollars do not cover the
hours` and rents nothing. After placement, waiting runs can fund extra time
on the rental. The quote includes that extension in its first-wave total.

**Accounting.** A place starts at placement or machine claim, whichever is
later. It lasts through the attempt's time limit while any attempt remains
active, bounded by session end and rental deadline. When the machine empties,
all places end. Each occupied interval contributes its cost once, split among
the distinct runs holding places. A retry on the same machine counts once.
A resume on another machine must still fund its projected share on the first.

A run that finishes early keeps paying for its place while peers continue.
Its peers can use their admitted hours if setup stays within the allowance and
the provider bills at the quoted rate. Run and experiment budgets use these
shares. The `shared by K runs` status counts peak concurrent active runs.
Queued attempts placed by `tick` still size their time limits at the machine's
full hourly rate and can end `over_budget`. See the [three-seed quote
example](../launcher.md#sharing-a-machine).

A pass's eval jobs are one run and take one slot, whatever their number. A
free slot on a machine training has rented is room for them, so with
`--slots 2` evals start beside training without another rental. Each eval
run using shared GPU inference starts its own agent process: two eval runs
sharing a machine do not share one. CPU game processes cache forwards directly.
