---
requires: a change or behavior to assess
produces: a change that works for the people who will use it, with what you saw and what is still wrong
action_style: procedural
---
Use the change the way the people it is for will use it, and fix what breaks.

You are its first user. Nobody should open a demo or merge a PR and find it broken
on the first click; if they would, this step failed.

## Use the real thing

Run the real product, not its tests: the built app, the installed CLI, the live
page in a browser, the API over the network, the simulator or device. Drive it
with computer use, a browser, a terminal or whatever reaches it the way a person
would. Internal setters, test-only endpoints, mocks and fixtures do not count.

Getting there is your job. Build it, start its services, create accounts and
realistic data, sign in, set up whatever state a real user would already have.
When no route exists, make one. Hand back only what truly needs a person, such
as their own credentials, a physical device, a payment or a production approval,
and say exactly what that left unchecked.

Make the scenario as real as you can reach:

- Arrive the way a person arrives: a fresh install or first visit, then a
  returning user with existing data from before this change.
- Do the main job end to end, then the things people actually do: wrong input,
  empty and large data, going back, interrupting and resuming, a slow or failed
  network, two things at once.
- Check what it leaves behind as well as what it shows: files, rows, messages,
  processes, and what another part of the product now reads.
- Look at it. Read the output, check the screenshot, notice what is confusing,
  slow or ugly, not only what is wrong.

## Fix and repeat

Repair what breaks within the change's scope, then go through the experience
again from the start. Keep going until it works for the people it is for. A
change to what the product should be is not yours to make; describe it
instead. When asked for a read-only review, report instead of
fixing.

Run tests only where they tell you something the real path cannot reach, and
keep any you add about what a person would notice.

## Keep what you learned

How to launch and drive this product belongs in this skill. When you had to work
out a step it does not describe, or a step it describes no longer works, write
the working version here with `lf skill customize qa`, replacing what was wrong.

Show what you did in `scratch/qa.html`: each scenario, what you did, what you saw
with screenshots or transcripts, and what you fixed. Finish with what works now,
what you could not reach and why, and what is still wrong, worst first. Do not
publish, land or choose the next Graph step from here.

## Etude: a local prototype

Use `docs/getting-started.md`, `docs/training.md` and `docs/evaluation.md` as
current author instructions. Copy `examples/prototype.py` into a fresh git/uv
lab outside this checkout. It trains a tiny coin model without a model library.
The `eval` extra belongs to the dependency, `etude-sdk[eval]`.

```sh
REPO="$PWD"
LAB=$(mktemp -d /tmp/etude-qa-XXXXXX)
cp examples/prototype.py "$LAB/experiment.py"
cd "$LAB"
cat > pyproject.toml <<EOF
[project]
name = "etude-qa"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = ["etude-sdk[eval]"]
[tool.uv]
package = false
[tool.uv.sources]
etude-sdk = { path = "$REPO", editable = true }
EOF
printf '.venv/\n__pycache__/\nhome/\n*.log\n' > .gitignore
uv python pin 3.12
uv lock
git init -q
git add .
git -c user.name=QA -c user.email=qa@example.test commit -qm prototype
export ETUDE_HOME="$LAB/home"
uv run etude train experiment.py
uv run etude eval experiment.py
uv run etude eval experiment.py
uv run etude report experiment.py
uv run etude smoke experiment.py
```

