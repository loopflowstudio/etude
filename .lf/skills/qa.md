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
For live refresh use `uv run etude report experiment.py --serve --port PORT`;
stop the exact process you started before finishing.

For interruption, make a separate lab copy with an author-controlled pause
(`time.sleep(0.1)` per update) and more updates. Drive `uv run etude train
experiment.py` with a Python subprocess driver, wait for a progress line, then
send SIGINT to that process. Require exit 75 and retained state. Follow the
printed `--job coin --seed 0 --resume` hint; check that updates continue from
the saved absolute coordinate. Use `uv run python` for the driver. A background
shell job may ignore SIGINT; do not signal processes by a broad name pattern.

Training code calls `track(config=config)` only inside Etude's assigned run.
Saves (`run.resume` / `run.save`) restore learning; `run.checkpoint(path)`
publishes evaluation inputs. Do not simulate worker identity in a standalone
tracking script or tune internal queue/block settings.

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
evals after training, then wait again. Read printed outcomes, logs and cleanup
receipts: exit 0 from wait alone does not establish success. Each job uses an
environment separate from the client. Keep `max_hours` and `max_usd` bounded.

Check a continuation with `uv run etude continue --run ADDRESS --until updates=N`.
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
A `launcher.sqlite is not initialized` failure can mean a fixture sets its own
`ETUDE_HOME` or calls `client._open(path)` without `upgrade(path,
development=True)`. Conftest initializes only its autouse home. `etude smoke`
initializes its own folder.

When a pinned `Counts(database=N)` moves, compare statements with main. Use a
temporary trace in `tests/launch/counting.py` to record normalized statements
in a file named by an environment variable, then remove the trace. Fenced
writes add `BEGIN IMMEDIATE`, `PRAGMA user_version` and `COMMIT`.
