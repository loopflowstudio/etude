# v0.1.3

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.1.3 turns the launcher from a way to run a command on a rented GPU into a way to train and evaluate agents end to end. A job is now a typed Python function, a launch says whether it may use machines you own, rent new ones or both, rentals may be CPU pods for evaluation work, and eval runs record matches you can query with DuckDB and watch arrive in a live report. Underneath, every wait is a budget the caller owns, bucket traffic is bounded so no computer reads the same bytes twice, and the fixes from running a real tournament on RunPod are in.

## Describe work as Python, not a command

A training job is a function with a typed config; an evaluation job is a tournament and your `play`. Etude derives the command, validates the config before anything is quoted or rented, and ships the checkpoint as a verified input rather than a signed URL in job config.

- `training(train, config=TrainConfig(...), max_hours=2, max_usd=Decimal(4))` runs `train(config)` on the machine. The config travels as JSON and is validated back into the parameter type. A config of the wrong type, one that does not validate, or a function that cannot be imported by name is refused when the job is made, as a `LauncherError`.
- `evaluation(tournament, play=play, checkpoint=Input(...))` (or `enter=` for a round robin) runs Etude's own `hold` on the machine. The eval run is the launched run, so `etude logs` and `etude status` lead to it and a retry resumes it.
- `Job(inputs=(Input(name=, key=, sha256=, size_bytes=),))` delivers files to `ETUDE_INPUTS_DIR/NAME` before the command starts. Bytes that do not match the digest are refused and the attempt ends `lost (setup_failed)`. Inputs are part of the job's identity.
- `Job.runtime` verifies and reuses a preinstalled interpreter: optional Python prefix, exact distribution versions and PyTorch CUDA-build constraints. The probe runs before user code under the attempt deadline; a mismatch retains diagnostics and does not retry training. The selected executable is exported as `ETUDE_PYTHON`; Etude's client keeps its own environment.
- `prepare_pytorch()` and `etude image prepare` resolve a framework tag to an immutable digest and verified platform without pulling layers or renting. `prepare_pytorch()` returns both the Image and its matching Runtime, so choosing the image also chooses its bundled interpreter and Torch/CUDA requirements. Each machine retains the reference, digest and platform it was rented with (`etude machines --images`). Preparation needs Docker CLI with Buildx.
- `Job(command=...)` remains for work that is not Python. Job files now have their own folder on the import path, so a job file beside `train.py`, a root package or a `src/` layout all work.

## Say where runs may go: owned pools, on-demand rentals, CPU pods

A pool is now only machines you own. Renting is a separate, named thing with its own budget, and a launch names one or both.

```sh
etude pool add lab
etude machine add mini me@mini.local --pool lab --cost-usd 0
etude ondemand add cpu --provider runpod --vcpus 8 --memory-gb 16 --max 2 --max-usd-per-hour 0.40
etude availability --rent cpu              # asks the provider; rents nothing
etude launch job.py --rent cpu --quote     # first wave and maximum spend; rents nothing
etude launch job.py --pool lab --rent cpu  # free owned machines first, then rent
```

- `--pool P` uses only P's machines, `--rent R` only rents from R, and both together prefer a free owned machine. Python mirrors this with `pool=` and `rent=` on `queue`, `launch`, `queue_cohort` and `launch_cohort`, plus `quote(...)` and `availability(rent)`, also over the SSH controller.
- Owned machines carry a committed `cost_usd` and an `expires_at`. At expiry a machine leaves its pool; an attempt still on it ends `lost/machine_lost` and retries wherever the run may go.
- An on-demand whose template names no GPU rents a RunPod CPU pod. The quote picks the cheapest in-stock flavor at the smallest power-of-two size covering the vCPUs and memory asked for, and the pod created is the one that was priced. CPU pods follow the same rent, take work, terminate-when-idle and guardian-deadline path as GPU pods.
- Each machine records the hardware it was rented as; `etude machines` and `etude status` show it. On a CPU pod, size worker pools from `os.sched_getaffinity(0)`, because `os.cpu_count()` reports the host.
- `etude default --pool P --rent R` sets the defaults a launch falls back to. `etude ondemand add|max|image|startup` and `etude ondemands` manage rentals.
- No paid machine waits on a source upload. The source archive is uploaded once per digest to `PREFIX/sources/SHA256.tar` before anything is rented, and the rental window starts after that. A 229 MB archive previously held a pod idle for about 16 minutes. `export-ignore` in `.gitattributes` keeps large files out of the archive.

