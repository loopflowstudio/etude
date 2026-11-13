# EXP-56: choices and direction

## Decision (2026-11-12)

Jack Heart: "5b. no opinion on the other stuff". This addressed choice 7 below
(presented to him as 5): the launch tells the recipe its target, and
`etude continue --until updates=6400` alone continues without changing config.
Choices 1–4 and 6 retain their built recommendations under "no opinion".

## Choices for demo

1. **Placement:** launched out is `RUN/out/`, carried with its run grant.
   `local_launch(out=DIR)` stores records at `DIR/etude/BASENAME(DIR)/`, matching
   the manabot stub. Alternative: local records in `~/.etude/track`.
   Making out itself the record folder would collide with recipe-owned
   `checkpoints/` and `state.json`. Every `track` in one local block shares
   the run; `etude report --source DIR` lists out files too.
2. **Checkpoint references:** files inside out are recorded in place, with
   digest, without a second copy; bytes must not change. Other local files
   are copied. Alternative: always copy, paying a second upload.
3. **Planned stop:** raise `etude.train.run.Stopped` after saving;
   `etude.call` exits 75, also available to command jobs; outcome `stopped`.
   Precedent: sysexits' `EX_TEMPFAIL` (temporary failure, retry), Slurm
   `RequeueExit`, Kubernetes `podFailurePolicy` on exit codes. Inferring this
   from a true `stopping()` would mislabel a crash while saving.
4. **Local entry:** `local_launch(out=, experiment=)` preserves `(config)`
   recipes. Alternative: `track(out=)` requires directory handling in each
   recipe. Naming remains open: `etude launch job.py` on pool `local` already
   means a local launch through a worker.
5. **Config:** CLI takes a typed JSON object (`--config '{"rate":0.001}'`),
   rejecting unknown fields/invalid values before queueing. Python takes a
   validated model. This replaces `FIELD=VALUE` to follow `AGENTS.md`;
   existing `--until` remains a separate target.
6. **Cancellation:** retain TERM alongside the stop file; saving requires
   handling TERM. Alternative: allow polling for `stop_seconds` before TERM.
7. **Target:** `run.until("updates", default=config.updates)`. This supersedes
   the Task wording "it declares a new run whose update target is the only
   changed config field". At `bd4a687`, inspection found `--until` alone could
   return at the old config target; no run was performed for that finding.

8. **A planned stop at the run's own hours limit** (open; found 2026-11-12 at
   `05b480f`, probe in `exp-56.md`). `ETUDE_DEADLINE` is the earlier of the
   machine's window and the run's remaining hours, so a recipe also saves and
   raises `Stopped` just short of the run's own limit. The launcher cannot tell
   that from a stop for a machine window: it rents again for the leftover
   reserve (40 s in the probe) and that attempt ends `out_of_time`. Built
   behaviour kept as the headless assumption: bounded by `max_attempts` and
   dollars, now documented in `docs/launcher.md` and pinned by a two-rental
   counting regression. Repeated early stops can instead exhaust attempts;
   `out_of_time` is not guaranteed for every retry. Alternatives: end the run
   with its limit kind when the run's own limit bound the stopped attempt
   (then a job exiting 75 for its own reasons on an owned host never retries,
   and reads `out_of_time` with hours left); or have the job say why it stopped
   (a second signal for recipes to learn).

9. **A script that names its experiment, launched from a plain job file** (open;
   found 2026-11-12 at `6fb164b` by source inspection; confirmed by a local-pool run in QA, `qa.html` section 7). On main a
   plain `job.py` left `ETUDE_EXPERIMENT` unset, so main's README example
   (`etude launch job.py` with `track(experiment="capacity-pilot", ...)`) ran
   and recorded under `capacity-pilot`. Since `b6482b7` the job gets experiment
   `job`, and `track` raises `experiment_differs_from_launch` in that script:
   on the worker, after any rental has started. `etude-sdk` 0.2.16 is published
   with that README. The repair also gives every unnamed plain-file job a new
   job digest, since the experiment is part of its identity. Built behaviour
   kept as the headless assumption: the launched file owns the experiment; a
   script drops `experiment=` or the job sets `Job.experiment`. One rule, and
   the launcher and tracking never disagree about a run's experiment.
   Alternative: the stem is only a fallback, so a script's own name wins over
   a defaulted one (main's behaviour kept; needs the worker to know defaulted
   from explicit, and the launcher records `job` while tracking records
   `capacity-pilot`). Whichever holds, the release note must say it; if the
   built rule holds, the old README example is a breaking change to call out.