Require one finished training run (`experiment/coin@0`), eight played matches,
no replay on the second eval, a report under `home/track/reports/`, and smoke
success. Inspect the report in a browser; its charts render in JavaScript.
For the live view run `uv run etude view . --port PORT` in the lab (record its
PID; stop that exact process before finishing). `train`, `launch` and `status RUN`
print `view: …/runs/ADDRESS` only while it is up. A paced plain `train(config)`
(sleep 0.2 s per update, 300 updates) gives time to watch a run page refresh in
place; Playwright `page.wait_for_timeout` and rereading `body` shows the clock rise.
Count each tab's full reloads with `page.on("load", …)`: a run tab reloads when a
block seals and stops once the final block lands; `/`, experiment and Machines tabs
update in place, so one load each while new work appears. A finished plain run trains
again only with `etude train FILE --resume --until updates=N` (plain re-train refuses
`run_name_taken`). Launching in a fresh lab needs `etude database upgrade --development` first.
`launch --pool local` starts the job itself; open the tabs before `launch`, not just
before `wait`, to see the first updates. The launcher keeps `setting_up` until a loop runs.
To see "no end recorded" without waiting 10 minutes, SIGKILL the train's process
group and `touch -t` its run folder's files into the past. The QA home sits inside
the lab, so the view must not count `home/sessions/…/src` copies as experiments.
A second view home for the same lab (e.g. a file-only `home2` with `--source`) must sit
outside the lab: the view skips only its own home, so the first home's retained
sources read as `experiment_named_twice`.
When neither browser extension is connected, drive headless system Chrome:
`uv run --python 3.12 --with playwright --no-project python driver.py` with
`p.chromium.launch(channel="chrome")`; no browser download is needed. For older run
files, `cp -cR ~/.etude/track` into a private folder and pass it as `--source` with a
private `ETUDE_HOME`.
The view's index (`home/views/*.sqlite`) is keyed by the `etude-sdk` version, not
the code: after fixing how charts are projected, stop the view, remove
`home/views` and restart it, or unchanged runs keep their old projection.
A view page that differs from the run's Charts tab is a bug worth checking with the
prototype, whose `report()` names no training panel.
A `nohup uv run etude view … &` ignores SIGINT; stop the view by sending SIGTERM to its
`.venv/bin/etude` child (`pgrep -P UV_PID`), or start `.venv/bin/etude view` from a
Python driver and send SIGINT, which is what Ctrl-C does. A launched run's console reaches
its page only when the loop collects it, about every 30 s, so a page shows a stale tail
between collections; check its "Last collected" age rather than expecting each refresh to move.
`etude launch` refuses a plain `train(config)` file; declare `training_jobs()` for a
launched run. Its `max_usd` must be above zero even on the free local pool. `etude continue --run RUN --until updates=N` writes a child run named by
a digest (`continues.parent` in its `run.json`); require each row to carry its own
launcher facts. A launched evaluation groups every tournament with new matches into one
`EXPERIMENT/evals-DIGEST`: add a second `Benchmark` to `eval_jobs` and raise `games` on
both to get a two-member group, then count rows per tournament in `statsdb`. To see a broken `report()`, write a syntax error into the file while a run
writes and restore it after. `etude server set` refuses an unreachable host, so reach a
failed launcher read with `chmod 0 home/launcher.sqlite` (stop `etude wait` first); an
open view shows `Launcher unavailable` within one poll and recovers without a restart.
Restore the file's original mode (`0600`) before finishing. A Playwright driver that
reads `inner_text` from tabs that reload itself must catch the read: a reload mid-read
raises and ends the driver silently while the launched job keeps running.

To drive the experiment page's report pane, give the experiment's `report()` QA
instrumentation: append a line to a gitignored log per call, `time.sleep(3)`, and raise
while a gitignored `FAIL` file exists. The log counts declaration reads as well as builds
(one of each per view start and source version). Open `/experiments/NAME` and
`/experiments/NAME/report` before launching: both read "Waiting for run files.", then
"Building report…", then the failure with **Retry report**; remove `FAIL` and click it.
The pane is the `#report` iframe (`page.frame_locator("#report")`); count its reloads with
`page.on("framenavigated", …)` for frames other than `page.main_frame`. Two concurrent
POSTs to the report URL should add one `report()` call. Poll `/experiments/NAME/status`
twice a second through a live run and require no "Run files:" note: a writer folds its
tails between the view's listing and its read.

