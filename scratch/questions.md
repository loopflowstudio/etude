# EXP-56 choices

The accepted rebuild is `design-train-api.md`; `docs/training.md` describes its
intended experience. Earlier local_launch/Run.until choices and QA history are
superseded as API direction, retained in git at `1666be1:scratch/questions.md`.
They remain evidence about the existing carriage and retry machinery.

## Implementation counterexample: state publication (2026-11-13)

The design promises a whole-or-nothing save of model, optimizer and clocks,
including after a machine loss. It also keeps the existing out carriage.
A bounded file fixture through the actual `StorageDestination` delivered save 1,
then interrupted save 2 after one file. It retained model=save-2,
optimizer=save-1, metadata=save-1. No training or network was involved.

Recovery intentionally accepts newer mutable out bytes than membership: both
`Courier._restore` and `BucketJournal.held` select membership keys, then read
current object bytes. Merely adding hashes to state.json or checking out.json
would detect a mismatch without recovering the previous weights: they were
already overwritten. Reordering mutable files has the same failure.

This changes the persistence design, not a reversible spelling. Dependent
implementation is stopped for design review, as implement requires. The
proposed correction is immutable save generations and a commit manifest
published last, with reader pinning and explicit generation retention/retirement.
An alternative is one atomic archive per save, with extraction for the author's
files. The layout and retention rule must be specified before building either.
Do not quietly weaken the documented recovery promise to per-file atomicity.

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

5. **How a launch names its experiment.** The design now says "Launch
   commands take the name as an argument" (the launcher is the authority; the
   stem is the default). No spelling is given and the docs do not show one
   yet; they still say an `eval(state)`/`report()` file requires its own stem.
   Simplest reading: `--experiment NAME` on `etude launch` and `etude run`.

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
