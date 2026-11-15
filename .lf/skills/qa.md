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

## Etude: driving a report

Build a lab outside the repo (e.g. `/tmp/lab`) with its own `ETUDE_HOME`:

1. Write training runs with `etude.train.tracking.track(experiment=..., run=..., job=...,
   clocks=[...], directory=RUNS)`, `run.log(...)`, `run.checkpoint(file, updates=N)`,
   `run.finish(timeout=60)`, via `uv run --project REPO python script.py`. Track the
   same run again to make a resumed attempt.
2. Put `NAME.py` (with `eval_jobs(state)` and `report()`) and its play module in the lab,
   then `git init` and commit: `etude eval` refuses play functions outside a git checkout.
   Play with `uv run --project REPO etude eval NAME.py --source RUNS` from that folder.
3. Serve with `uv run --project REPO etude report NAME.py --source RUNS --serve --port P`
   (a folder path serves an index). Drive it in Chrome; `curl -X POST /` is the Refresh.
   Edits to `charts.js`/`page.html`, the experiment file and the helper modules it imports
   from its folder take effect on the next Refresh; edits to Etude's Python need a restart.
4. Mixed execution setups: have `play` return `Result(tables=..., execution={"device":
   os.environ["QA_DEVICE"], ...})` and run `etude eval` once per device. To interrupt a
   pass part way, slow `play` with a sleep and send SIGTERM (`pkill -TERM -f "etude eval
   NAME.py"`): a backgrounded job in a non-interactive shell ignores SIGINT. Results
   played before the signal are sealed; the next pass plays only the rest. A seating's
   name is its identity (`seating_conflict` if its conditions change): add new seatings
   under the same tag instead. Replay already-played matches on another setup with
   `etude.eval.match.evaluate(experiment=..., matches=..., run="replay-x")`, taking the
   benchmark from `Evaluation.model_validate_json(job.config)` for each job
   `etude.experiment.asked(read_experiment(file).eval, observe(RUNS, NAME))` returns.
   Inspect provenance with `etude.eval.query.statsdb(NAME, RUNS)` (`matches.execution`).
   A page's own numbers are in `JSON.parse(document.getElementById("data").textContent)`
   (`cells` with `selection`, `subject`, `matches`, `rows`).
5. Export `ETUDE_HOME` for `python -c` statsdb reads too: otherwise `statsdb` keeps its
   database under `~/.etude/statsdb/`. To see what `etude status` would say of a launched
   eval pass without renting, run `etude eval` with `ETUDE_OUTPUT_DIR=DIR` and render
   `DIR/etude-result.json`'s `tally` with `etude.launch.records.Tally`.
6. Stop the servers before finishing. In zsh, `$args` does not word-split: call
   commands explicitly rather than looping over argument strings.
7. A "still running" lab run must not `os._exit` right after `run.ship()`: with a plain
   `directory=` there is no destination, so `ship()` is True at once and the background
   writer has not yet written `checkpoints/NAME.json`. The `.pt` without its manifest reads
   as no checkpoint (the run vanishes from every curve). Wait until that `.json` exists
   before exiting. `run.checkpoint` needs a value for every declared clock (otherwise
   `checkpoint_clock_mismatch`, and nothing is recorded). `play` must live in its own module beside
   the experiment file, not in the file itself. The experiment is named for its file, so
   a second report variant goes in another git-tracked folder as `NAME.py`.
8. Bucket path: `etude sync --directory RUNS --to r2://manabot/etude-r2-test/NAME`
   under `doppler run -p manabot -c prd --only-secrets R2_ACCOUNT_ID,R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY`,
   then `etude report ... --source r2://manabot/etude-r2-test/NAME` with a fresh
   `ETUDE_HOME`. `du` of that home after each build shows the bytes the build read.
   Afterwards delete the prefix with `S3Storage.client.delete_objects`.
   The aws CLI also works under the same `doppler run`: `sh -c 'AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
   AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY aws s3 rm --recursive --region auto --endpoint-url
   https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com s3://manabot/etude-r2-test/NAME/'`.
9. To tell a regression from an old bug, run the same script on main: `git worktree add
   --detach /tmp/NAME-main main`, `uv run` from there, then `git worktree remove`.
   Compare `etude.page.build(RUNS, FILE)` by hashing `page.training.model_dump_json()`
   and `repr(page.cells)`; rendered HTML carries timestamps.