A real selected server needs no root: run a user sshd on this Mac
(`/usr/sbin/sshd -f CONFIG`) with `Port 2222`, `ListenAddress 127.0.0.1`, a fresh
`HostKey`, `AuthorizedKeysFile` holding `~/.ssh/id_ed25519.pub`, `UsePAM no`,
`StrictModes no` and `SetEnv ETUDE_HOME=/tmp/…/home`, so the server side never opens
`~/.etude`. Put an `ssh` shim first on `PATH` (for the client, `etude view` and
`etude wait`) that execs `/usr/bin/ssh -p 2222 -o StrictHostKeyChecking=accept-new
-o UserKnownHostsFile=… -o BatchMode=yes "$@"`. Initialise the server's home with
`etude database upgrade --development` under its `ETUDE_HOME`, then `etude server set
jack@127.0.0.1`. Its pool is `server`, a server launch needs `--source`, and its
local machine delivers to the server's `home/track` whatever `--source` says; view
those files with `--source file:///…/home/track`. Killing the sshd PID gives a failed
launcher read; `etude server unset` and the PID ends it.

To take the view's run storage away and bring it back without spend, put a bucket on
this Mac: `uv run --no-project --python 3.12 --with "moto[server]" moto_server -p 5055`,
create a bucket of three or more characters, and give only the view a small asyncio TCP
forwarder (`:5056` to `:5055`) through `AWS_ENDPOINT_URL=http://127.0.0.1:5056` with
dummy `AWS_*` keys (the lab needs `etude-sdk[eval,s3]`). Run `etude view . --source
s3://BUCKET/runs`. Local launches still write `home/track`, so a driver thread uploads
changed files there straight to `:5055`, standing in for delivery. Killing the forwarder is
the outage; starting it again is recovery. A never-reached prefix shows a fresh index
before any successful file pass. Do not name a driver after a standard module
(`queue.py` shadows the `queue` that boto3 imports). `etude launch --pool local` refuses
while the one local slot is busy; use `etude queue` for a queued entry. `etude wait`
returns once the runs it started with end, so drive a later queue entry with another wait.

To count what the view reads from that bucket, have the forwarder log each request line
(`GET /BUCKET/KEY`); discovery should show listings plus `run.json`/`outcome.json` only.
For the real Manabot history, run the branch from `~/src/manabot` with `uv run --no-sync
--with-editable 'REPO[s3]' etude view experiments/ --source r2://manabot/jobs`. That
overlay leaves manabot's environment untouched. Use a private `ETUDE_HOME` and the
Doppler R2 keys from `manabot/prd`. Cap `du -sm HOME/cache`, not the whole home: the
view's index (`views/*.sqlite`) reaches ~80 MB and a temporary file briefly doubles the
home. A cold first pass should publish ~58 experiments in about a minute on ~50 MB of
cache (legacy `run.json` files get one 4 KiB ranged read), and a restart should publish
within seconds with no new cache bytes. Delete the home afterwards.

