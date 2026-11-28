# Evaluation records and queries

This reference is for maintainers changing evaluation records and queries; authors start with [the guide](../evaluation.md).

The panel and multi-game examples below describe released record formats.
New authored panels use one config and indexed matches; see
[config and seeds](../evaluation.md#config-and-seeds). Their old keys, source
provenance and tables remain readable without reinterpreting stored results.

## Eval runs

`evaluating` opens one `EvalRun`. Pass a tournament, or an environment with
an explicit match list; `run.play(play)` plays its unstored matches with an
`async def play(match, agents)`, and `run.play_many(play_many)` drives a
streaming async generator over `MatchStream`. Outside a launch, give `experiment=`.
When `run.should_stop` is true, the runner stops granting matches. On a stop
request or SIGTERM it seals staged results and raises `Terminated`. Games play
in fresh `multiprocessing` spawn processes, which import the calling script
again. Keep `run.play` and `run.play_many` under `if __name__ == "__main__":`.

<!-- implementation-reference -->
```python
from etude.eval.tournament import evaluating

with evaluating(
    world,
    experiment="mirror",
    matches=[
        Match(
            players=(a, b, c, d),
            config=Deal(deal=7),
            groups={"north": (0, 2), "south": (1, 3)},
            games=4,
        )
    ],
    storages=[bucket],
) as run:
    run.play(play)
```

- **Players.** A checkpoint seat is a player: `CheckpointSlot()` for the checkpoint
  being evaluated, `checkpoint_player(ref)` for a fixed checkpoint, or a relative
  seat below. Wrap seats in your own frozen Pydantic records to describe search,
  belief models or other composition; Etude interprets only their checkpoint
  leaves. `compose(record)` makes the record portable and gives it a key; a
  seating also accepts the author record directly. The whole wrapper type and
  its fields identify the player, with each resolved leaf replaced by its
  checkpoint key. A bare network, search over it, and search using another
  belief model are three players. Nesting wrappers and containers works too.
  Results and reports name a wrapper by its qualified type and fields, such as
  `search:PUCT(over=small, simulations=64)`, distinguishing same-named types
  from different modules.
  `Code(name="heuristic", source=[...], config=...)` still names code players.
  Code's version records source provenance without changing its key.
  A checkpoint's key includes its experiment, training run and file name, or its outside address,
  never its bytes or current location. Released inference-configured players
  remain readable under their original keys, so their matches stay delivered.
  Give policy parts to `CheckpointRef(experiment="ppo", job="base", seed=1,
  name="updates-500.pt", artifact=saved)`, or use
  `key.checkpoint(name="updates-500.pt", artifact=saved)`. An outside checkpoint
  uses `CheckpointRef(address="hf://lab/weights.pt", artifact=saved)`.
  Resuming a run accepts moved checkpoint files, including leaves inside
  wrappers, without replaying delivered matches. Their digests and byte lengths
  must still agree with the stored references.
- **A checkpoint is a folder or a file.** `play` finds it at the player's
  `checkpoint.path`. A checkpoint a run published from named objects
  (`run.checkpoint(policy=model)`, [Training](../training.md#checkpoints)) is a
  folder holding `checkpoint.json`, which names its files, and the playing
  code loads the ones it needs:

  <!-- implementation-reference -->
```python
  folder = player.checkpoint.path
  files = json.loads((folder / "checkpoint.json").read_text())["files"]
  policy.load_state_dict(torch.load(folder / files["policy"]))
  ```

  One checkpoint holding several models is still one checkpoint. An author's
  wrappers select how to use its loaded agent. A checkpoint recorded as a
  single file is that file.
- **Setup.** How a game is run, such as the device or dtype, is provenance,
  like the seed, and is said in two places. What `play` is asked to run with
  goes under a `setup` key of the environment's or a player's config:
  `environment("duel", config={"world": "w6", "setup": {"device": "cuda"}})`.
  What `play` actually used it returns: `Result(tables=tables,
  execution={"device": "cuda:0", "compute": "shared-eval-v2"})`, kept with
  each result (`MatchRecord.execution`, `matches.execution`) alongside its
  seed (`matches.seed`) and each player's code version (`seats.commit`,
  `seats.source`). Neither is part of any identity: a CUDA benchmark is the
  CPU one's environment, players and matches, a match played under either
  counts for both, and their games pool. Requested games may finish across
  any setups; a replay's latest delivered result counts once in reports.
  Etude never guesses which fields are setup: anything outside `setup`,
  in a wrapper, `Code.config` or match config, is identity, and
  older configs keep theirs. `execution` on older records reads as `{}`.
- **Where a checkpoint stood.** A seated checkpoint may say where it stood on
  its clocks: `CheckpointRef(address=..., artifact=..., at={"updates": 600})`.
  A tracked checkpoint's `ref` fills `at` from its run's clocks; give it
  yourself for a checkpoint no Etude run recorded. It is carried to each
  match's seats (`seats.at`, and every distinct one in `players.seated_at`) so
  a report can place the point, and is part of no key: correcting it never
  forks a player or replays a match. Where seats disagree, a report draws the
  latest and notes the earlier.
- **Which learner it is.** A policy checkpoint derives `line` from its
  policy's job, seed and role. Its continuations share that line; foreign
  checkpoints add their experiment when viewed in another experiment's report.
  For an outside checkpoint, give `line="seed-1"` to compare it with other
  checkpoints on that learner's curve. Like `at`, the line is carried to
  the seats (`seats.line`, `players.lines`) and is part of no key.

- **Source files.** A directory stands for the files below it. In a git
  checkout only tracked files count, named by their path in the checkout, so
  caches and scratch files never change a version; an uncommitted edit does,
  at the same commit. A launched job's source has no `.git`; there files are
  named by their path below `ETUDE_SOURCE_DIR` and the commit is
  `ETUDE_COMMIT`, both of which the launcher sets, so the same commit gives the
  same version on a rented machine as on yours.
- **Older eval runs.** Records earlier etude-sdk releases wrote, with
  `agents` and a seat column named `player`, read as players and `seat`, with
  the same keys, so nothing is played again. A `seat` column beside `player`
  in such a table was the game's own, and reads as `game_seat`. Players recorded by etude-sdk
  0.2 were identified by a digest; they load as `etude-sdk-0.2:DIGEST`, which
  no key since equals, so their results stay readable and nothing new counts
  them as played.
- **Match config.** Your Pydantic model, such as `Deal`, describes one
  match's settings. `Seating(configs=(Deal(deal=7), ...))` makes a match for
  each config. Annotate `async def play(match: Match[Deal], agents: Sequence[object])`
  or `async def play_many(matches: MatchStream[Deal])` to receive that type,
  including in worker processes. Etude defines no match config base class.
  The JSON values identify the match; the Python class name does not. Stored
  `conditions` fields still decode; the old Python property is removed.
- **Matches.** Players in their seats under fixed `config`, played for
  `games`: the unit Etude schedules, resumes and resamples. A tournament's
  `Seating` makes one match per config. Seat swaps belong
  inside `play`. Group `all` holds every seat; a player may sit in two seats.
- **Game processes and placement.** `Placement` owns the group's process
  layout, checkpoint bytes per inference owner and startup line. The
  checkpoint reservation is `Agents.memory_gb` or half of host memory.
  - Shared inference (`bus="socket"`) requires a declared, detected CUDA or
    Metal accelerator and more than one game process. Each game process serves
    one tournament and gets one thread. The agent process gets the remaining
    cores and the full checkpoint reservation. Both accelerators cap the number
    of game processes at `max_batch`.
  - In direct inference (`bus="local"`), each process can serve the tournament
    queues in turn and keeps its cache across streams. Direct copies share the
    checkpoint reservation and use `max(1, (cpus - processes) // processes)`
    inference threads each. Process count is capped so each cache can fit the
    largest pending match's known checkpoint bytes, with a one-process floor.
  - Code-only games (`bus=None`) use one thread per game process and skip the
    accelerator probe.

  Each game process has one event loop. Async `play` runs up to
  `games_per_process` matches at once and flushes forwards on a zero-delay
  loop barrier. Placement sizes for one process per async match first, then
  divides the batch budget across processes. Each async tournament bounds
  the slots it needs using its own process share. Direct `play_many` sizes
  for one process per `max_batch` matches, rounded up. Shared and code-only
  `play_many` size for one process per match. Both play modes request their
  first matches after loading and setting up the callback. Active streams
  without held matches count toward pending process demand. A freed dedicated
  process slot goes to the
  tournament with unmet demand and the fewest active processes. Each
  `play_many` generator submits forwards on the call and requests matches for
  its free slots.
  `MatchStream.target_matches` uses the same share calculation as grants and is
  capped at `max_batch` when a loader is declared. The startup line shows the
  hints given to the initial streams. Shares account for held matches and
  reserve pending work for empty processes. A positive request receives at
  least one while grants are open and work remains. Only processes serving
  that tournament count toward grant shares.
  The last grant closes the stream's requests locally, even if it includes matches.
  Both kinds of tournament share an agent process when using shared inference.
  Per-process diagnostics go to `artifacts/agents/game-{pid}.json`
  (`GameReport` with `tournaments: tuple[str, ...]`), keeping a game-thread-filtered
  `builtins.__import__` hook active from module load through player wrapper
  loading and play until process exit (with `"mlx"` alongside `"torch"`,
  `"jax"`, `"tensorflow"` in `_HEAVY`) so inference-thread imports are ignored
  while wrapper and lazy game imports stay visible. Import warnings print once
  on stderr for heavy ML libraries when a bus is active. Socket `play` also
  warns when the module count exceeds `MAX_GAME_IMPORTS`. `GameReport` embeds a
  frozen `BusReport` (`calls`, `answers`, `arrays`, `arrays_copied`, `pickled`,
  `pickled_bytes`, `copied_bytes`, `wire_bytes`, `handoff_seconds`,
  `round_trip_seconds`).
  Batch row counts and stage timings use one `Batch` record:
  `artifacts/agents/batches-{pid}.json` per direct game process and
  `artifacts/agents/batches.json` for the shared agent process. The shared
  agent has up to two seconds within the run deadline to close its report
  before termination. `evaluating` has no setting for process count.
- **Order.** Matches start in the order `etude.eval.schedule.ordered` gives,
  so consecutive matches reuse the checkpoints kept loaded: first those
  loading the fewest checkpoints not yet used, then those reusing the most
  recently used. Ties, and queues of code players only, keep your order.
  Order changes no result.
- **Rows are yours.** `play` returns any tables. Each row names a `seat`
  (its position in `match.players`) or a `group` of its match; Etude adds `run` and `match`. Values are
  numbers, text, booleans or nulls. A game's own seat numbering, when it
  differs from the match's, goes in `game_seat`.
- **Blobs.** Bytes a match keeps beside its rows, such as a game's replay
  evidence: return `Result(tables=..., blobs=[...])` (`etude.eval.result`)
  and name each blob in a row by `hashlib.sha256(blob).hexdigest()`.
  `read_blobs(db, store, digests)` gives them back, verified.
- **Rollups.** A summary you will query often is declared once and summed
  as each match is stored, never from raw rows again:

  <!-- implementation-reference -->
```python
  from etude.eval.records import Rollup

  cards = Rollup(
      name="card_deals",
      table="card_counts",
      join=("card_plays",),
      by=("card", "deal", "on_play", "player", "opponents"),
      sums={"gih": "gih_copies", "gih_wins": "gih_copies * win"},
      where="eligible",
  )
  progress = Benchmark(name="cards", environment=world, seatings=seatings, rollups=(cards,))
  with evaluating(progress, experiment="cards") as run:
      run.play(play)
  ```

  `statsdb` then has table `card_deals`: per run and value of `by`, `rows`
  and the total of each of `sums`, a DuckDB expression over a row (a
  boolean counts as 1, a null as 0; a mean is a total over `rows`), over
  the rows for which `where` holds. A row is `table`'s joined to each of
  `join` on whichever of run, match, seat and game both have. `by` may name
  `player`, the key of the row's seat's player, and `opponents`, the other
  seats' keys in seat order, a list (`opponents = ['KEY']` in a two-seat
  match). Blocks sealed by etude 0.3.0 joined them with commas, which keys
  also hold; such a value reads as a one-element list of that text, the
  opponent's key in a two-seat match. A column a match's rows never
  gave reads as null. A rollup that cannot sum a match (a sum of text, an
  expression DuckDB rejects) fails the match. Each block keeps its matches'
  partial totals in `card_deals.parquet`; `statsdb` adds a new block's to the
  totals it holds, and a match two blocks hold counts once, from the later.
  A run keeps the rollups its first block had: resuming with others is
  refused (`eval_run_rollups_conflict`).

  A rollup thought of later is given to the query instead:
  `statsdb("cards", bucket, rollups=[cards])` sums each block's partial
  totals once from its tables and keeps them on this computer, never in the
  store, and keeps adding new blocks' on every later open. A rollup is its
  name and its definition: another definition under a held name raises
  `rollup_conflict`, so a changed rollup takes a new name.
- **Strict.** A match that returns no rows, a row with neither or both of
  `seat` and `group`, a seat or group the match lacks, a column named
  `player`, or a column whose type changes during a run is refused. A refused
  or raising match is recorded as failed with its error, nothing else of it
  is stored. Ordinary `play` errors and returns of the wrong type fail one
  match. In `play_many`, yield `(match, error)` for an ordinary error and
  keep taking pending matches.
  Refused tables, uncaught generator errors, invalid yields and early returns
  stop new grants for the tournament. Broken streams fail their held matches.
  Errors loading or setting up authored game code fail the tournament and
  leave ungranted matches available on resume. Sibling tournaments continue.
  A game process that exits without reporting, a failed shared-agent connection
  or an explicit retry interrupts the group and keeps unfinished matches
  eligible for retry.
  Completed outcomes remain available for resumption.
- **Environments are never pooled.** An eval run has one environment;
  resuming with another is refused, and a report shows each drawn value from the
  environment whose latest match is newest, noting only overlaps on values
  reachable panels draw.
- **Resume.** Outside a launch, reopen the same eval run with
  `evaluating(..., resume=True)` and call `run.play(play)` or
  `run.play_many(play_many)` to carry on.
  A launched retry resumes automatically. Matches already delivered to any
  of `storages` are skipped, even on a machine with an empty disk.
  A retryable error is returned for each tournament with queued or in-flight
  matches. Matches dropped from the queue after a permanent failure remain
  unplayed. The group raises `Terminated` after closing its members when a stop
  interrupted them. A game process failure belongs to its unfinished streams.
  A failure after all streams end applies to all tournaments that process
  served. Already stored matches are reused on retry.
- **Progress.** Each stored match prints one line to stderr:
  `etude: RUN: 12/36 matches played, 220s elapsed`.
- **Storage.** Stored matches are sealed into blocks every five minutes (or
  256 MiB of blobs), on a failure and on finish, and each block is delivered
  once sealed; `etude sync` finishes delivery later. A block is a folder
  written once, `RUN/blocks/WRITTEN.WRITER/`: `blobs.zip` (uncompressed
  members named by SHA256), `TABLE.parquet` per table, `ROLLUP.parquet` per
  rollup, then `block.json` with each match's record. A table's rows are
  sorted by match, and by `game` where rows have one, in row groups of 8192
  rows, so a read filtered to a few matches skips most of a file. Plain
  DuckDB reads a run's tables without Etude:
  `read_parquet('RUN/blocks/*/plays.parquet')`. A process killed before it
  seals leaves its matches staged on disk; reopening with
  `evaluating(..., resume=True)` in the folder seals them. SIGTERM seals before the run ends.
- **Older eval runs.** Runs written before blocks kept a file per match,
  table and artifact. Readers refuse them (`eval_run_not_converted`) until
  `etude convert-eval RUN --source URI` writes blocks beside the old files,
  each artifact becoming a blob; `etude.eval.convert.convert(RUN, URI,
  rollups=[...])` also sums rollups into them. It converts only what no block holds yet,
  so it may run again. `etude delete-eval-files RUN --source URI` then
  deletes exactly the files delivered blocks replace, and deletes nothing
  while any old file there is unconverted. Convert where the run is written,
  deliver, then delete in each store.

## Forward bus

`etude.eval.bus` defines the single transport boundary between game code and
loaded checkpoints (`ForwardBus` and `ForwardCall`). It imports no
batching or loader code; NumPy is imported when an array is first seen, and
Etude never imports a model library.

<!-- implementation-reference -->
```python
@dataclass(frozen=True)
class ForwardCall:
    checkpoint: str  # Key registered by bind().
    inputs: list[object]
    rows: int


class ForwardBus(Protocol):
    @property
    def flight(self) -> Flight: ...

    def bind(self, checkpoint: CheckpointRef) -> None: ...
    def submit(self, calls: Sequence[ForwardCall]) -> list[asyncio.Future[list[object]]]: ...
    async def close(self) -> BusReport: ...
```

A seat's `Forward` handle holds a checkpoint key and the game process's
`_Forwarder`. In `play_many`, a call submits immediately. In `play`, the
forwarder locks inputs and groups calls from the same event-loop turn by
checkpoint and row budget. Both paths resolve each caller's future with its
slice of the reply. The forwarder records loading failures even when a match
abandons its awaitable.

Both transports use `_FlightBook` to allocate request IDs, own the input locks
and count transfers. The barrier holds a reference in the same lock ledger
until submission acquires its own. A writable NumPy input stays locked
through the last reply using it, including after cancellation. Batching uses
`_Batches` to merge ready requests up to `max_batch` and `Forwards` to load
and evict checkpoints. It encodes each reply independently so a serialization
failure applies only to its caller.

**Direct inference.** `LocalForwardBus` queues requests to a non-daemon
inference thread. Only that thread holds the loaded models. Plain
C-contiguous, non-object NumPy inputs travel as read-only views. Pickle
protocol 5 carries containers with eligible array buffers sent out of band.
The thread copies NumPy outputs before replying, so the model can reuse its
buffers. It posts replies to the game loop with `call_soon_threadsafe`.
Closing the bus signals the thread and joins within the evaluation deadline.
If the join expires, outstanding calls keep their inputs locked. An unexpected
thread exit fails pending and later calls with `RetryRequested`.

**Shared inference.** `SocketForwardBus` sends length-prefixed frames to the
agent process over a private Unix socket. Serialization copies the NumPy
outputs. Each request and reply carries its call ID and checkpoint key in
the header. An undecodable body fails that call with a permanent
`ForwardError`. The reserved ID zero carries a peer-wide framing error.
An unexpected disconnect or truncated frame requests a retry.

On both transports, the array codec walks lists, tuples, dictionary values and
dataclass fields. It retains containers with unchanged leaves and reconstructs
containers that need a conversion, preserving cycles.
It tries the buffer protocol before CPU DLPack so lazy MLX outputs materialize
on the inference thread. It copies the resulting arrays and releases buffer
views on the producing thread. A device array that cannot export CPU data
fails its call with `ForwardError`. The shared contract suite in
`tests/eval/test_bus.py` exercises both transports.

## Tournaments

- **Benchmarks.** Named seatings, each its own line over training. A
  seating is the rule for who meets whom: `players` in seat order, with its
  own `configs` and `games`, and it makes one match per config. A
  seating's value is its first player's. `evaluating(benchmark, checkpoint=ref)`
  fills the seats for the checkpoint (by its key) and plays each given
  seating; with no checkpoint it plays every seating ever defined for every
  checkpoint held, skipping what is delivered.
- **Seats.** A seat is one of three kinds:

  | Seat | Seats |
  |---|---|
  | `CheckpointSlot()` | the held checkpoint, played directly |
  | `Code(name="heuristic", ...)`, or `checkpoint_player(ref)` | the same fixed player for every checkpoint: code, or one chosen checkpoint such as the initialization or a frozen anchor |
  | `Earlier(clock="updates", by=400)` | the held checkpoint's line exactly 400 updates before it |
  | `Fraction(clock="updates", value=0.5)` | its line at exactly half the held checkpoint's updates (3,000 for 6,000) |
  | `Fraction(clock="running_hours", value=0.5, at_or_before=True)` | its line's latest checkpoint at or before half the held checkpoint's running hours |
  | `Earlier(clock="running_hours", by=2, at_or_before=True)` | its line's latest checkpoint at or before two running hours before it |
  | `At(clock="updates", value=200)` | its line at update 200 |
  | `Final()` | its line's last checkpoint, once its run has ended |
  | `Aligned(clock="updates", job="small")` | job `small` at exactly the held checkpoint's updates |
  | `Aligned(clock="running_hours", job="small", at_or_before=True)` | job `small`'s latest checkpoint at or before the held checkpoint's running hours |
  | `Final(experiment="tla-frontier", job="small")` | every seed of another experiment's job |
  | `Final(policy=PolicyKey(experiment="tla-frontier", job="small"))` | one policy, using its lowest seed by default |

  The remaining rows are relative seats: each names a checkpoint by where it
  stands relative to the one held, and supplies that checkpoint as a leaf. Wrap a relative seat in the same author records as a held or fixed seat.
  `Earlier`, `Fraction`, `At` and `Final` look in the held checkpoint's own
  line unless given a policy selector: `Final(job="small")` seats
  job `small`'s final checkpoint, `At(clock="updates", value=0, job="small",
  ...)` its initial one. `Aligned` uses the held checkpoint's clock value
  on the selected policy. A `job` is named as the report names it
  ([tracking](../tracking.md#reports)): its launch name, or the full name with
  its config digest. Leave all policy selectors out to mean the held
  checkpoint's own line.
  A job seat fills from each of that job's lines (see Seeds).

  Beside `job`, every relative seat takes `experiment=`, `seed=` and
  `role=`, the parts of a [policy's name](../evaluation.md#players-and-composition). A seat given any of
  them leaves the held checkpoint's line, and the parts it leaves out are
  filled as a key's are: this file's experiment, the experiment's only
  training job, the run's only role. The seed is the exception: left out, the
  seat fills from every seed, and `seed=1` keeps it to one. So
  `Final(experiment="tla-frontier", job="small")` seats the
  final checkpoint of each seed of another experiment's job. `policy=` takes
  a `PolicyKey` instead and seats that one policy; a seat given `policy=`
  and any of the four parts is refused when it is built. An experiment with
  no runs under the source yet is a gap, as an unstarted job is.

  A hold finds relative seats' checkpoints among its `lines`: the training
  lines of the experiment and of every other experiment a seat names
  (`observe(source, experiment).lines` for each), held or not; `etude eval`
  and `etude launch` pass them themselves.
  A seating against a fixed player and one against a relative seat are held,
  scored and drawn alike. A seating with a relative seat needs a held
  seat to be relative to. A seating of two fixed players, such as two jobs'
  checkpoints chosen in `eval_jobs(state)`, is drawn at its first seat's checkpoint,
  so `Curve(matches=...)` over such seatings draws each pair once.

  `Final` is `At` the position where the run ended: a gap until it ends,
  finished or failed, then its last stored checkpoint. A run whose machine
  was lost never writes its end; it has failed once the launcher on the
  computer reading it has closed it without success (`etude status RUN`),
  so its `Final` is its last stored checkpoint too. Holding a seating
  against `Final(job=...)` for a job's own final checkpoint gives final
  against final. To meet one chosen checkpoint of another job, seat it as a
  fixed player.

  CheckpointSlot for a checkpoint of the job a seat's `job` names, a seating plays
  that job, and a seat that finds the held checkpoint itself seats it:
  self-play is a match like any other, as a cooperative game or a score
  besides winning wants. To play a seating for some jobs only, hold its
  benchmark for their checkpoints.

  Positions are exact by default: a checkpoint at 2,990 is not half of
  6,000. A seat with a position takes `at_or_before=True` for the latest
  checkpoint at or before the position instead, found once the line has a
  checkpoint past it or has ended, so a later save never moves it. Etude's
  clocks, `running_hours` and `hours_since_start`, are on every checkpoint
  ([tracking](tracking.md#etudes-clocks)); no checkpoint is saved at an exact
  hour, so a seat on them takes `at_or_before=True`. A seat with a position
  takes `trained=True` to seat only checkpoints saved after training began:
  a line's initial weights, where every clock its run declared stands at 0,
  are then never seated, so `Fraction(clock="running_hours", value=0.5,
  at_or_before=True, trained=True, ...)` plays no early checkpoint against
  its own initialization. A held checkpoint with no trained one to meet is a
  gap that never fills. A seat
  with no checkpoint there yet plays nothing, and the next hold with no
  checkpoint plays it once one is saved, so a large job's seating against
  `Aligned(job="small")` fills in as the small job catches up. A relative
  seat resolves to the same checkpoint whenever it is held, so a played
  match never moves as runs save more.
- **Seeds.** A seating need not name a seed. `games` is per config and
  point, a position of the held checkpoint's job: by default the point
  plays every pair of a seed of that job and a seed of the other seat's
  (its own line, a fixed player, or a seat given `seed=` or `policy=`,
  counting as one), the games split
  evenly over the pairs, the remainder to the lowest by seed, and pooled.
  A job seat waits while any of its seeds may still save there. A seed is a
  line, with the seed its first run declared. `seeds="one"` plays all of a
  point's games between one seed per side, drawn from the seating's name
  and the seed sets, the same pair at every point: faster, but the curve
  shows one pair of seeds, not the jobs. A seed added later changes the
  split, so that job's points are played again and count only the new one.
- **Pairs of checkpoints.** A seating with no held seat is played once,
  not per checkpoint, so chosen checkpoints can meet: seat each as
  `checkpoint_player(ref)`, the player a held seat would make of it.
  Prefer a relative seat where one says the rule; hand-built pairs suit a
  rule none states. Hold such a benchmark in batch whenever more checkpoints
  appear; each pass plays only the pairs not yet delivered. A hold on the go
  leaves these seatings to batch. Their rows are `schedule` rows with no
  `checkpoint`; `scores` follows only seatings with a held seat.
- **Round robins.** `evaluating(round_robin, enter=players)` plays every pair of
  the players given once per config; seat swaps belong inside `play`.
  `evaluating(round_robin)` then plays every pair of every entrant so far that no
  eval run delivered, such as a late entrant against the earlier ones.
- **CheckpointSlot for an experiment.** A tournament only decides which matches to
  play. `evaluating(..., experiment=...)` makes its eval run the experiment's, so
  its results sit beside the experiment's training runs and their
  checkpoints in the experiment's database.
- **Stored in its eval runs.** Each eval run `evaluating` opens keeps
  `tournament.json` in its own folder: the definition it was held with, and
  the checkpoint or entrants it added. A tournament is every eval run naming
  it, so a hold on a rented machine delivers its part like any other run, and
  many eval runs can grow one tournament at once.
- **Reuse.** A match is the same wherever it is played, so within one
  experiment and environment its results are reused by key: `scores`
  and a batch hold count a scheduled match that any of the
  experiment's eval runs in the environment holds, whether held for this
  tournament, another, or none. Each counts once, from the eval run that
  recorded it last.
- **Conflicts.** Another format, environment or round-robin rules under one
  name (`tournament_conflict`), or a seating name defined two ways
  (`seating_conflict`), is refused by a batch hold, a pass and `scores`;
  `statsdb` reports it in the `tournaments` table. Reading is
  not refused: where a later revision of an experiment file defines a
  tournament otherwise than its eval runs were held with, `etude report`
  draws what they recorded and says so.
- **Run names.** An eval run is named for the tournament and what is given,
  or, played by an [evaluation job](../evaluation.md), for the tournament and
  its matches, so a retry reopens and resumes it, finding the same checkpoint
  (by its key) wherever the retry put it.
- **Stored shares.** Eval runs held as one part of a split hold (etude 0.4.0
  and earlier) still read, including the `[k, n]` pair written by etude 0.3.0;
  new runs play the whole hold.
- **Cost.** Holding a checkpoint or entrants reads nothing of the
  tournament: it plays their matches. Holding nothing new (batch) lists each
  place once and reads each eval run's `run.json` and, for the experiment's,
  its `tournament.json` (or its `environment.json` when it was held for no
  tournament), and each block index of those in the tournament's
  environment, to play what the tournament calls for and none of the
  experiment's eval runs in its environment delivered.
- **`scores`** gives one `Scores` per seating and training run: each held
  checkpoint's name, ordinal, `at` (where its run's clocks stood when it
  saved it), and the first player's value with a 95% interval over whole matches.
  A held checkpoint whose relative seat found nothing has no point:
  `Tournament.resolve(lines)` gives each such `Gap` and why, `not_yet` or
  `never`, with the `value` of the seat's clock it looked for, and the report
  draws it. Checkpoints no training run under the
  source recorded make one `Scores` per seating,
  ordered by name, numbers in order (`updates-900` before `updates-1000`).

## Measuring CUDA evaluation

Read `game-*.json` for matches played by each process and `batches.json` for
rows per forward and time spent waiting for requests.
`max_batch` defaults to 32 rows and bounds how much the agent process merges.
An oversized pre-collated request runs alone.

Before fair-share grants, Manabot played the same 128-game float32 panel on
one RTX 4090 through Etude's synchronous API and through `play_many` with
64 slots per process.
All 128 traces per checkpoint matched across the two callbacks. Two of the
12 streaming processes played matches. The socket arm spent 30–38 seconds
waiting for requests and 11–12 seconds in forwards.

| Checkpoint | Sync games/hour | Streaming games/hour | Sync GPU busy | Streaming GPU busy |
| --- | ---: | ---: | ---: | ---: |
| Trained `w160l8` | 11,686 | 8,017 | 20.1% | 13.3% |
| Seeded `w384l8` | 11,506 | 9,488 | 23.6% | 17.4% |

Sync used Manabot `966e8ff296` and streaming used `fb1580b472`. These figures
capture the combined changes to game stepping and Etude serving on one host.

With `w384l8`, cutting the engine to 16 slots gave eight processes matches
and raised streaming throughput to 17,995 games/hour at 33.1% GPU busy.
Mean rows per forward fell from 8.25 to 6.37 as time waiting for requests
fell from 30.3 to 5.6 seconds. The 64-slot arm also ran with `max_batch=64`
and delivered 9,441 games/hour at the same mean rows per forward. Capping
that 64-slot callback at four processes with `max_batch=32` delivered
9,403 games/hour. Two processes received matches in both runs.

## Querying

`statsdb(experiment, store)` gives each of your tables across every eval run of
the experiment, beside Etude's own:
`runs` (with the tournament each was held for), `players` (`kind` `model` or
`code`, `name` the latest its seats give and `names` every one, with a
`config.KEY` column per scalar config key, dotted when nested), `matches`
(`newest = 1` marks the record that counts: of a match's done records in its
environment, the latest), `seats`,
`groups`, `tournaments` (with any `conflict` among its eval runs), `schedule`
(every match a tournament calls for, with its seating and checkpoint) and
`training_checkpoints` (every checkpoint a training run under the source
recorded, with its `at`, joined to players and the schedule by `checkpoint`, its key) and
`blobs` (each blob's `sha256` and size, by run and match; a converted run's
artifacts name no match), and one table per rollup.

<!-- implementation-reference -->
```python
from etude.eval.query import read_blobs, statsdb
from etude.storage import storage_from_uri