## Evaluate agents and query the results

Eval runs play matches in parallel worker processes and store the tables your `play(match)` returns as Parquet. Benchmarks, round robins and `connect` build on them, and everything is read through DuckDB.

```python
progress = Benchmark(
    name="anchors",
    environment=world,
    matchups=(
        Matchup(
            name="search vs no search",
            players=(Model(config=PUCT(simulations=64)), Model(config=Direct())),
            conditions=deals[:10],
            games=4,
        ),
    ),
)
hold(progress, checkpoint=ref, play=play, storages=[bucket])  # per checkpoint
hold(progress, play=play, workers=8, storages=[bucket])  # batch: fill what is missing

db = connect(bucket)
for line in curves(db, "anchors", table="plays", value="score"):
    print(line.matchup, line.run, [(p.ordinal, p.value) for p in line.points])
```

- `evaluate` is the general eval run and knows nothing about tournaments. An agent is a checkpoint or code plus a config, so one checkpoint at two search budgets is two agents. Checkpoint agents may also name their inference code, and the identity covers kind, checkpoint digest, code digest and config, so results from changed model code no longer pool with old ones.
- Identity hashing is stable across machines: inside a git checkout only tracked files count, and in a launched job's extracted source paths hash relative to `ETUDE_SOURCE_DIR`, which the worker now sets.
- A tournament is every eval run that names it. Each `hold` writes `tournament.json` into its own run folder, so on-the-go holds from rented machines count toward one tournament with nothing extra to deliver. Holding a checkpoint reads nothing of the tournament; a batch hold lists each place once and reads each eval run once. Conflicting definitions surface on read: a batch hold, `curves` and `standings` refuse them, `connect` reports them in `tournaments.conflict`, and the HTML report shows them.
- Matches are reused within an environment. `curves`, `standings` and the report count a scheduled match delivered by any eval run in that environment, once, from the latest delivery. Resume skips matches already delivered to any storage, so a fresh machine replays nothing. `hold` resumes by checkpoint digest rather than path, so a retried evaluation job is not refused.
- `share=(k, n)` deals scheduled matches round-robin, so shares differ by at most one match and a retried share keeps the same part. Bootstrap intervals sort blocks before resampling, so a directory and a bucket give identical intervals.
- `evaluate` and `hold` print `etude: RUN: 12/36 matches played, 220s elapsed` to stderr after each stored match.
- `etude reports eval --watch SECONDS` rewrites the report at that interval, and the open page reloads itself. It keeps polling until the first eval run exists, prints the path and opens the browser once, and closes its DuckDB connection after each write.

## Every wait is a budget the caller owns

Slow client installs were cut off by three conflicting internal limits. Now startup, setup, cleanup, TERM grace, final delivery and log publication add no deadline by default; an explicit finite limit is capped by the enclosing rental or attempt deadline, and children and retries share remaining time.

- Installer output is retained and sanitized while startup runs, including lock contention and deadline failures. Startup ends at the worker start receipt; setup inside the job command is job execution.
- Per-attempt setup and cleanup hooks run under the attempt deadline. Timed-out remote requests stay uncertain and are never replayed automatically.
- Hidden subprocess and network caps and the implicit delivery reserve are gone. The SQLite lock timeout (30 seconds) and the tracking finish timeout (10 seconds) remain, each separately overridable.
- The independent spending guardian survives stalled budget reads and still starts before dependency installation.

## Read each bucket file once per computer, and explain the traffic

The rule is that bytes read from S3 over a file's life are at most twice the bytes written, and no machine reads the same bytes twice. Transfer usage is retained so an operator can explain what traffic did happen.