## Headless assumptions

Plain training-file launches (QA finding 2): default an unnamed job's
experiment to its file stem, following the existing experiment-file naming
rule. Preserve explicit `Job.experiment` values in plain files; keep the
conflict check for files defining `eval`/`report`. Recipes name no experiment.
This does not change report discovery or Python launches without a job file.

Keep 1–4 and 6 for demo. `Run.until` follows `Job.until`/`--until` vocabulary,
accepts fractional clocks, and uses the required default for a missing or
different-clock launch target. Undeclared requested clocks raise; integer-only
loops convert to `int`. No config-field inference or extra target record.
Without a named directory `run.out` raises `out_not_named`; with no
stop file/deadline `stopping()` is false. First rows set new attempt cutoffs;
historical cutoffs survive.

Headless concept reviews, 2026-11-12: copied latest checkpoints accompany
empty or populated out; continuation refuses when neither can seed the child;
a removed latest out checkpoint never selects an older one implicitly. Docs
and errors use "out". These preserve existing behavior and naming, without
changing the API choices above. Repairs, probes and checks are in `exp-56.md`.
`local_launch` naming remains choice 4.

Restoration is per file as last carried: write atomically and keep one resume's
needs in one file. Parent mutation after queueing fails input verification.
Membership follows data-before-index delivery, without deleting retained
checkpoint objects. Private/unfinished files stay local. The earlier unlink
failure and checks are in `exp-56.md`; repairing it did not decide API choices.

Out membership implementation: preserve recovery's existing tolerance for a
mutable save newer than its last membership. Continuation uses the published
fingerprint and still verifies inputs at delivery. Sync holds temporary hard
links during a pass, following the existing atomic-save requirement, to keep
the fingerprint and upload on one saved inode without duplicating bytes.

`Stopped` is defined in `etude/stopping.py` so `etude.call` exits 75 without
loading tracking, and re-exported from `etude.train.run`. Docs and tests import
it from `etude.train.run`, as the Task's acceptance check words it ("replacing
`manabot/recipes/proposed_etude.py` imports by `etude.train.run`"). `AGENTS.md`
asks for imports from the defining module; the Task wording is kept. Cosmetic,
reversible: switching is three import lines and the cutover note below.

## Manabot cutover (MTG-329; do not edit here)

- Import `track`, `local_launch`, `Stopped` from `etude.train.run`, and `Step`
  from `etude.train.records`; delete `manabot/recipes/proposed_etude.py`.
- Preserve the stub's `track(strict=True)`; Etude defaults to `strict=False`.
- Pass `updates=int(run.until("updates", default=config.updates))` to the loop.
- Make `training/loops/self_play.py:Stopped` inherit Etude's, preserving its
  `update` and message. `cli.py` catches that class; typed jobs call recipes
  directly.

Open for demo: when a belief continuation extends its policy, should it
recollect histories and refit the sampler, or deliberately keep both from the
parent? At `f47e2621d`, `belief_sampler.py:belief` passes `config.updates` to
both self-play and the sampler checkpoint; `collect_belief` and `train_belief`
return existing outputs without checking the policy. Merely changing the loop
target leaves those outputs tied to the old policy. The sampler's checkpoint
coordinate must describe the policy it actually used, not blindly the config
or launch target. If recomputing within one run, use an immutable checkpoint
path: Etude rejects changing bytes at a previously recorded address. This is
a source inspection finding, not an executed manabot acceptance check.

