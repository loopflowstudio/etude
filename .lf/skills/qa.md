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
   played before the signal are sealed; the next pass plays only the rest. A matchup's
   name is its identity (`matchup_conflict` if its conditions change): add new matchups
   under the same tag instead. Replay already-played matches on another setup with
   `etude.eval.match.evaluate(experiment=..., matches=..., run="replay-x")`, taking the
   benchmark from `Evaluation.model_validate_json(job.config)` for each job
   `etude.experiment.asked(read_experiment(file).eval, observe(RUNS, NAME))` returns.
   Inspect provenance with `etude.eval.query.statsdb(NAME, RUNS)` (`matches.execution`).
   A page's own numbers are in `JSON.parse(document.getElementById("data").textContent)`
   (`cells` with `selection`, `subject`, `matches`, `rows`).
5. Stop the servers before finishing. In zsh, `$args` does not word-split: call
   commands explicitly rather than looping over argument strings.