10. Repository-level report modules (a package beside `experiments/`): make the lab a
   uv project, `dependencies = ["etude-sdk[eval]"]` with `[tool.uv.sources] etude-sdk =
   { path = REPO, editable = true }` and a hatchling build of its package, `uv sync`, then
   run `uv run etude ...` from the lab, not `--project REPO`. `/tmp` is a symlink to
   `/private/tmp`, which exercises resolved-path module matching.
11. Launcher-style jobs without launching: set `ETUDE_JOB` (the launcher's key, which
   hashes the commit) and `ETUDE_JOB_NAME` (the launch name) before `track()`; a new key
   under one name is a relaunch at another commit, a different `config=` a config change.
   Run names are global across experiments (`run_name_belongs_to_another_experiment`):
   prefix a second experiment's runs. Use one `Benchmark` across all runs: a benchmark
   per run makes each panel show one seed's tournament. Make `play` depend on the run
   (`player.model_dump_json()` holds the run name), or every job's curve overlaps.
   Changed `play` code reuses delivered matches: delete `RUNS/ladder.*` and `ETUDE_HOME`
   to replay.
12. When Chrome extensions are not connected: `npm i playwright-core` in a scratch folder
   and launch with `executablePath` set to the cached
   `~/Library/Caches/ms-playwright/chromium_headless_shell-*/…/chrome-headless-shell`.
   Click `getByRole("button", {name: "Refresh"})` and read `document.body.innerText`.
   `page.on("pageerror")` and `page.on("console")` show script errors; a blank chart with data
   in the page JSON is otherwise silent.
   Gap reasons and point values are only in hover text: `page.mouse.move(x, y - scrollY)` on a
   point in a full-page screenshot, then read `innerText` of `.u-legend` and `[class*=tip]`.
   A seating curve's per-checkpoint data is the page JSON's `points`: `selection` indexes
   `selections`; `at` gives its position, `value` its score, and `gap` (an eval `Gap`; its `missing` is `not_yet` or `never`) why it has none.
13. Cross-experiment seats (`Final(experiment=...)`, `PolicyKey`): write each experiment's
   runs with `track(experiment=E, run="small@0", job="small", seed=0, ...)` into one source;
   two experiments may share a run name. Hold another experiment's checkpoints with
   `observe(SOURCE, OTHER).policy(PolicyKey(...)).checkpoints`. An experiment with no runs of
   its own still evaluates ("Evaluation only"). To replay an eval from scratch, delete both
   `RUNS/E/NAME.*` and `ETUDE_HOME/track/E/NAME.*`: with only the source copy gone, the pass
   resumes the finished local run and re-ships just `outcome.json`, and the page shows
   nothing recorded. `etude.page.build` takes `Path`s. The page's legend and
   `players[].line` show whether foreign checkpoints sit on their own lines. Returning-user
   check: same file and runs evaluated on a `main` worktree, then `etude eval` here must
   print `nothing to play`, and hashing `(subject, opponent, value, rows, matches)` over
   `build(...).cells` must match.
   Adding a seed to an every-seed seat replays most matches: games are re-split over
   the new seed pairs, so match keys change (main plays 16, then 28 more for a third
   seed). Compare with a main run before calling a replay count a regression.
   Keep helper scripts in their own folder with names that are not stdlib modules:
   a `/tmp/X/inspect.py` beside the script shadows `inspect` and breaks `etude.page`.
   Drive the Python path as well as the CLI: a script calling `evaluating(bench,
   experiment=E, checkpoint=ref, directory=SOURCE, storages=[], run="hand-x",
   lines=observe(SOURCE, E).lines + observe(SOURCE, OTHER).lines)` then `run.play(play)`,
   run from the lab with `PYTHONPATH=LAB` so `games` imports. Build `ref` each documented
   way (`PolicyKey(...).checkpoint(name=, artifact=)`, flat `CheckpointRef(job=, seed=,
   name=, ...)`, `CheckpointRef(address=...)`). `ArtifactRef` needs `sha256` or the ref
   fails validation. A hand-built ref of a tracked file must give its stored position
   (`name="out/policy-200.pt"`); `name="policy-200.pt"` keys `.../checkpoints/policy-200.pt`,
   a second player on the same line. Compare `seats.line` per eval run in `statsdb`
   with the CLI pass's. In zsh, an unmatched glob aborts the whole `rm`; use `find -exec rm`.