bucket = storage_from_uri("r2://manabot/evaluations")
db = statsdb("cards", bucket)
named = [d for (d,) in db.sql("select evidence_sha256 from cards limit 10").fetchall()]
for digest, evidence in read_blobs(db, bucket, named):
    ...
```

The database is kept in a file under `ETUDE_HOME/statsdb`, one per
experiment and store. Opening it lists the store once and reads only what
arrived since it was last opened, so a query never waits on every result
being read again: a new block costs its index and one file per table and
rollup, and never its blobs. Catch-up fetches each chunk of new blocks
concurrently and commits and checkpoints as it goes, publishing an empty schema
first on a cold start so an interrupted first open resumes from the last
committed chunk. Private generations (`rebuild=True` or catch-up while a reader
holds the current snapshot) update summary tables once at completion and publish
atomically. A rollup's totals are added to, never summed
again from earlier blocks. `read_blobs` reads each pack's adjacent blobs in one range,
kept on this computer, so asking again reads nothing. The store stays the authority: `statsdb(..., rebuild=True)`
reads everything again, and a database from another Etude schema is rebuilt
automatically. Each connection reads a snapshot. If new blocks arrive while a
reader holds the current snapshot, catch-up publishes a new one for later readers.
Experiments are never joined: each has its own database.

`compare(db, table=, value=, per=, where=, by=)` sets each subject against
each opponent and against all: a seat's player against the other seats'
players, or a group against the match's other groups. The value is
`sum(value) / sum(per)`, or the mean without `per`. The 95% interval is a
percentile bootstrap over whole matches with a fixed seed, independent of
the order rows are read in; fewer than two matches have none.

`estimate` answers many ratios over the same resampling units, such as deals,
with one bootstrap, as a page of per-card rates over the same deals needs:

<!-- implementation-reference -->
```python
from etude.eval.query import estimate

# Units × columns: per deal, each card's wins and games.
wins = [[1, 0], [2, 1], [0, 1]]
games = [[2, 1], [3, 1], [1, 2]]
for card in estimate(["deal-1", "deal-2", "deal-3"], wins, games):
    print(card)  # Estimate(value=..., interval=(low, high)), or None without games
```

Units are resampled in the order of their keys, never of their values or of
how they were read, so each column's interval is the one it would get alone.
Releases through v0.2.9 resampled blocks in the order of their values, so
their intervals differ slightly from these: the same distribution, other draws.
