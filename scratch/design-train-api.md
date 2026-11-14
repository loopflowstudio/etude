# The Etude train API and Manabot's loops: design for EXP-56 and MTG-329

State publication, 2026-11-13: a multi-file save cannot keep its whole-save
recovery promise through the retained mutable out carrier
(`scratch/questions.md`; probe at `6f70baa:scratch/state-publication.py`).
Direction given the same day: "snapshot directory + pointer published last,
recovery follows the pointer", and on cost, Jack Heart: "local storage
probably fine, but ideally avoid remote extra storage". Then: saves are
self-contained ("1b is fine"), the launcher cleans up where a worker cannot
("2a is the best of thse options"). The layout below and `docs/training.md`
show it; `scratch/questions.md` has his words and what is unanswered.

Accepted train API design, 2026-11-13. The full request and exploration
quotes are at `9a762a1:scratch/design-train-api.md` and
`1666be1:scratch/positioning.md`. Build on PR #103's carriage, retries,
continuation and verified inputs. Current implementation and remaining work
are in `exp-56.md`.

## Where this is going

Three layers, in Jack Heart's words: "Lab That Remembers is etude cloude";
"League is, I think, closer to the face of [the Etude SDK] but what we're
trying to build now is mostly focused on the train API, which is the building
block for the League API".

- **Etude Cloud** (later): questions, findings, agents assigned to experiments.
- **Etude SDK, the face**: the league. A checkpoint is a player; an evaluation
  is matches between players. Front line: "Which checkpoint is better?"
- **Etude SDK, now**: the train API. Its job is to let any loop run unattended
  and publish checkpoints the league can seat.

## Principles

Each is something Jack Heart said on 2026-11-13; the design is held to them.

1. **The loop is the author's.** "move as much execution management outside of
   Manabot and into Etude as possible so that Loop code can be really focused
   on algorithms and parameters". Etude is a library: "we're not behind a
   closed API... You can just call our code". No Trainer: "I don't want to do
   that... not in [Etude]".
2. **A call is API only when something else in Etude reads what it writes.**
   "writing this kind of code is relatively cheap... where it starts to add
   value is just when its a shared and interplays with other parts of hte
   system... the API (and the positioning) should be designed with that in
   mind". Anything else is a snippet in the docs.
3. **You own the files.** "it's saved to your machine, it's in normal-ass
   formats that you can read, and you own it. We do not own it... Using our
   framework to write your trainer does not lock you into us". Etude publishes
   "directories, checkpoints, and JSON files".
4. **Resuming is automatic.** "why would you not be able to resume a run. that
   should be something we are saying we handle automatically".
5. **One stage, one model is the heavy default; the rest generalizes.** "the
   heavy heavy default should be 1 stage 1 model, and the API should accept
   that but generalize where needed". Jace's kind of loop must stay possible:
   "I don't want to go so far as to rule out innovative learning patterns".
6. **Pydantic is the config protocol.** "i am fine with using pydantic as basic
   a dependency / native protocol to etude". The docs must say it plainly.
7. **Jobs have no seed; runs do.** "we also have names for run (is seeded) and
   job (is not seeded)".
8. **New and resumed are different commands.** Nothing is inferred from a
   directory that happens to exist. People may choose run ids.
9. **Compatibility between attempts is the author's.** "if you change the code
   within attempts at a run, you get undefined behavior. seems ifne".
10. **Written for agents as much as people.** "100% this". One file, typed
    refusals, spending caps, commands safe to repeat.
11. **An SDK experiment is a label.** "a formal definition of an experiment
    ... sits with Etude Cloud. I think an Etude SDK experiment is just kind of
    a nice name for a label".

## Names

From the ontology review. Jack Heart, 2026-11-13,
answering its questions: "1a -- i think RunAttempt is probably better than
Attempt though", "2a", "3a", "4a, or alternatively use cohort instead of
experiment as a more general word", "5a", "6b". All of the renaming is in
EXP-56, before the release.

The sentence the nouns make:

> An experiment is a label over jobs. A job with a seed is a run. A run
> executes as run attempts on machines. While it executes it writes rows on
> its clocks, keeps state so it can carry on, and publishes checkpoints. A
> checkpoint with code that plays it is a player. Players take seats in
> matches in an environment. A report shows rows and match scores.

Built names and predecessors are in `exp-56.md` (Delete — do not maintain).
The full naming table is retained at `ef6099a:scratch/design-train-api.md`.

- Report "line" becomes job. Cohort stays a launch option for shared limits
  and is not taught as a concept.