14. The stats database under `ETUDE_HOME/statsdb` rebuilds its derived tables (`players`,
   `schedule`) only when new data arrives or its `SCHEMA` changes, so after editing
   `etude/eval/query.py` delete that folder before judging the page.

## Etude: storage outages on the SDK and a launched worker

SDK against fake S3: `uvx --from 'moto[server]' moto_server -p 5047`, then put a
small TCP proxy in front of it that, when a mode file says `dead`, accepts new
connections and stops forwarding on existing ones without closing them. boto3 reuses
keep-alive sockets, so a proxy that only refuses new connections never causes an
outage. Run the script with `AWS_ENDPOINT_URL=http://127.0.0.1:PROXY`, dummy AWS keys,
`storages=[storage_from_uri("s3://BUCKET/runs")]` and a short
`ETUDE_STORAGE_TIMEOUT_SECONDS`. Time how long the process takes to exit as well as
`finish()`: s3transfer threads hold interpreter exit. Add
`faulthandler.register(signal.SIGUSR1, all_threads=True)` to see where it waits;
py-spy needs root on macOS. Recover with `etude sync --directory RUNS --to s3://…`.
It prints nothing on success, so list the bucket afterwards.

Launched worker without renting: the built-in `local` machine has no storage, so use
an SSH host. Build a `python:3.12-slim` image with openssh-server, iptables and
`/uv` copied from `ghcr.io/astral-sh/uv`, plus your public key in
`/root/.ssh/authorized_keys`. Run it with `--cap-add NET_ADMIN -p 127.0.0.1:2247:22`.
Leave `~/.ssh` alone: put `ssh`/`scp` wrappers first on `PATH` that exec
`/usr/bin/ssh -F /tmp/…/ssh_config` (a Host alias with Port, `StrictHostKeyChecking
accept-new` and its own `UserKnownHostsFile`). With an isolated `ETUDE_HOME`:
`etude storage add qa --bucket manabot --prefix etude-qa/NAME --provider r2`,
`etude machine add box ALIAS --storage qa`, then from a committed job repo (a uv
project depending on `etude-sdk`, with `package = false`) run `etude launch job.py
--pool box` followed by `etude server run` in the background. Both need
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` **and `R2_API_TOKEN`**
from Doppler `manabot/prd`. Without the token, launch is refused (`capacity_refused`).
Cut storage with `docker exec box iptables -A OUTPUT -p tcp --dport 443 -j DROP`;
`-D` restores it. Read the worker's own log with `etude logs --machine box` or
`/root/.etude/sessions/*/worker.log` and `usage.jsonl` in the container. Restart
that loop after code changes. Afterwards remove the container and delete the
`etude-qa/NAME/` prefix.

## Etude: launching an experiment file on this computer

Initialize the empty lab launcher with `etude database upgrade --development`
before its launcher commands. Reads never initialize it.

A lab that runs typed jobs (`TrainingJob(name=..., train=train, config=...)`) is a git repo, outside the
repo, with an isolated `ETUDE_HOME`:

- `pyproject.toml`: `dependencies = ["etude-sdk[eval]"]` (the `eval` extra brings
  duckdb; without it every tournament fails), `[tool.uv] package = false`, and
  `[tool.uv.sources] etude-sdk = { path = "<this checkout>", editable = true }`.
  `uv lock`, add `.gitignore` with `.venv/` and `__pycache__/`, and commit, or every
  run says "plus uncommitted changes".
- `train.py` with a Pydantic config whose real size raises, so a passing smoke shows
  its smoke form ran; `games.py` with `play(match)`; `ppo.py` with `training_jobs()`, `eval_jobs(state)`
  and `report()`. A job's `max_usd` must be more than zero or omitted.
- `uv run etude smoke ppo.py`, `uv run etude launch ppo.py`, then
  `etude status`. Nothing runs the scheduler's loop here: cancel what you queued
  (`etude cancel --experiment ppo`) and `etude wait RUN` until each says `done: cancelled`,
  so no worker is left behind. macOS has no `timeout`; use
  `perl -e 'alarm 60; exec @ARGV' uv run etude wait RUN`.
- Client jobs submitted directly from Python need `experiment=`
  (`job.model_copy(update=...)`). `etude launch FILE.py` defaults unnamed jobs
  to the file stem; recipes call `track()` without an experiment.
- Smoke trains each distinct `job_id` once: two jobs differing only in name share one
  smoke run, so give each lab job its own config.
- Pass output: unbuffered (`PYTHONUNBUFFERED=1`) to see stdout and stderr in order;
  a failing pass otherwise shows its error before its state line.
- A `--keep` smoke folder's `track/` holds a tiny tournament (games=1): as an
  `etude eval --source` it gives `seating_conflict`. Copy only the training run's
  folder out for a clean source.

## Etude: tracking and reading a run against a bucket

Write a real run with `track(..., directory=D, segment_rows=500)` and `run.finish()`;
`queue_size=50_000` stops a tight logging loop dropping rows. Two `track` calls with the
same run name, the first with `run.checkpoint(path, updates=N)`, give a resumed attempt.
Deliver with `uv run etude sync --directory D --to r2://manabot/etude-r2-test/NAME`, with
secrets injected by `doppler run -p manabot -c prd --only-secrets
R2_ACCOUNT_ID,R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY -- ...`. Set `ETUDE_HOME` to an empty
directory: `~/.etude/cache` holds ~200k files and each cached range read scans them all,
so bucket reads take minutes there. `tests.run.storage.MeasuredStorage` counts requests
(needs `PYTHONPATH=.`). Delete the prefix afterwards with `storage.delete` over
`storage.keys("")`.

## Etude: a recipe's out, planned stops and retries on a fresh host

Local recipe path: a lab outside the repo with its own `ETUDE_HOME` and a
wrapper script `bin/etude` that runs `exec uv run --project REPO --extra train
etude "$@"` (zsh will not split a command held in a variable). Copy
`examples/coin.py`, or write a recipe as `docs/training.md` shows
(`track(config=config)`, `run.resume(...)`, `run.steps(...)`, `run.save()`),
with a `time.sleep` per step so a stop lands mid-run. Then:

- `etude train FILE [--until clock=N]` (0 finished, 75 stopped); runs land in
  `ETUDE_HOME/track/ADDRESS/` (`run.json`, `outcome.json`, `metrics/`, `out/`).
- Ctrl-C: a background job in a non-interactive shell ignores SIGINT, so send it
  from a tiny Python driver (`Popen(argv)`, `sleep`, `send_signal(SIGINT)`,
  print `wait()`). `--seconds 2` gives a deadline stop instead.
- `etude train FILE --resume [--until ...]`; refusals to check: rerun without
  `--resume` (`run_name_taken`), resume a finished run (`run_already_finished`),
  changed `--config` (`run_identity_conflict`), a target on a clock the recipe
  does not count (`until_clock_undeclared`). Never pipe a run into `head`: the
  SIGPIPE fails the run.
- A `training_jobs()` file with `seeds=(0, 1, 2)` trains `NAME@0..2` in order; `--job`
  and `--seed` select. Do not teach `--resume` as finishing a partly started
  selection (an unstarted member is refused; Open 6).
- Read rows with `etude.train.read.read(ETUDE_HOME/track, EXPERIMENT, ADDRESS)
  .points(metric, clock).x`; `etude checkpoints` lists stored checkpoints;
  `etude report FILE` writes `ETUDE_HOME/track/reports/NAME.html`.
- Hanabi: `bin/hetude` running `uv run --extra train --extra eval --extra hanabi
  etude` from the repo root, then `docs/hanabi.md`'s commands; 2000 updates take
  long enough to interrupt at ~5 s.

Launch a plain file defining only `training_jobs()` to check that its
stem supplies the experiment. Also check an explicit `Job.experiment`: plain
files preserve it, while files defining `report()` or `eval_jobs(state)` require
their own stem.

For an installed-package check, build with `uv build --wheel --out-dir /tmp/qa-dist`,
create an isolated Python 3.12 environment with `uv venv --python 3.12 /tmp/qa-client`,
and install the wheel's `train,eval,hanabi` extras with `uv pip install --python
/tmp/qa-client/bin/python '/tmp/qa-dist/etude_sdk-VERSION-py3-none-any.whl[train,eval,hanabi]'`.
Invoke `/tmp/qa-client/bin/etude` with a separate `ETUDE_HOME`. Copy the examples
into a temporary git repo before `etude eval`; `Seating` needs `configs=({},)`
even when the game has no config fields. Hanabi's group scores can be queried
with `statsdb`/`compare`; current report charts draw individuals and explain when
team scores are omitted.

To exercise automatic retries without renting, declare a job with `max_hours`,
`max_attempts=2` and `Runtime(python=...)` pointing to a **separate job environment**
containing the wheel. Using the client environment as the job runtime is refused.
Have the real recipe register a counter, save at update 2, and raise `Stopped`;
its next allocation resumes at 3. Run `etude launch FILE --pool local`,
then `etude wait --experiment NAME`, inspect `etude logs --run ADDRESS`, and require a stopped
attempt followed by success. Check `etude continue --run ADDRESS --until updates=N`
too: implicit recipe clocks must work across branching. Check cleanup receipts and ensure
no owned worker remains after waiting. A wait command's exit 0 alone does not mean
the run succeeded; read its printed final outcome.

Manabot acceptance without touching its checkout: `git ls-files -z | rsync -a
--from0 --files-from=- . /tmp/X/` plus the in-tree `managym/_managym.*.so`; then
`uv venv -p 3.13` and `uv pip install` manabot's dependency list (without
managym, which would build Rust) with `-e <this checkout>`, and `--no-deps -e .`.
Apply the cutover in `scratch/questions.md` and run `.venv/bin/manabot train
--recipe belief --out DIR` (about 85 s; a rerun about 13 s).

SSH boxes when `docker build` hangs on registry metadata: `docker run -d` the
cached `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` (amd64 here, so pass
`--platform linux/amd64`), `apt-get install openssh-server git iptables procps`
inside, copy the key, `docker commit --change 'CMD ["/usr/sbin/sshd","-D","-e"]'`.
The job repo must install this branch on the box: build a wheel into
`wheels/`, depend on `etude-sdk[s3]` with `[tool.uv.sources] etude-sdk = {path =
"wheels/…whl"}`, and `uv lock --upgrade-package etude-sdk` after each rebuild
(same version, new hash). Wrap Doppler in a script (`withr2 CMD…`): zsh does
not word-split a command stored in a variable.

To place a retry on a fresh host deterministically: launch, `etude pool off P`
at once, `etude wait RUN` under `perl -e 'alarm 100'` until attempt 1 ends
`stopped`, then `docker rm -f` and `docker run` the box again on the same port
(`ssh-keygen -R '[127.0.0.1]:PORT' -f KNOWN_HOSTS`), `etude pool on P`, wait.
Grep `etude logs --run RUN` for the job's own lines. Per-session bucket reads
are in `sessions/S/out/usage.jsonl`. Destroying an owned host mid-attempt is
never declared lost (no deadline): the run stays `running`, and cancelling
leaves it `stopping`. Use a fresh `ETUDE_HOME` and prefix after installing an
interim schema. Afterwards remove the containers and delete the prefixes
(`aws s3 rm --recursive` under Doppler, as above).

## Etude: one pass over an experiment file (train, eval, launch, queue)

Lab as in "launching an experiment file on this computer", with `two.py` declaring
`training_jobs(state)` (base, seeds 0 and 1; then `fine` with `start=` base@0's
`final` once it is finished) and `eval_jobs(state)` (a `Benchmark` over every
`run.final`). Use a recipe that checkpoints a folder (`run.checkpoint(policy=...)`):
single-file checkpoints hide nested input names (`start/checkpoint.json`).

- Here: `etude train two.py` three times (stage one, stage two, all `finished`),
  `etude eval two.py` twice (`N new ... N played`, then `nothing to play`), `etude report two.py`.
- Launched: fresh `ETUDE_HOME`, `etude launch two.py --max-usd 5`, then
  `perl -e 'alarm 240; exec @ARGV' uv run etude wait --experiment two` (wait drives the
  local pool), and `launch` again, immediately and after waiting. In-flight matches read
  `0 new, N running`. `--max-usd 1.5` with `max_usd=1` per job holds the second seed.
- Ctrl-C: a slower copy of the file (`pause=0.5`) under the `Popen`/`SIGINT` driver after
  ~7 s gives exit 75; the next pass prints `stopped; --job base --seed N --resume`
  (which seed depends on timing); follow that hint.
- Cancelled before start: `etude queue two.py`, `etude cancel --experiment two`, wait;
  the next pass prints `failed; --job base --seed 0 --resume`; following it with both
  `etude train` and `etude launch` must start the run.
- Returning user: `git worktree add --detach /tmp/X main`, a lab whose pyproject points
  at it, train `runs()` files (compact `ppo@0`, `solo`) and `etude queue` (makes
  `launcher-19.db`). Repoint the pyproject at this checkout, `uv lock`, rename per
  `release/UNRELEASED.md`, and check stored runs read `finished`.

## Driving Etude

- Isolate each scenario: a fresh `ETUDE_HOME` and a fresh `--source` directory.
  Eval runs are written under `$ETUDE_HOME/track` and delivered to the source.
  For recovery, reuse the home and move checkpoint files without changing their
  keys, digests or lengths: bare and composed players must resume without
  replaying delivered matches. Changed author fields or checkpoint fingerprints
  must still raise `eval_run_tournament_conflict`.
- Evaluations: `uv run etude eval FILE.py --source DIR`, run twice (the second
  pass must play nothing), then `--retry-failed`. `examples/agents.py` covers
  grouped tournaments, a shared checkpoint and nested wrappers. To vary the
  game or loader, copy the example files into a temp folder and run
  `uv run --project <checkout> etude eval agents.py` from there.
- Results: `statsdb(experiment, DIR)` from `etude.eval.query`, using the
  `players`, `seats` and `matches` tables.
- Report: `uv run etude report FILE.py --source DIR --out OUT`, then look at it with
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new
  --disable-gpu --screenshot=OUT.png --window-size=1300,800 file://OUT/FILE.html`
  and read the PNG. The page renders in JavaScript, so grepping the HTML text
  shows nothing.
- Failure paths: copy the example into a git lab and edit `agent_models.py`
  (`raise RuntimeError(...)` for a bad checkpoint, `raise RetryRequested(...)`
  from `etude.errors` for a transient one) or `play` (raise in one lane). Each
  needs a fresh `ETUDE_HOME` and source; run three passes to see what is
  replayed, held for `--retry-failed`, or settled.
- To interrupt a pass at a known point, read its output from a Python driver
  (`Popen(..., stdout=PIPE, stderr=STDOUT)`) and send SIGTERM after the line
  `K/N matches played`; a fixed sleep misses because this Mac runs ~16 games
  at a time. The next pass should play only the interrupted matches.

## Etude: grouped evals with agents

Copy `examples/{agents.py,agent_game.py,agent_models.py,agent.txt}` into a git lab
and run `uv run --project REPO etude eval agents.py --source RUNS` with a fresh
`ETUDE_HOME`. Drive scenarios with environment knobs read inside the lab's own
modules: a `play` wrapper that raises when `QA_FAIL` appears in a player's JSON or
sleeps for `QA_SLOW` (make one player's matches ten times longer, or every match
finishes before a SIGTERM lands), a loader that raises when `QA_BAD` names a
checkpoint key, and `Agents(memory_gb=float(os.environ["QA_MEM"]))` (`3e-9` fits
one two-byte checkpoint and forces eviction). Send each pass's output to its own file:
`grep -v` in a pipe buffers until exit, so a slow pass looks hung, and several passes
in one tool call can exceed its timeout on a busy Mac. Judge a suspected hang from the
timestamps under `ETUDE_HOME/track/NAME/artifacts/agents/` and the run's `blocks/`,
not from `ps` elapsed times.

Retain the startup game count and inference thread count with protocol comparison
results. Controlled CPU/memory checks cover one CPU, fewer pending games than
CPUs, memory-limited games, agentless games and multiple tournaments; verify
one shared allocation reaches dispatch and the loading module. Keep scalar
Torch threads at one; grouped Torch loading honors `OMP_NUM_THREADS` before
model execution. Use the unchanged frozen panel, retain its whole-arm timer
before querying reports, and compare complete action sequences.

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
A `launcher.sqlite is not initialized` failure can mean a fixture sets its own
`ETUDE_HOME` or calls `client._open(path)` without `upgrade(path,
development=True)`. Conftest initializes only its autouse home. `etude smoke`
initializes its own folder.

When a pinned `Counts(database=N)` moves, compare statements with main. Use a
temporary trace in `tests/launch/counting.py` to record normalized statements
in a file named by an environment variable, then remove the trace. Fenced
writes add `BEGIN IMMEDIATE`, `PRAGMA user_version` and `COMMIT`.