A directory source with an incomplete listing needs no bucket: under the view's
`home/track`, `mkdir -p locked/inner && chmod 000 locked` beside the runs (never the
run's own folder, which its writer needs), and `chmod 755` to recover; restore it in a
`finally`. The "no end recorded" rule needs a real 10-minute window, so give that
driver its own background run. Start long drivers with the shell tool's background
option, not `nohup … &`: a shell `&` job ignores SIGINT and every child it spawns
inherits that, so `etude view` then looks hung on SIGINT. SIGTERM stops it either way.

For interruption, make a separate lab copy with more updates and a four-second
pause per update so the driver can stop training between saves. Use
`uv run python` to start `uv run etude train experiment.py` as a subprocess.
Send SIGINT to that process four seconds after launch. Training prints no
progress lines, so waiting on `readline` can block the driver. A background
shell job may ignore SIGINT. Require exit 75, then follow the resume command
printed by the next plain `etude train experiment.py`. Read
`etude.train.read.read(etude.storage.default_root(), "experiment",
"coin@0").points("heads", "updates")`. Check the update numbers in `.x` and
the metric values in `.y`. Each update must appear once, in order, across
both metric writers.

To check block selection, copy the run folder on every poll while it writes.
The copies hold level-1 blocks and tails that the final fold replaces with a
level-2 block. Put the older files beside the finished files in a copy and
read it with `--source`. Each update must still appear once. On a long run,
`Latest metric` shows the latest bucket mean.

Training code calls `track(config=config)` only inside Etude's assigned run.
Saves (`run.resume` / `run.save`) restore learning; `run.checkpoint(path)`
publishes evaluation inputs. Do not simulate worker identity in a standalone
tracking script or tune internal queue/block settings.

## Etude: report notes and anchors

To seat an outside checkpoint, commit a small file in the lab and declare
`CheckpointPlayer(name=..., checkpoint=CheckpointRef(address=URI,
artifact=ArtifactRef(uri=URI, sha256=..., size_bytes=...)))` with
`URI = path.resolve().as_uri()` and no `at`. To compare against main, run
`git archive main src | tar -x -C DIR` and draw the same home with
`PYTHONPATH=DIR/src uv run python -c 'from etude.cli import main; ...'
report FILE --out OTHER`. The page's notes sit in its embedded JSON
(`"notes": [...]`). Serve both pages with `uv run python -m http.server --bind
127.0.0.1` to drive the toolbar, and stop that server afterwards. Separate
tournaments (or environments) are separate selections, so a cross-environment
overlap note needs tournaments merged from different experiments. Drawing a
Manabot report from `r2://manabot/jobs` with a cold `ETUDE_HOME` took over 30
minutes, so prefer a lab.

## Etude: evaluation recovery and placement

Copy `examples/{agents.py,agent_game.py,agent_models.py,agent.txt}` into another
committed lab to exercise wrappers and shared checkpoint leaves. Keep one
`config={"lane": name}` per seating and indexed matches. Increasing `games`
keeps the completed prefix; changing config or base seed changes match keys.

- Run `uv run etude eval agents.py` twice; the second pass plays nothing.
  Query `statsdb` from `etude.eval.query` for matches, players, seats and scores.
- In separate fresh labs, raise `RuntimeError` or `RetryRequested` (from
  `etude.errors`) in the checkpoint loader. Ordinary failures require
  `--retry-failed`; transient failures retry on the next pass. Fail every
  undelivered match using that leaf across tournaments, even when play catches
  the error. Preserve successes delivered before the failure and unrelated
  results. Use controlled ordering, not a timing race, to check both cases.
- Interrupt eval after a progress line, then rerun with the same home and
  source. Only unfinished matches should play. Relocate retained checkpoint
  bytes and update their locators while preserving keys, digests and lengths;
  bare and composed players retain their keys and delivered results.
- For explicit manual match lists, use `evaluating` from
  `etude.eval.tournament`: `with evaluating(environment, experiment=...,
  matches=matches) as run: run.play(play, agents=Agents(...))`. Put execution
  under `if __name__ == "__main__":`; play, load and build are importable
  module-level functions. There is one signature, `play(match, agents)`.

CPU games load forwards directly and keep one model-library thread per game.
On visible NVIDIA hardware, a shared agent process batches checkpoint forwards;
only that path keeps model loading out of game imports. Code-only builds start
no loading process and perform no hardware probe. Check controlled CPU/memory
capacity, cross-tournament batching, multiple leaves, eviction and partial-batch
drain with bounded fixtures; never create resource pressure. Keep complete
results and protocol counts separate from timing observations. Hardware capacity
does not select the author's model device. Do not rent just to refresh QA notes.

Returning-user checks explicitly read released config panels and multi-game
records with their original keys and seeds. They do not teach those formats for
new declarations. Use a disposable retained-data copy, never operational state.

## Etude: local launcher and source-free continuation

In a fresh lab home, run `uv run etude database upgrade --development` before
launcher commands for unmerged SQL. Reads never initialize the database.
`uv run etude launch experiment.py --pool local` submits pending work;
`uv run etude wait --experiment experiment` drives it. Launch again to discover
evals after training, then wait again. Read printed outcomes, logs (`etude logs --run RUN --job-only`) and cleanup
receipts: exit 0 from wait alone does not establish success. `wait` takes no
time limit and macOS has no `timeout`; bound it with the shell tool's own timeout. Each job uses an
environment separate from the client. Keep `max_hours` and `max_usd` bounded.

Check a continuation with `uv run etude continue --run ADDRESS --until updates=N`.
The parent must have logged at least one metric row: `continue` refuses a
clock that was only stepped (`a continuation needs a target on a clock its
parent recorded`). A continuation keeps the parent's config, so a recipe that
asserts its final count against `config.updates` fails past the old target.
For the SSH scheduler path, submit frozen source once, make the client's
checkout unavailable, and continue the retained run. The server must reuse its
source archive without requiring another client checkout or transferring the
same archive again. Preserve source-free output fetch and launch-failure evidence.
The controlled stand-in SSH scenario is
`tests/launch/test_server.py::test_remote_launch_freezes_sources_config_and_returns_outputs`.

Placement errors need pending work: an empty owned pool quotes no free machine;
a disabled pool refuses placement. Cancel lab work and wait for cleanup before
finishing. Credentials for explicitly authorized bucket/remote scenarios come
from Doppler, injected into the command; never print them. Keep each scenario's
home and storage prefix isolated and remove only its own resources.

## Etude: explicit launcher upgrades

Use an empty lab `ETUDE_HOME` and explicitly run `etude database upgrade
--development` before launcher scenarios with unmerged SQL. Ordinary status
and library opens must leave missing or outdated state unchanged. Real-data
snapshots are inspection inputs only: never call wait, tick, cancel, rent or
refresh against them. Keep remote training and guardians running. Recovery
requires stopping scheduling and reconciling provider state.

Use controlled additive and breaking migrations on the canonical file. Check
that a compatible open writer keeps working, an incompatible writer stops,
failed SQL rolls back, and backups and exports preserve the original facts.
Inspect with a compatible writer holding committed WAL data; status must read
it without copying the database. Normal SQLite sidecar coordination is allowed.
Read-only inspection must not change scientific or operational records.

Working route on the installed wheel: `uv build --wheel`, `uv venv --python 3.12`,
`uv pip install` it, then drive `VENV/bin/etude` with `ETUDE_HOME` per scenario.
Before merge, non-development `database upgrade` correctly refuses
(`unmerged or unproven migrations: …`): `merged.json` is `{}` in a branch wheel,
so upgrading an existing home and the backup/rollback path stay test-only until merge.
A newer etude's migration, simulated by a fixture, must insert the ledger row
(`name, sha256, floor, program, applied_at`) **and** set `PRAGMA user_version` to the
row count, as `_apply` does; without the pragma an open writer's fence never sees it.
For a returning user, `cp -c` (APFS clone) the retained consolidated snapshot into a
lab home, and deny the network with
`sandbox-exec -p '(version 1)(allow default)(deny network-outbound (remote ip))' CMD`.
Copy a live old file only with `sqlite3 "file:SRC?mode=ro" ".backup DST"`.
Old QA folders under `/tmp` can hold read-only prepared venvs; use a new folder.
`etude usage` with no filter reads every transfer (about 135 s on 1.77 M rows); run it once.

## Etude: rehearsing a launcher database upgrade on this machine's data

The one-time conversion of this machine's pre-ledger files is separate from the
shipped forward-upgrade API. Inventory and snapshot actual files first, using
SQLite's read-only backup API. Rehearse the conversion in a private temporary
home without scheduling. Compare distinct facts and allocation identities,
including runs, sessions, rentals, attempts, cohorts and transfers. Deduplicate
inherited facts and retain original policy definitions in recovery snapshots.
Never infer exclusive ownership from a quiet WAL or heartbeat.

Operational conversion requires stopping identified local scheduling clients,
updating every relevant environment including Manabot, and retiring old active
filenames to private recovery storage. Workers and guardians continue. Use
merged code for the final schema, verify the one operational database through
CLI and library reads across experiments, and never rent as part of validation.
Do not complete the Task on merge alone.

Before QA, read the PR's CI (`gh pr checks N`, `gh run view RUN --log-failed`).
An `unmerged or unproven migrations` failure in a fixture that upgrades a
non-empty home means it patches `_merged` too late: patch it before the first upgrade.
A `launcher.sqlite is not initialized` failure can mean a fixture sets its own
`ETUDE_HOME` or calls `client._open(path)` without `upgrade(path,
development=True)`. Conftest initializes only its autouse home. `etude smoke`
initializes its own folder.

When a pinned `Counts(database=N)` moves, compare statements with main. Use a
temporary trace in `tests/launch/counting.py` to record normalized statements
in a file named by an environment variable, then remove the trace. Fenced
writes add `BEGIN IMMEDIATE`, `PRAGMA user_version` and `COMMIT`.

## Etude: a live RunPod check, when a Task authorizes spend

Clone the checkout into `scratch/qa/repo` (commit first; a clone misses
uncommitted edits), `git remote remove origin` so nothing is pushed to GitHub,
and `uv sync` there. Drive `repo/.venv/bin/etude` from `repo` with an isolated
`ETUDE_HOME`; wrap it in `doppler run -p etude -c prd --only-secrets RUNPOD_API_KEY
-- doppler run -p manabot -c prd --only-secrets R2_ACCOUNT_ID --only-secrets
R2_ACCESS_KEY_ID --only-secrets R2_SECRET_ACCESS_KEY --only-secrets R2_API_TOKEN --`.

```sh
etude database upgrade --development     # a branch build refuses without it
etude storage add r2 --bucket manabot --prefix launch/etude-r2-smoke-DATE-TASK --provider r2
etude provider set runpod --kind runpod --storage r2
etude ondemand add demo-cpu --provider runpod --vcpus 2 --memory-gb 4 --max 1 --max-usd-per-hour 0.20
etude ondemand add demo-gpu --provider runpod --gpu "NVIDIA RTX A6000" --vcpus 4 --memory-gb 16 --max 1 --max-usd-per-hour 0.75
etude launch examples/prototype.py --rent demo-cpu --max-usd 0.50 && etude wait
etude machines --all                     # ended rentals read `deleted, …, ended`
```

The CPU pod is cpu3c at $0.06/hour, about a minute per run. The prototype's
eval budget (`max_usd=0.10`) does not cover a GPU hour; raise it in the clone
(0.30 quotes $0.18 on an A6000) and raise `games` so the eval has new matches.
RunPod's catalog can say `rentable now` while every create is refused with 400.
To find stock, add one throwaway on-demand per GPU (`--gpu "NVIDIA GeForce RTX 4090"`,
`"NVIDIA RTX A5000"`, `"NVIDIA L4"`, …) and run `etude availability NAME --timeout 30`;
only `rentable now` under its ceiling is worth a create. A queued run needs a loop: drive
`etude wait` from a Python subprocess driver that samples `etude status` and sends SIGINT
(exit 130), since `wait` keeps waiting while creates are refused. A cancelled eval run reads
as failed: the next launch needs `--retry-failed`. `etude report FILE --source r2://… --out DIR`.
On a GPU the job log shows `etude: agents: … N inputs in M batches` for the shared agent process.
List pods with `curl -H "Authorization: Bearer $RUNPOD_API_KEY"
https://api.runpod.io/v2/pods` (`.pods[]`); `rest.runpod.io` does not resolve
here. Other sessions' `etude-*` pods may be present; touch only your own.
zsh does not word-split a `$W` wrapper variable: put the Doppler chain, `ETUDE_HOME`
and `cd repo` in a small `scratch/qa/e` bash script ending `exec … etude "$@"`.

Shared slots cost cents on CPU: `ondemand add cpu3 … --slots 3`, then copy the prototype
with `seeds=(0, 1, 2)` and `max_usd=Decimal("0.006")` (a share of 0.26 h at $0.06 is
$0.0052; one seed alone needs $0.0156 and is refused). Commit each variant in the clone.
`etude rent cpu3 --hours 0.3 --max-usd 0.02` gives a manual machine whose free slots a
launch fills; `etude terminate MACHINE` ends it. Read exact run dollars with
`etude.launch.client.status(RUN).usd` (status rounds to cents). Open the lab
`launcher.sqlite` without `?mode=ro`: read-only opens of its WAL file fail.

## Etude: a live Lium check, when a Task authorizes spend

Same clone and Doppler wrapper as RunPod, adding `--only-secrets LIUM_API_KEY`
from `etude/prd`; export `LIUM_SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)"`.
Launch from the clone (`examples/prototype.py`), never from a `/tmp` lab whose
`etude-sdk` is a local path source: the job's remote `uv sync` fails with
`Distribution not found`. Raise the training job's `max_usd` to 0.60 in the
clone: Lium reserves a whole hour ($0.36–0.41 on a 4090).

Lium nodes took about fourteen minutes to reach RUNNING, and a launch-rented
machine lives only job hours + `setup_seconds` (900, not settable from the CLI);
`ondemand startup --seconds` cannot extend that. For a GPU job that must
finish, `etude rent ONDEMAND --hours 1 --max-usd 0.60` first, then launch.
Host keys are unknown before boot: put a QA-only `ssh` shim first on `PATH`
that adds `-o StrictHostKeyChecking=accept-new`. Executor `4b606116-…` (DE)
stayed `PENDING / connecting to node` on two rentals; `--data-center GR` is
the node that booted, when it is in stock. Read the pod inventory with
`LiumAPI(key).ask("GET", "pods")` inside `limit_time(30)`. A run that loses
its machine retries on a fresh rental: set `etude ondemand max NAME 0` and
`etude cancel` before finishing, then confirm `GET pods` is `[]`.

## Etude: a live EC2 or Lambda check, when a Task authorizes spend

Rent from the repo clone (as for RunPod), never from a `/tmp` lab whose
`etude-sdk` source is an editable path on this Mac: the remote job's `uv`
cannot resolve that path and the run fails after the machine boots. S3 storage
needs the client's `s3` extra; without it a command says only `s3_requires_boto3`.
AWS keys come from the SSO role, Lambda keys from Doppler:

```sh
eval "$(aws configure export-credentials --format env)"
doppler run -p etude -c prd --only-secrets LAMBDA_API_KEY --only-secrets LAMBDA_TERMINATE_KEY -- CMD
etude storage add west --bucket etudefantasia --prefix manabot/etude-launch/qa-TASK --provider s3 --region us-west-2
etude provider set aws --kind ec2 --storage west
etude ondemand add t4 --provider aws --gpu T4 --max 1 --max-usd-per-hour 0.70
etude availability t4 --timeout 60        # g4dn.xlarge ... $0.5438/hour
```

`availability` reads are free; probe spot, quota (`--data-center us-east-1`
spot is 0 vCPUs), unknown GPU names and providers without spot there first.
The prototype's `max_usd=0.10` does not cover a T4 window; raise it to 0.30 in
the clone. Boot to job takes about six minutes (image pull). us-west-2 spot is
often refused in every zone; a refused create rents nothing. Confirm cleanup
with `aws ec2 describe-instances` / `describe-volumes` filtered on
`tag-key=etude-session` (state reason `Client.UserInitiatedShutdown` is the
host's own power-off).

To prove interruption and resume, add a job that sleeps per update, saves every
update and appends `ETUDE_ATTEMPT` to its resumed extra; give it `max_usd=0.60`
(the admission check adds setup time to the hours). Start `etude wait
--experiment NAME` with output to a file, wait until `etude status` shows a few
updates, then `aws ec2 terminate-instances` on the instance tagged with its
session. A standard-price loss reads `2 of 3 attempts used`; a spot loss keeps
`1 of 3`. The attempt is recorded lost only after the old volume is gone
(about six minutes). `etude fetch RUN NAME DIR` takes the directory positionally;
the fetched file should list both attempt ids. `status` prints `last loop: never`
under `etude wait`: only `etude server run` writes that heartbeat.