Affected user docs: `docs/tracking.md`, `docs/launcher.md`, `README.md`.

## Train API docs (2026-11-13): open choices and assumptions

Written while drafting `docs/training.md` from `design-train-api.md`. Nothing
here was decided by a person; each is the spelling the docs currently show.
The design's own Open list (name `track`, `continue` as child run, games in
flight, the public example, save cadence) is unchanged and not repeated.

Open product choices the design does not spell:

1. **Class names for players.** The design renames the noun (`Model` →
   player; Jack Heart: "works for me") but gives no class name, and
   `etude.eval.records.Player` is already the `Model | Code` union. Docs avoid
   naming the class and write `Inference(...).player(ref)` and
   `checkpoint.player(inference)`. Question: what is today's `Model` class
   called (`CheckpointPlayer`? `Player` with the union renamed?), and is
   `Code` kept?
2. **The one checkpoint reference type.** Design: "`CheckpointRecord`, plus
   one reference type" replacing `Checkpoint`, `CheckpointKey`,
   `CheckpointRef`. No name given. Docs keep `CheckpointRef`/`CheckpointKey`
   spellings in `docs/evaluation.md` untouched.
3. **How playing code receives a folder checkpoint.** Docs take the smallest
   extension: `player.checkpoint.artifact.uri` names the folder, which holds
   `checkpoint.json`. Alternative: a `path` accessor on the reference.
4. **`etude launch job.py --resume RUN` limits.** Docs say the job's limits as
   the file now states them bound the whole run, earlier attempts included, so
   raising them in the file is how a run that ran out gets more. Alternative:
   limits fixed at first launch, with flags to add.

Assumptions taken (reversible, simpler option):

- Run id is `NAME-seed-N` (`baseline-seed-7`, as the design writes it; the
  launcher's `RunRecord.name` writes `seed7` today). An unnamed job is named
  for its file stem: `etude run train.py` starts `train-seed-0`. `--name`
  names the job part; the id still carries the seed.
- `etude run FILE` calls the function named `train` in FILE, with its config
  type's defaults; `--config` is a JSON object of changed fields. `--seconds N`
  is a deadline: the run stops saved and exits 75.
- Storage layout is `PREFIX/EXPERIMENT/RUN/` (design: `<experiment>/<run>/`,
  ids unique within an experiment). Launcher docs changed to match; older flat
  `PREFIX/RUN/` folders stay readable. This moves launcher keys and grants.
- Checkpoints from named objects live in `out/checkpoints/CLOCK-VALUE/`
  (eight-digit zero padding, as the design shows). A single file from outside
  `out` is still copied to `RUN/checkpoints/`. A second different checkpoint
  at one position is `CLOCK-VALUE.ordinal-N/`.
- `run.save()` is due five minutes after the last save (design: wall time, no
  number); `run.steps(..., every=25)` switches to a count.
- A checkpoint outside or after `run.steps` is placed at the stepped clock's
  current value and the latest value given for every other clock.
- `run.resume(extra=model)` returns that same object, loaded; Etude saves it
  as it stands, so authors mutate it in place.
- `run.checkpoints()` items expose `at` and `path` (folder or file here).
- `training(..., start=CHECKPOINT)` takes a checkpoint from
  `observe(source, experiment).run(RUN).final`; the loop reads
  `run.input("start")`. `seeds` is a launch option on `Job`, outside job
  identity; `runs()` returns `Job` or `list[Job]`; `job.runs(...)` and
  `RunRequest` remain for hand-built launches.
- Deprecated spellings (`track(seed=)`, `local_launch`,
  `run.latest_checkpoint()`) warn for one release. State files an older loop
  kept in `run.out` are not read by `run.resume`.

Not documented, because nothing user-facing is settled: EXP-37's remaining
points (acquisition mode and loss causes in records, acknowledged checkpoint
durability, fault-injection fixtures). The docs do state its "application
declares its restorable state and any discontinuity" point: only registered
objects are restored, and streams or games in flight start fresh.