- The two group words, each with one meaning (Jack Heart, 2026-11-13, on
  this pair: "fine for now"). **Experiment**: the label on runs, taken from
  the file; no limits, nothing formal. **Cohort**: a named envelope of limits
  (`parallel`, `max_usd`) that runs are admitted under. A later launch adding
  to an existing cohort is EXP-61, not this work.
- One stored `RunRecord` describes a run. `RunAttempt` is one machine
  allocation and owns its `writers`, the tracking/eval contexts with separate
  first-row cutoffs. Writers do not count against allocation limits or rental
  accounting. `RunData.writers` reads a run's contexts; its historical
  `attempts` accessor warns until 0.5. The job digest stays identity, while
  the report's config-disambiguated job label is derived when reading.
- Runs and evaluations already stored stay readable: readers accept the old
  file name (`state.json` as an outcome) and old field spellings. Nothing
  stored is rewritten. Renamed Python names keep an alias that warns and
  names its removal release.
- Not renamed here: `Record` and `Storage` (each names two classes) and
  `Session`. The review lists them; no answer was given.

## The train API

### What the author writes

The default loop is the opening example in `docs/training.md`: register
objects with `resume`, iterate `steps`, log, save and publish checkpoints.
It has no state class, stop check or resume branch.

Requirements, stated in the docs: the config is a Pydantic model with no Etude
base class and no required field; it survives a trip through JSON, checked at
launch and refused there; `train` takes that one annotated parameter and sits
at the top level of an importable file.

`train(config)` is one attempt at the whole job. It returns (finished), raises
`Stopped` (saved on purpose; call again), or raises anything else (failed).

### The calls

The surviving public calls are documented in `docs/training.md` (Calls).
The original implementation table is at `ef6099a:scratch/design-train-api.md`.

Details that matter:

- **`run.steps(clock, to=)`** starts at the position `run.resume` restored
  (zero for a fresh run) plus one. Its target is the launch's (`--until`,
  a continuation) or else `to`. It must raise `Stopped`, not end quietly:
  code after the loop runs only when the target was reached. Otherwise a
  phased recipe runs its next phase on a half-trained model.
