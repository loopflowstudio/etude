# The Etude train API and Manabot's loops: design for EXP-56 and MTG-329

State publication, 2026-11-13: a multi-file save cannot keep its whole-save
recovery promise through the retained mutable out carrier
(`scratch/questions.md`, reproduced by `scratch/state-publication.py`).
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
- One stored `RunRecord` describes a run. `RunAttempt` describes a machine
  allocation; `RunWriter` describes a tracking context linked to an optional
  allocation. This correction follows the reproduced many-writers-per-allocation
  case in `questions.md`: merging them would lose row-replacement boundaries
  or inflate retry counts and cost. It is an implementation design correction,
  not a new decision attributed to a person, and it departs from the "1a"
  answer above; the choice is open in `questions.md`. The job digest stays an identity;
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
  `every=` in steps). The write replaces the previous state whole or not at
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
    return training(train, config=TrainConfig(), smoke=TrainConfig(updates=1),
                    seeds=(0, 1, 2), max_hours=2, max_usd=Decimal(4))
```

One job with three seeds is three runs. Without `seeds`, one run with seed 0.
A run's id is the job's name and its seed (`baseline-seed-7`) unless given.
`RunRequest` remains the record underneath for code that builds launches by
hand.

| Intent | Command | Result |
| --- | --- | --- |
| a new run, here | `etude run train.py [--config JSON] [--seed N] [--name NAME] [--until clock=value] [--seconds N]` | always new; prints the id |
| a new run, on machines | `etude launch job.py [--rent ...]` | always new |
| the same run, after a lost machine or `Stopped` | none; the launcher calls again | a new attempt of the same run |
| the same run, after it ended unfinished | `etude run train.py --resume RUN`, `etude launch job.py --resume RUN` | a new attempt of the same run, on current code |
| the same run, further | `--resume RUN --until ...` on either | a new attempt of the same run, finished or not (Open 2) |
| changed, or a branch of a launched run | `etude continue --run RUN --until clock=value [--config ...]` | a new run descended from the first |

- `--name` chooses the job name; the run id adds its seed (`wide-seed-7`).
  An exact run id without that suffix is chosen with `RunRequest(id=...)`.
  It is unique within its experiment. The authority is the run's folder:
  `run.json` is created create-only, so a second claimant fails on any
  computer. The launcher also refuses early, before renting.
- A taken name is refused, never resumed. This changes `declare`, which today
  resumes a re-used name.
- `--resume` refuses a changed config or seed, and a finished run given no
  later target.
- `etude run` replaces `local_launch` in user code and Manabot's
  `train --out`. Exit codes: 0 finished, 75 stopped, other failed.
- New commands print JSON with `--json` from the start (EXP-59 covers the
  rest).
- Bug already assigned to EXP-56: a refused or interrupted attempt must not
  mark a finished run failed.

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

Not checked: how the playing code receives a checkpoint today beyond "one
file as an input". EXP-56 must read that and extend it.

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

## Record corrections (concept review, 2026-11-13): built

1. **`run.json` holds only declared facts.** `RunRecord` has an optional
   `RunSubmission` for specification and scheduling facts, absent from new
   standalone and worker declarations. The launcher database owns those facts;
   controller replies carry the submission. Historical nested declarations,
   default-filled files and flat launcher records still decode. Schema stays 19.
2. **A run has one name.** `RunRecord.name` is the id's run part, including
   its `-seed-N` spelling. Quotes use that same name when assigned; only
   hand-built requests with no id get a derived label. Unlabelled command
   jobs keep opaque references.
3. **Outcome and state are different words.** `RunOutcome` replaces
   `RunState`. The stored `outcome.json` field stays `state`; the new
   `etude run --json` command prints `outcome`.
4. **Imports name the defining module.** Training teaches `Stopped` from
   `etude.stopping`; tracking no longer exports it. Deprecated
   `etude.train.run` keeps its alias until 0.5.

Delete — do not maintain: the flat launcher fields on `RunRecord`, its
second derived run name, `RunState`, and tracking's public `Stopped` export.
Keep historical decoding, remote controller record replies, planned-stop
retries, row replacement, continuation and cumulative limits. Eval planner
changes remain blocked on the planning boundary in `questions.md`.

## Corrections from the second concept review (2026-11-13): built

Review evidence and the detailed direction remain at
`394aa37:scratch/design-train-api.md` (second concept review).

- The training docs distinguish launcher retries inside the run's limits
  from local `etude run`: exit 75, then explicit `--resume RUN`. No local
  retry loop, which would undo Ctrl-C or `--seconds`.
- `RunData.outcome` holds the `RunOutcome`; `state` is a warning property
  until 0.5. Internal readers use `outcome`. Stored `outcome.json` still
  has its `state` field; SQL and lifecycle summaries keep `state` too.
  Historical `state.json` outcomes remain readable. The tracking docs name
  evaluation's current `outcome.json` file.
- `--name` names the job: `--name wide --seed 7` starts `wide-seed-7`.
  An exact run id without the seed suffix uses `RunRequest(id=)`.

No schema, query or provider count changes.

## Corrections from the third concept review (2026-11-13): built

Probed with `examples/coin.py` in a temporary `ETUDE_HOME`; output in
`questions.md` (third pass).

1. **One run reference, here as under a launch.** `etude run --resume`
   accepts `EXPERIMENT/RUN` as `etude launch --resume` does, through the same
   `run_address`; a bare name is looked up in `--experiment`, else the file's
   stem. Today a run started with `--experiment lab` resumes only when
   `--experiment lab` is repeated: the bare id gives `run_not_found`, and
   `lab/wide-seed-7` gives `unsafe_run_or_experiment_name`. `run_not_found`
   names the experiment it looked in. The `run_name_taken` hint prints a
   command that works (`--resume lab/wide-seed-7`), not the bare id. The
   printed id and the `--json` fields stay as they are. Training docs
   (Running it): `--resume` takes the run's id in this file's experiment, or
   `EXPERIMENT/RUN`. One focused CLI test: start under another experiment,
   resume by the qualified reference and by `--experiment`, refuse the bare id
   naming the experiment searched.
2. **The docs teach a branch command that does not run.** `etude continue
   --run RUN --config ...` fails with `Missing option '--until'`, and with
   `--until` it reads only the launcher database, so a run started by
   `etude run` gives `no run 'coin/coin-seed-0'`. Make the docs true to what
   is built, and add no command: the Running it row, the `etude continue`
   bullet and Counting say `etude continue --run RUN --until clock=value
   [--config ...]`, for a run the launcher started. `docs/launcher.md` (The
   run promise) spells it `etude continue RUN --experiment`; the option is
   `--run RUN`. Whether a run started here can be branched is open
   (`questions.md`).
3. **The config refusal the docs print is not the one Etude prints.** Built:
   `etude: run_identity_conflict: lr was 0.1, is 0.2`. The Training page shows
   a two-line message with the run's name and `etude continue` advice. Show
   the built line; put the advice in prose (a new `--name` here; `etude
   continue` for a launched run). Keep the code and the named fields.

Delete — do not maintain: nothing. No schema, query or provider count
changes; no new command or option.

## Manabot (MTG-329)

Separate Task; do not edit its checkout here. The full loop sketch, recipe
cutover and adoption checks are preserved at
`9a762a1:scratch/design-train-api.md` (Manabot). Keep these constraints:

- Replace its Tracker/stub, untracked loop and hand-written resume/save/stop
  branches with `Run`, `resume`, `steps`, `save` and `checkpoint`.
- Register learner, optimizer, EMA and cumulative counts. Preserve the
  world-bound policy format and publish it through the path checkpoint form.
- Remove seed from configs; jobs supply seeds. Split belief policy and sampler
  into jobs joined by `start=`. Retire `manabot train --out` for `etude run`.
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

## Order of work

1. **EXP-56**: the calls, the on-disk layout, PyTorch writer, `etude run`,
   `--resume`, names, config and seed checks, folder checkpoints through
   evaluation, `start=`; the Training page (`docs/training.md`, drafted); a
   tiny non-Magic example. Old spellings
   (`track(seed=)`, `local_launch`) warn and name their removal release.
2. **MTG-329**: rebuild on an isolated override of the unreleased SDK, then
   pin the release.
3. **EXP-12** (launcher database migration) starts as soon as EXP-56 is done.
4. After: README led by the league (`1666be1:scratch/etude-readme-draft.md`,
   its recipe surface superseded), EXP-60.

## Acceptance for EXP-56

- The default example above runs with `etude run`, is interrupted, resumes
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
2. Jack Heart chose "Both": `--resume RUN --until ...` extends the same run,
   including a finished run; `continue` creates a child when config changes.
   The same config and seed identify the same run.
3. Jack Heart chose "Show how to keep them" for games in flight. The docs
   show an object holding pending games passed to `run.resume`.
4. The public non-Magic example remains unanswered. Use a tiny game shipped
   with Etude, also for the two-model acceptance example.
5. Save cadence outside a stop remains unanswered. Use wall time by default,
   or a step count when `every=` is given.
