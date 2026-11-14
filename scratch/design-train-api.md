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

## Later steers (2026-11-13): built, writer shape for demo

`questions.md` records the exact replies. Writers are sub-parts of the
allocation's `RunAttempt`, with separate first-row cutoffs and no effect on
allocation limits or cost. Existing metric files remain the writer evidence;
the progress/finished journals carry their metadata to the allocation record.

The eval author returns a pure tournament:
`evaluation(arena, config=..., play=play)` validates config and calls it at
submission, preserving pending-match planning and selected checkpoint inputs.
The worker opens `evaluating(benchmark)` and calls `run.play(play)`. Rollups
belong to Benchmark/RoundRobin. Planned stops seal results and raise Stopped;
resume is explicit outside launched retries. Low-level relative-seat contexts
retain `lines=`; authored jobs carry the planner's lines without runtime
training reads. This detail and the tentative writer shape stay for demo.

Delete — do not maintain: match execution, worker pool and progress ownership
in `eval.match.evaluate`; it and `hold` are warning-only wrappers until 0.5.
The execution owner is `EvalRun`. Remove the launch-only progress/Tally record
implementations; shared `RunProgress` and `Tally` own the journal data.
Readers and reports use writers rather than treating contexts as allocations.
Preserve old records, cutoffs, command jobs with no contexts, multiple contexts
and runs in one allocation, row replacement, planned-stop retries and costs.
Schema stays 19 (edited in place); full acceptance stays with gate.

## Typed match config and demo (2026-11-13): built

`questions.md` holds the exact new words and chosen defaults. Match[AuthorModel]
receives config validated at the playing boundary, including worker processes;
Seating/RoundRobin accept author models. Python config fields serialize under
historical conditions, so keys and stored JSON stay stable. Old keyword and
property warn until 0.5. statsdb calls its column config (same unreleased schema
14 cut). No Etude-owned MatchConfig class or stored Python type name.

Delete — do not maintain: mapping-only Python conditions fields and their
live consumers; the unreleased etude.job module; the CLI-file top-level
function refusal. Shared types live in etude.spec, constructors in etude.jobs.
Preserve historical JSON, keys, seeds, scheduling and selected inputs.

The optional Hanabi example is examples/hanabi.py and docs/hanabi.md. OpenSpiel
is an optional extra, absent from required/test dependencies. It trains one
small shared PyTorch policy, stops/resumes whole episodes and evaluates a pure
benchmark with typed match configs and team group rows. The demo must be felt
by a person; headless checks prepare it without inventing reactions.

## Built review corrections (2026-11-13)

The first three review passes are built: declaration-only `RunRecord` with
optional `RunSubmission`; one run name; `RunOutcome` and `RunData.outcome`;
`Stopped` from `etude.stopping`; local exit 75 followed by explicit resume;
`--name` names the job; qualified local resume and actionable refusal hints;
launched-only continuation with required `--run` and `--until`. Historical
outcome files and warning aliases until 0.5 remain readable.

Detailed direction, evidence and deletion lists are retained at
`6f70baa:scratch/design-train-api.md` (first through third concept reviews).
No schema, query or provider-count changes in those corrections.

## Fourth concept review (2026-11-13): built

Refusal remedy for a direct `train` call, local-versus-launched reader words
and the stored layout's checkpoint records and qualified id. Full text at
`cb0e1fd:scratch/design-train-api.md`.

## Corrections from the fifth concept review (2026-11-13): built

Walked at `cb0e1fd` in a fresh ETUDE_HOME: coin new / taken / resume /
`--resume --until updates=60`; Hanabi `run`, `eval` (8 new), `eval` again
(0 new), `report`, `statsdb`. All behaved as documented. The mismatches are
at the join to the league, in what the playing code and the tournament write.

1. **A seated checkpoint has no path.** Training reads a checkpoint as
   `rival.path` (`CheckpointRecord.path`). Playing code, the same checkpoint
   one page later, is taught
   `Path(urlparse(player.checkpoint.artifact.uri).path)`
   (`docs/evaluation.md`, `docs/launcher.md`, `tests/eval/games.py`,
   `examples/hanabi.py`). The docs' spelling is also wrong: with no
   `unquote`, a folder under `/tmp/Application Support/` comes back as
   `/tmp/Application%20Support/x` (run here). This design already says the
   playing code "is handed the folder" and that `run.checkpoints()` and
   evaluation read the same records. Build: `CheckpointRef.path`, the one
   owner of that parse, with the record's `path` reading through it and the
   same refusal for an address not on this machine; `play` and every doc
   and example use `player.checkpoint.path`. No other accessor, no helper
   function. Delete — do not maintain: the hand parse in docs, tests'
   playing fixtures and the example, and the record's own copy of it.
2. **`Seating(config=deals)` is several configs under a singular name.**
   Every other `config=` in the same file is one model
   (`evaluation(arena, config=ArenaConfig())`, `environment(..., config=)`,
   `Inference(config=)`, `Match.config`); on `Seating` and `RoundRobin` it
   is a tuple making one match each, and the docs say both "one match per
   condition" and "a match for each config". Jack Heart's words were about
   the author's class ("and be like MatchConfig or something"); the field
   spelling on `Seating` was the worker's reading, and `config=` there is
   unreleased. Build: `configs=` on `Seating` and `RoundRobin` (attribute
   and keyword), `Match.config` unchanged, stored JSON still `conditions`,
   released `conditions=` still warning until 0.5, with no alias for the
   unreleased singular. Docstrings and `docs/evaluation.md` say "config"
   wherever they still say "condition" for this. Keys, seeds and stored
   bytes must not move (the existing historical-key test holds this). This
   is the review's reading, not Jack Heart's words: show it at demo.
3. **The Hanabi example carries two seeds.** `Deal(seed=101)` is the deal;
   `match.seed` is Etude's seed for the match and the example ignores it,
   then stores the deal as column `deal`. Name the field `deal`.
4. **`docs/hanabi.md` reads the wrong folder.** It says to set `ETUDE_HOME`
   for every command, then reads `Path.home() / ".etude/track"`. Use the
   same home in the snippet.

Built: one path decoder on CheckpointRef; configs on the tournament collections,
with historical conditions serialization and warnings preserved; Deal.deal;
Hanabi reads default_root(). The old collection config keyword and hand parses
are deleted. No schema, query, provider-count or stored-format change; no new
command. Focused receipts are in exp-56.md; full acceptance stays with gate.

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
