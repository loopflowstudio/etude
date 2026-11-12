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

1. Write training runs with `etude.train.run.track(experiment=..., run=..., job=...,
   clocks=[...], directory=RUNS)`, `run.log(...)`, `run.checkpoint(file, updates=N)`,
   `run.finish(timeout=60)`, via `uv run --project REPO python script.py`. Track the
   same run again to make a resumed attempt.
2. Put `NAME.py` (with `eval(state)` and `report()`) and its play module in the lab,
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
7. `run.checkpoint` needs a value for every declared clock (otherwise
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
   Gap reasons and point values are only in hover text: `page.mouse.move(x, y - scrollY)` on a
   point in a full-page screenshot, then read `innerText` of `.u-legend` and `[class*=tip]`.
   A seating curve's per-checkpoint data is the page JSON's `points`: `selection` indexes
   `selections`; `at` gives its position, `value` its score, and `gap` (an eval `Gap`; its `missing` is `not_yet` or `never`) why it has none.
13. The stats database under `ETUDE_HOME/statsdb` rebuilds its derived tables (`players`,
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
--pool box` followed by `etude controller run` in the background. Both need
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` **and `R2_API_TOKEN`**
from Doppler `manabot/prd`. Without the token, launch is refused (`capacity_refused`).
Cut storage with `docker exec box iptables -A OUTPUT -p tcp --dport 443 -j DROP`;
`-D` restores it. Read the worker's own log with `etude logs --machine box` or
`/root/.etude/sessions/*/worker.log` and `usage.jsonl` in the container. Restart
the controller after code changes. Afterwards remove the container and delete the
`etude-qa/NAME/` prefix.

## Etude: launching an experiment file on this computer

A lab that runs typed jobs (`training(train, config=...)`) is a git repo, outside the
repo, with an isolated `ETUDE_HOME`:

- `pyproject.toml`: `dependencies = ["etude-sdk[eval]"]` (the `eval` extra brings
  duckdb; without it every tournament fails), `[tool.uv] package = false`, and
  `[tool.uv.sources] etude-sdk = { path = "<this checkout>", editable = true }`.
  `uv lock`, add `.gitignore` with `.venv/` and `__pycache__/`, and commit, or every
  run says "plus uncommitted changes".
- `train.py` with a Pydantic config whose real size raises, so a passing smoke shows
  its smoke form ran; `games.py` with `play(match)`; `ppo.py` with `runs()`, `eval(state)`
  and `report()`. A job's `max_usd` must be more than zero or omitted.
- `uv run etude smoke ppo.py`, `uv run etude launch ppo.py --cohort NAME`, then
  `etude status`. Nothing runs a controller here: cancel what you queued
  (`etude cancel COHORT`) and `etude wait RUN` until each says `done: cancelled`,
  so no worker is left behind. macOS has no `timeout`; use
  `perl -e 'alarm 60; exec @ARGV' uv run etude wait RUN`.
- Client jobs built in Python need `experiment=` (`job.model_copy(update=...)`); an
  experiment file names it from its stem, and `track()` otherwise raises
  `experiment_not_named`.
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