- **`run.resume`** takes anything with `state_dict()` / `load_state_dict()`.
  `extra=` is an optional Pydantic model for the author's own numbers
  (Manabot's cumulative counts), loaded and returned. A `name=` argument
  separates the state of two phases in one run. For state not shaped like
  this, `run.resume()` returns the last saved folder and `run.save(fn)` hands
  `fn` an empty one.
- **`run.save()`** is called every pass. Etude writes on the last pass before
  a stop and otherwise on an interval (default by wall time, overridable with
  `save_every=` in steps). The write replaces the previous state whole or not at
  all: a new self-contained save folder, then the pointer naming it.
  Recovery and `run.resume` follow the pointer. Delivery sends only the save
  the pointer names, whole, then the pointer; Etude removes replaced saves
  (the courier, or the controller under upload-only grants).
- **`run.checkpoint(policy=model)`** writes named objects the same way state
  is written, so "a lot of the exports... look the same". One call is one
  checkpoint and one player, however many models it holds. A single path
  still works, for projects whose checkpoint file has its own format
  (Manabot's world-bound policy file).
- **Carrying on restores what was registered.** Random streams and anything
  in flight are restored only if they are in a registered object. The docs
  say so.
- **Etude checks config and seed between attempts and names the fields that
  differ.** It does not check model shape, file format or environment.
  Source may change between attempts of a run; each attempt records its
  commit.

### What is on disk

The layout contract is in `docs/training.md` (What is on disk):
self-contained immutable saves, selected by `current.json`, published last;
checkpoint folders hold their manifest and named framework files. A folder
checkpoint is digested by a manifest of its files' digests. PyTorch writers
are imported only when used; MLX/JAX wait for an adoption need.

### Jobs, runs, names, and the commands

A **job** is the function and its config, with no seed. A **run** is a job
with a seed: the thing launched, named, resumed and reported. The seed is not
a config field.

Names (Jack Heart, 2026-11-13: "maybe Run and etude.run want ot change", then
"ok, RunRequest works"): `Run` is the object a loop holds inside `train`. What
a job file declares, a job and a seed to run it with, is a `RunRequest`
(today's `etude.launch.records.Run`). The internal package `etude.run`
(declarations, delivery, storage) is renamed so it is not a second `run`
module. It was to become `etude.tracking` (Jack Heart: "or etude.track?" then
"etude.tracking also fine iether way"). His later layout supersedes that:
"run should be top level not in training", with `TrainingRun` and `EvalRun`
as subclasses (`questions.md`, Package layout). Built that way.

The author never has to name `RunRequest` (Jack Heart: "still in favor of
not needing to know its called a RunRquest"). A job file returns jobs, and
seeds are one of the ways a job is launched, beside its name, hours and
dollars; they are not part of what the job is.

```python
# job.py
def runs() -> Job:
    return training(
        train,
        config=TrainConfig(),
        smoke=TrainConfig(updates=1),
        seeds=(0, 1, 2),
        max_hours=2,
        max_usd=Decimal(4),
    )
```

One job with three seeds is three runs. Without `seeds`, one run with seed 0.
Addresses and command selection follow "Run addresses" below. `RunRequest`
remains available for explicit ids and admission-time repetitions; local
training of an explicit request needs an id. New and resumed attempts are
separate calls. A refusal never changes the stored outcome.

### Beyond one stage, one model

Each rung adds one thing and changes nothing below it. No "stage" noun.

| | Case | Adds | When |
| --- | --- | --- | --- |
| 1 | one stage, one model | nothing | this work |
| 2 | several models in a run (Jace) | more names in `run.resume(...)` and `run.checkpoint(...)` | this work |
| 3 | phases in one run (Manabot belief) | `name=` on resume and save | this work |
| 4 | stages as separate runs (AlphaGo: imitation, then self-play from it) | `training(..., start=<checkpoint of another run>)`, `run.input("start")` | this work, over the built `Input` |
| 5 | wanted runs computed from what exists: `runs(state)`; runs reading each other while training | | EXP-60 |

Rung 4's rule of thumb for the docs: split into two runs when stages want
different machines or budgets, when several runs start from one, or when the
earlier result is evaluated on its own.

### What evaluation must accept (the join to the league)

"Model" is the author's word for a network. Etude's evaluation noun for a
checkpoint and the code that plays it (today's `Model`) becomes **player**;
code that plays with no checkpoint (today's `Code`) is also a player. Jack
Heart, 2026-11-13, on this proposal: "works for me". Stored evaluation records
keep their spelling; the rename is of the names people write and read.

- A checkpoint that is a folder: the playing code is handed the folder and
  `checkpoint.json`, and loads the files it needs. One checkpoint can be
  seated by two different pieces of playing code.
- Relative seats keep resolving against declared clocks.
- `run.checkpoints()` and evaluation read the same records.

Built: playing code reads `CheckpointPlayer.checkpoint.path`, whose folder
contains `checkpoint.json` and the named files (`etude.checkpoints.CheckpointRef.path`).
`tests/train/test_checkpoints.py::test_two_models_publish_one_checkpoint_and_read_the_same_record`
seats both models, joins the evaluation to its training checkpoint and verifies
the `start=` inputs. `tests/eval/games.py::play_checkpoint_folder` reads the manifest.

### Set up for Etude Cloud without building it

Every fact the Lab will want is already a file in the author's bucket, which a
hosted reader can read without being in the write path (STU-14): runs with
their job, seed and config; attempts with commit, machine and cost; the edges
between runs (`continue`, `start=`); checkpoints with clocks; evaluations.
The SDK adds no experiment record: an experiment stays the label those files
share.

The experiment's name comes from outside the training code. Jack Heart,
2026-11-13: "in the future Cloud will have names for experiments and it will
supply the experiment name to the [Etude] SDK launcher". So the launcher is
the authority for a run's experiment: a name given at launch wins, the file's
stem is only the default when none is given, and training code never needs to
name its experiment. Launch commands take the name as an argument.

## Built cuts and review history

Earlier review directions and receipts remain at
`312928d:scratch/design-train-api.md`; current behavior and verification are in
user docs and `exp-56.md`. Schemas stay at launcher 19 and statsdb 14, the
single unreleased changes over main.

## Run addresses (Hanabi demo steers, 2026-11-13)

Words and the superseded `--run`/`--job`-naming steers are in demo-hanabi.md.
`etude train FILE` (was `etude run`) is built. The latest steers stand:

- Jack Heart: "ii think --experiment to override the overall experiment name,
  but still enforicng hte -job-seed [but allowed to skip those if there is
  only one of either]", then "experiment/job/seed i would say?".
- Then: "maybe we want a differnet divider for job and seed",
  "experiment/job:seed?", "i mean @ or :, whatever you think". The operator
  chose `@`: a run's address is `experiment/job@seed`. Omit the job level
  when the experiment defines one job, the seed level when the job has one
  seed: `hanabi`, `sweep@1`, `sweep/wide@1`. One spelling everywhere:
  commands, output, file and folder names and bucket keys.
- The command line names only the experiment (`--experiment`, default the
  file stem). `--job` and `--seed` select what the file defines in Python.
  No `--run` or `--name` flag. `--resume` takes no name: it resumes the run
  the selection addresses.
- Operator's choice, not Jack's: a changed `--config` under an existing
  experiment and job is refused naming the changed fields and suggesting
  `--experiment`.
- Same addressing in train, launch, continue, eval, status, logs. Historical
  runs stay readable under their stored names.

Implementation, settled headlessly from the recorded choices and built at
`44aa394`/`6f358b3`:

- Expand the full job/seed declarations before selection, in `etude.spec`;
  file and Python clients share it. Explicit `RunRequest` ids are preserved.
- `etude train` selects `runs()` jobs and calls their typed functions here.
  With only `train(config)`, the file defines one job and seed 0. Selected
  seeds run in order; a saved stop leaves later seeds unstarted. Local resume
  covers the selection; launch resume requires one selected run.
- `run.json` is still the sole stored identity. New ids, folders, prefixes and
  writer addresses use the compact spelling. The worker supplies a full
  address beside the local run name. Historical records keep their stored ids
  when read or delivered; no rewrite, schema change or extra remote read.
- A compact training root may contain sibling eval roots. Discovery traverses
  those while excluding the parent's own `out`, artifacts and metric data.
- Remove `--name`, resume's run-name argument, `NAME-seed-N` generation and
  the file reader's seed expansion. Preserve explicit historical ids, row
  replacement, state publication, continuation links and budgets.
- Taken-name refusals show `--resume` and `--resume --until CLOCK=TARGET`.
  Changed local config names fields and points to `--experiment`.

## Loop ergonomics (Hanabi steer, 2026-11-13)

Exact words: demo-hanabi.md and steer
`23932ad5-e4c7-43cc-b086-96c4f5bd0bcb`. Jack Heart: "1a",
"2a - but i still dont exactly undersatnd why/when we need to add the wraper",
"3a". `steps(clock, to=, save_every=, save_every_seconds=)` declares a missing
step clock with its name as label. Later steer `7df6a146-0f10-4a94-921b-2a6b64dee635` supersedes extra-axis
wrappers: each log keyword declares a count clock, labelled by its name.
`track(clocks=...)` is optional; Time stays for custom seconds. Step is only
a released warning alias through 0.5. Historical declarations stay readable. Rows before any clock
raise with this remedy, even in non-strict mode. Later rows missing a used
clock raise in strict mode or warn and count incomplete rows otherwise. Default cadence is 300 seconds;
step cadence replaces that default; explicit seconds plus steps saves at either
threshold. Stops and completion always save. `should_stop` is a property on
training and eval runs. Delete the unreleased `every=` and `stopping()`
spellings; no aliases. Publish added clocks before their rows and seal the
writer's prior columns before switching, preserving already queued rows.
Resume retains stored clocks even when the recipe omits the primary clock.
Steer `6cf6a4f2-1c7c-4b48-8beb-f82b005f1dd8`: optional
`track(clocks=["updates", ...])` closes the set; omitted, declarations are by
use. Unknown closed-set keywords refuse their row (strict raises, otherwise
warn once and drop); steps on an unknown closed-set axis raise. Every row
requires all clocks in use; no sparse switch. A closed resumed set must match
the stored kinds and names; stored labels survive.
Steer `cf6392bc-72d6-4aee-a020-909704a9bcb2`: metrics may vary per row;
missing means null, never zero or carried forward. Evals are separate runs.

## Selection and review decisions

Ninth review corrections and the selection recovery probe are built; full
rationale remains at `9799f9d:scratch/design-train-api.md` (Ninth concept review
and Selection recovery decision). Current behavior is in user docs;
`exp-56.md` lists the cut and `questions.md` preserves the probe receipt.

Preflight every selected run before executing any. Multi-run resume skips
finished members without a later target; a sole finished member refuses.
Never-started members refuse under resume (Open 6). Config conflicts name
fields and both remedies: resume with --until, or a new --experiment (Open 7).
Local training needs no rental hours or git; record a commit when available.
Checkpoint output and report --experiment follow the compact address.
JSON emits one object per selected run, in order. Preserve exit codes,
identity refusals, row replacement, state publication and cumulative budgets.

Open 6's alternative is resume meaning finish the whole selection: resume
started members, skip finished ones, start unstarted ones, with the same
whole-selection preflight. A later conflict must leave all members untouched;
I/O failure cannot mean an absent run. No new sweep record or layout follows.
Open 7's alternative groups quick config variants as jobs in one experiment;
that needs a naming interaction and must not reinterpret --job selection.
Neither alternative is chosen. Until answered, recovery uses explicit seed
commands and variants use new experiments. The QA walkthrough now uses
etude.train.tracking and etude train FILE [--resume].

## Manabot (MTG-329)

Separate Task; do not edit its checkout here. The full loop sketch, recipe
cutover and adoption checks are preserved at
`9a762a1:scratch/design-train-api.md` (Manabot). Keep these constraints:

- Replace its Tracker/stub, untracked loop and hand-written resume/save/stop
  branches with `Run`, `resume`, `steps`, `save` and `checkpoint`.
- Register learner, optimizer, EMA and cumulative counts. Preserve the
  world-bound policy format and publish it through the path checkpoint form.
- Remove seed from configs; jobs supply seeds. Split belief policy and sampler
  into jobs joined by `start=`. Retire `manabot train --out` for `etude train`.
- Existing Manabot `state.pt` folders have no new loader; document the last
  revision that reads them. Frozen experiment evidence stays untouched.
- Verify tiny fresh/stop/resume, seed-197 parity and a capped launched smoke
  in MTG-329. The tiny resumable PPO loop remains the README candidate.

## Jace adoption

What Jace needs is exactly rungs 2 and 4: `run.resume` over five models and
six optimizers, one checkpoint holding all its models, `run.checkpoints()` for
past versions at the table, and `run.input` for its human-trained drafter. Its
`limited_iteration` is unchanged; its caller becomes a `run.steps` loop
(Several models in `docs/training.md`); its `tracking.py` is deleted. The same
checkpoint seats as a limited player and as a constructed player. Etude's
docs should carry a multi-model example of this shape so the path is visible
without reading Manabot.

## Delivery order

EXP-56's implementation and docs are built; `exp-56.md` separates focused
evidence from the remaining gate acceptance below. Coin is the small example;
Hanabi supplied the hands-on demo (`demo-hanabi.md`). MTG-329 owns Manabot's
cutover and smoke; EXP-12 follows EXP-56 for database migration. The later
league-led README sketch is at `1666be1:scratch/etude-readme-draft.md`;
its recipe surface is superseded.

## Acceptance for EXP-56

- The default example above runs with `etude train`, is interrupted, resumes
  with `--resume`, and reads back as one run.
- A two-model example saves, resumes and publishes one checkpoint that an
  evaluation seats.
- A taken name is refused. A changed config or seed is refused naming fields.
  Changed source resumes and the attempt records its commit.
- A run started from another run's checkpoint records that link.
- A stop between phases never lets the later phase run.
- A finished run is never marked failed by a later refused attempt.

## Not in this work

A Trainer or callback API; a W&B-shaped module; the live view (EXP-57);
machine metrics (EXP-58); JSON on existing commands (EXP-59); pool seats,
population reads and ratings (EXP-60); a spending limit across launches
(EXP-61); anything hosted.

## Open

1. The name `track` stands. The concern about two classes named `Run` was
   resolved with `RunRequest`; Jack Heart did not explicitly choose the name
   `track`.
2. Jack Heart chose "Both": `--resume --until ...` extends the same run,
   including a finished run; `continue` creates a child when config changes.
   The same config and seed identify the same run.
3. Jack Heart chose "Show how to keep them" for games in flight. The docs
   show an object holding pending games passed to `run.resume`.
4. The public non-Magic example remains unanswered. Use a tiny game shipped
   with Etude, also for the two-model acceptance example.
5. Save cadence: answered "1a" (`save_every=`/`save_every_seconds=` on
   `run.steps`); built as described under Loop ergonomics.
6. Unanswered. After `etude train sweep.py` is stopped partway (seed 0
   finished, seed 1 saved, seeds 2-9 never started), should
   `etude train sweep.py --resume` also start the never-started runs? Principle
   8 says new and resumed are different commands, which leaves no single
   command that finishes the selection: `--seed` takes one seed, so the rest
   are eight commands. Assumed until answered: refuse them by name before any
   work. The alternative reads `--resume` over a selection as "bring every
   selected run to its target".
7. Unanswered. Is a quick config variant a new experiment or a new job in the
   same experiment? The operator's choice (Run addresses) sends a changed
   `--config` to `--experiment NAME`, so each variant is its own experiment and
   its own report page; nothing draws `wide` beside `coin`. Jack Heart called
   `--experiment` the "overall experiment name". The alternative puts variants
   in one experiment as jobs, which needs a way to name a job from the command
   line, where he said `--job` selects.