- Source, checkpoint, input and completed-output downloads are cached by SHA256 and length with a 20 GiB default allowance. Interrupted downloads resume validated ranges; the complete file is verified before the destination is replaced. Retried attempts and later jobs on the same pod download nothing again. Inputs too large to cache are reused in place once they verify.
- Launcher polling asks by ETag. Inbox, attempt records, logs, usage and `ended` return 304 with no body while unchanged; the courier's inbox poll does the same.
- Tracking and eval reads from a bucket go through a per-computer object cache at `ETUDE_HOME/objects` (LRU, 20 GiB). Sealed files and block ranges are fetched once; other files are asked for by ETag. This covers `reports write --watch`, `reports serve`, `checkpoints`, `read`, `connect`, eval resume and batch holds. Six report generations read the same bytes as one.
- Uploads are verified by S3 with a SHA256 checksum and never read back. Multipart uploads complete with returned ETags, removing the part-listing requests and permission.
- `etude usage --run RUN` and Python `usage()` report request counts, bytes received and supplied, failures, retries and cache reuse from retained evidence, with request IDs for reconciling against provider access logs and caller-supplied cost estimates. Usage describes observed transport, not an invoice; installation, SSH, rental and storage byte-hours stay outside it.
- The worker log records state changes such as `inbox: 1 work, 0 to stop` rather than a line per poll, so `etude logs --run RUN` is readable without `--job-only`. Transfers go to the session's usage journal instead of the log.

## Operational notes

This release changes the prototype database, the public package layout and the pool CLI. Check these before upgrading an existing launcher installation or a downstream project.

- The launcher database advances through schemas 3 to 8 across this range and now refuses any older file; there is no migration. Preserve the old database and journals before selecting a fresh controller home.
- `etude.track` is split into `etude.run` (records, storage, delivery, listing shared by every run) and `etude.train` (rows, checkpoints, the training run, reports and `serve`). The training entry point is `from etude.train.run import track`, formerly `init`. There is no `etude.track` alias. The single-subject `Run.evaluation` API and `EvaluationRecord` are deleted; eval runs replace them. Training behaviour, the `~/.etude/track` layout and CLI output are unchanged.
- Removed from pools with no compatibility path: `Pool.on_demand_template`, `Pool.max_on_demand`, `MachineTemplate.max_usd_per_hour`, the provider, template and max flags on `pool add`, and `pool max`, `pool image` and `pool default`. `etude rent --ondemand R` replaces `rent --pool`. Executions answer `offer()` (hardware, price, availability) instead of `price()`.
- Tournaments stored under the earlier shared `tournaments/NAME/` layout are not read. Eval runs held before the per-run layout have no `tournament.json` and do not count toward any tournament. Any folder holding a `tournament.json` is treated as a run.
- An on-the-go hold does not check the tournament before playing, so a redefined matchup is played and only reported later by `connect`, a batch hold, `curves` or `standings`.
- During a provider image pull, cleanup depends on an active controller because the in-machine guardian cannot run yet.
- Live verification in this range: CPU rentals, pool-plus-rent placement, `--quote`, `availability` and job inputs each ran on real RunPod CPU pods, with pods confirmed deleted afterwards and total spend under a few cents. Image resolution, GPU execution, the typed-job path and the watched eval report were verified against fakes and the official PyTorch image, not against a paid machine.

## Small changes

- Two product races found by running the suite under load are fixed at their cause: a worker's attempt `run` file is written atomically so the courier can no longer read it empty and delete the pod, and on Darwin an EPERM from signalling a dissolving process group after the leader is reaped is treated as the group being gone.
- `Standing.opponent_name` and `CurvePoint.name` are available; checkpoints no training run recorded are ordered by name with numbers compared as numbers. `Model(config=PydanticModel())` type-checks.
- Pending SSH startup is inspectable and cancellable without holding placement authority, and stale preparation is rejected before a provider create.
- Counting tests pin provider, bucket, git and SSH calls for CPU rentals, source staging, inbox polls, inputs and warm report reads, so changes to a path that reaches another machine show up as a changed number.