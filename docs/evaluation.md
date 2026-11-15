# Evaluation

Play players against each other in parallel game processes, hand Etude whatever tables
your game code produces, and query each experiment's results as one DuckDB
database. Tournaments decide which matches to play: each checkpoint of a run
through a benchmark's seatings as it trains, or hand-picked players in a round
robin. Evaluation is independent of training.

```sh
uv add 'etude-sdk[eval]'   # DuckDB, for writing and querying tables
```

```python
from pydantic import BaseModel

from etude.eval.match import Tables, environment
from etude.eval.query import statsdb
from etude.eval.records import (
    Benchmark,
    Code,
    Fraction,
    Inference,
    Match,
    RoundRobin,
    Seating,
)
from etude.eval.tournament import evaluating, scores, standings
from etude.experiment import observe
from etude.checkpoints import CheckpointRef, checkpoint_id

world = environment("allies-vs-lessons", files=["game/"], config={"world": "w6"})


class Deal(BaseModel):
    deal: int


deals = tuple(Deal(deal=1_910_101_000 + d) for d in range(25))
heuristic = Code(name="heuristic", source=["players/heuristic.py"])
greedy = Code(name="greedy", source=["players/greedy.py"])


def play(match: Match[Deal]) -> Tables:  # importable by name: games play in fresh processes
    results = play_deal(match.players, deal=match.config.deal, games=match.games, seed=match.seed)
    return {
        "plays": [
            {
                "game": r.game,
                "seat": r.seat,
                "score": r.score,
                "on_play": r.on_play,
                "opening_lands": r.lands,
            }
            for r in results
        ],
        "games": [{"game": r.game, "group": "all", "turns": r.turns} for r in results],
    }


# An Inference seat is filled by the checkpoint being held, run by your inference code
# and playing with your config (a Pydantic model or JSON): together, a player.
net = ["players/net.py"]
direct = Inference(source=net, config=Direct())
progress = Benchmark(
    name="anchors",
    environment=world,
    seatings=(
        Seating(
            name="no search vs heuristic",
            players=(direct, heuristic),
            configs=deals,
            games=8,
        ),
        Seating(
            name="no search vs greedy",
            players=(direct, greedy),
            configs=deals,
            games=8,
        ),
        Seating(
            name="search vs no search",
            players=(Inference(source=net, config=PUCT(simulations=64)), direct),
            configs=deals[:10],
            games=4,
        ),
        # A relative seat: the held checkpoint against its own learner at half its updates.
        Seating(
            name="half-training",
            players=(direct, Fraction(clock="updates", value=0.5, inference=direct)),
            configs=deals[:10],
            games=4,
        ),
    ),
)

# On the go: as each checkpoint is saved, one small eval run plays every seating.
# A checkpoint is its key, its run and file name, with the saved file to read.
# Relative seats find theirs among the experiment's training lines.
ref = CheckpointRef(key=checkpoint_id(run="ppo-1", name="updates-500.pt"), artifact=saved)
lines = observe(bucket, "ppo").lines
with evaluating(progress, experiment="ppo", checkpoint=ref, storages=[bucket], lines=lines) as run:
    run.play(play)

# Batch: add a seating to the definition; holding with no checkpoint plays every
# missing match, so the new seating is played for every checkpoint held so far.
with evaluating(progress, experiment="ppo", storages=[bucket], lines=lines) as run:
    run.play(play)

# Batch: hand-picked players, each against every other.
league = RoundRobin(name="finals-1", environment=world, configs=deals, games=8)
with evaluating(league, experiment="ppo", enter=finals, storages=[bucket]) as run:
    run.play(play)

db = statsdb("ppo", bucket)  # or "s3://my-bucket/runs", a directory, or nothing: ~/.etude/track
for each in scores(db, "anchors", table="plays", value="score"):
    print(each.seating, each.run, [(p.name, p.at, p.value) for p in each.points])
for entry in standings(db, "finals-1", table="plays", value="score").entries:
    print(entry.name, entry.opponent_name, entry.value, entry.interval)
```

```python
db.sql("""select a.name, avg(score) from plays
          join seats using (run, match, seat) join players a using (player)
          where on_play and opening_lands >= 2 group by all""").show()
```

## Eval runs

`evaluating` opens one `EvalRun`. Pass a tournament, or an environment with
an explicit match list; `run.play(play)` plays its unstored matches.
Outside a launch, give `experiment=`. `run.should_stop` checks
stop requests between matches; a planned stop seals completed tables and
raises `Stopped`. `hold` and `evaluate` are warning wrappers until 0.5.

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

- **Players.** A player is whatever takes a seat: a checkpoint and the code
  that plays it (`Inference`), or `Code` alone, such as a heuristic. "Model"
  is left to mean your network. A checkpoint player's
  key is the checkpoint's own key plus its inference config; code's is its
  name plus its config. A config is free JSON or a Pydantic model. One
  checkpoint at two search budgets is two players. A checkpoint's key
  (`CheckpointRef.key`) is its training run and name
  (`ppo-1/checkpoints/updates-00000500`) or, for any other, its address; never
  its bytes, so two runs started from one file hold two checkpoints,
  each evaluated. An outside player is one whose checkpoint is an
  address. `Inference(source=["players/net.py"], config=...).player(ref)` and
  `Code(name="heuristic", source=[...])` name the code that plays: its commit
  and source digest are its `Version`, recorded with each match
  (`seats.commit`, `seats.source`) but not part of the key, so changed code
  reuses what was played. Joins use the key, encoded (`players.player`,
  `seats.player`). Eval runs stored before this wording keep `"kind":
  "model"` and read the same.
- **A checkpoint is a folder or a file.** `play` finds it at the player's
  `checkpoint.path`. A checkpoint a run published from named objects
  (`run.checkpoint(policy=model)`, [Training](training.md#checkpoints)) is a
  folder holding `checkpoint.json`, which names its files, and the playing
  code loads the ones it needs:

  ```python
  folder = player.checkpoint.path
  files = json.loads((folder / "checkpoint.json").read_text())["files"]
  policy.load_state_dict(torch.load(folder / files["policy"]))
  ```

  One checkpoint holding several models is still one checkpoint. Two
  `Inference` seats with different source seat it as two players: one that
  loads every file to draft, build and play, and one that loads only
  `player.pt`. A checkpoint recorded as a single file is that file.
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
  in `Inference.config`, `Code.config` or match config, is identity, and
  older configs keep theirs. `execution` on older records reads as `{}`.
- **Where a checkpoint stood.** A seated checkpoint may say where it stood on
  its clocks: `CheckpointRef(key=..., artifact=..., at={"updates": 600})`.
  A tracked checkpoint's `ref` fills `at` from its run's clocks; give it
  yourself for a checkpoint no Etude run recorded. It is carried to each
  match's seats (`seats.at`, and every distinct one in `players.seated_at`) so
  a report can place the point, and is part of no key: correcting it never
  forks a player or replays a match. Where seats disagree, a report draws the
  latest and notes the earlier.
- **Which learner it is.** `line` names the learner a checkpoint belongs
  to, such as a seed or a recipe: `CheckpointRef(..., at={"updates": 600},
  line="seed-1")`. A report compares a checkpoint with earlier ones, and
  draws its curve, only within its line. A tracked checkpoint's `ref` fills
  `line` with the first run of its run's chain of continuations, so a
  continued learner is one line. Like `at`, it is carried to the seats
  (`seats.line`, `players.lines`) and is part of no key.
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
  each config. Annotate `play(match: Match[Deal])` to receive that type,
  including in worker processes. Etude defines no match config base class.
  The JSON values identify the match; the Python class name does not. Stored
  `conditions` still reads, and the old keyword/property warns until 0.5.
- **Matches.** Players in their seats under fixed `config`, played for
  `games`: the unit Etude schedules, resumes and resamples. A tournament's
  `Seating` makes one match per config. Seat swaps belong
  inside `play`. Group `all` holds every seat; a player may sit in two seats.
- **Game processes.** Matches are played in fresh processes, as many at once
  as the run has CPUs, by affinity and container quota, and never more than
  there are matches. `evaluating` has no setting for the number; a job that
  declares [agents](#agents) has it sized for them too.
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
  evidence: return `Result(tables=..., blobs=[...])` (`etude.eval.match`)
  and name each blob in a row by `hashlib.sha256(blob).hexdigest()`.
  `read_blobs(db, store, digests)` gives them back, verified.
- **Rollups.** A summary you will query often is declared once and summed
  as each match is stored, never from raw rows again:

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
  is stored, and the run stops and ends failed. When a match cannot be
  played, let `play` raise: an error caught and returned as empty tables is
  still refused (`match_has_no_rows`), never stored as a result.
- **Environments are never pooled.** An eval run has one environment;
  resuming with another is refused, and a report shows each value from one environment.
- **Resume.** Outside a launch, reopen the same eval run with
  `evaluating(..., resume=True)` and call `run.play(play)` to carry on.
  A launched retry resumes automatically. Matches already delivered to any
  of `storages` are skipped, even on a machine with an empty disk.
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
  | `Inference(source=net, config=Direct())` | the held checkpoint, playing with that config |
  | `Code(name="heuristic", ...)`, or `Inference(...).player(ref)` | the same fixed player for every checkpoint: code, or one chosen checkpoint such as the initialization or a frozen anchor |
  | `Earlier(clock="updates", by=400, inference=direct)` | the held checkpoint's line exactly 400 updates before it |
  | `Fraction(clock="updates", value=0.5, inference=direct)` | its line at exactly half the held checkpoint's updates (3,000 for 6,000) |
  | `Fraction(clock="running_hours", value=0.5, at_or_before=True, inference=direct)` | its line's latest checkpoint at or before half the held checkpoint's running hours |
  | `Earlier(clock="running_hours", by=2, at_or_before=True, inference=direct)` | its line's latest checkpoint at or before two running hours before it |
  | `At(clock="updates", value=200, inference=direct)` | its line at update 200 |
  | `Final(inference=direct)` | its line's last checkpoint, once its run has ended |
  | `Aligned(clock="updates", job="small", inference=direct)` | job `small` at exactly the held checkpoint's updates |
  | `Aligned(clock="running_hours", job="small", at_or_before=True, inference=direct)` | job `small`'s latest checkpoint at or before the held checkpoint's running hours |

  The remaining rows are relative seats: each names a checkpoint by where it
  stands relative to the one held, and plays it with its own `inference`.
  `Earlier`, `Fraction`, `At` and `Final` look in the held checkpoint's own
  line unless given a `job`: `Final(job="small", inference=direct)` seats
  job `small`'s final checkpoint, `At(clock="updates", value=0, job="small",
  ...)` its initial one. `Aligned` always names a job, as the report names it
  ([tracking](tracking.md#reports)): its launch name, or the full name with
  its config digest. Leave `job` out to mean the held checkpoint's own line.
  A job seat fills from each of that job's lines (see Seeds). A hold finds relative seats' checkpoints among
  its `lines`, the experiment's training lines (`observe(source,
  experiment).lines`), held or not; `etude eval` and `etude launch` pass them themselves.
  A seating against a fixed player and one against a relative seat are held,
  scored and drawn alike. A seating with a relative seat needs an inference
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

  Held for a checkpoint of the job a seat's `job` names, a seating plays
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
- **Seeds.** A seating never names a seed. `games` is per config and
  point, a position of the held checkpoint's job: by default the point
  plays every pair of a seed of that job and a seed of the other seat's
  (its own line, or a fixed player, counting as one), the games split
  evenly over the pairs, the remainder to the lowest by seed, and pooled.
  A job seat waits while any of its seeds may still save there. A seed is a
  line, with the seed its first run declared. `seeds="one"` plays all of a
  point's games between one seed per side, drawn from the seating's name
  and the seed sets, the same pair at every point: faster, but the curve
  shows one pair of seeds, not the jobs. A seed added later changes the
  split, so that job's points are played again and count only the new one.
- **Pairs of checkpoints.** A seating with no inference seat is played once,
  not per checkpoint, so chosen checkpoints can meet: seat each as
  `Inference(...).player(ref)`, the player an inference seat would make of it.
  Prefer a relative seat where one says the rule; hand-built pairs suit a
  rule none states. Hold such a benchmark in batch whenever more checkpoints
  appear; each pass plays only the pairs not yet delivered. A hold on the go
  leaves these seatings to batch. Their rows are `schedule` rows with no
  `checkpoint`; `scores` follows only seatings with an inference seat.
- **Round robins.** `evaluating(round_robin, enter=players)` plays every pair of
  the players given once per config; seat swaps belong inside `play`.
  `evaluating(round_robin)` then plays every pair of every entrant so far that no
  eval run delivered, such as a late entrant against the earlier ones.
- **Held for an experiment.** A tournament only decides which matches to
  play. `evaluating(..., experiment=...)` makes its eval run the experiment's, so
  its results sit beside the experiment's training runs and their
  checkpoints in the experiment's database.
- **Stored in its eval runs.** Each eval run `evaluating` opens keeps
  `tournament.json` in its own folder: the definition it was held with, and
  the checkpoint or entrants it added. A tournament is every eval run naming
  it, so a hold on a rented machine delivers its part like any other run, and
  many eval runs can grow one tournament at once.
- **Reuse.** A match is the same wherever it is played, so within one
  experiment and environment its results are reused by key: `scores`,
  `standings` and a batch hold count a scheduled match that any of the
  experiment's eval runs in the environment holds, whether held for this
  tournament, another, or none. Each counts once, from the eval run that
  recorded it last.
- **Conflicts.** Another format, environment or round-robin rules under one
  name (`tournament_conflict`), or a seating name defined two ways
  (`seating_conflict`), is refused by a batch hold, a pass, and `scores` and
  `standings`; `statsdb` reports it in the `tournaments` table. Reading is
  not refused: where a later revision of an experiment file defines a
  tournament otherwise than its eval runs were held with, `etude report`
  draws what they recorded and says so.
- **Run names.** An eval run is named for the tournament and what is given,
  or, played by an [evaluation job](#evaluation-jobs), for the tournament and
  its matches, so a retry reopens and resumes it, finding the same checkpoint
  (by its key) wherever the retry put it.
- **Splitting.** `share=Share(index=k, count=n)` plays part k (from 0) of
  n disjoint parts, for n machines: contiguous runs of the scheduled matches
  in play order, so parts differ by at most one match and each seats only
  some of the checkpoints. The cut covers delivered matches too, so a
  retried share keeps its part. `tournament.json` stores it as
  `{"index": k, "count": n}`; an `[k, n]` pair written by etude 0.3.0
  still reads.
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
- **`standings`** gives a round robin's payoff matrix, best first, each entry
  naming its player and opponent; `complete` says whether every scheduled
  match is held.

## Evaluation jobs

```python
# ppo.py: experiment "ppo"
from decimal import Decimal

from etude.jobs import EvalJob, ExperimentState

from arena import play, progress  # play(match) -> Tables; progress is a Benchmark


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    finals = [run.final for run in state.runs if run.final]
    return [
        EvalJob(
            play=play,
            tournament=progress,
            checkpoints=finals,
            max_hours=0.5,
            max_usd=Decimal(1),
        )
    ]
```

```sh
etude eval ppo.py --source s3://my-bucket/runs     # play here
etude launch ppo.py --rent cpu8                    # get a machine to play
```

An eval job is one play function, one tournament and the checkpoints to play
it for. It is the same kind of thing as a training job: one function called
with one Pydantic value, written as a typed `Job`
([Typed jobs](launcher.md#typed-jobs)). `eval_jobs` returns `EvalJob`s (one,
or a list); anything else is refused with an error naming `eval_jobs` and the
type it takes.

- **It is named by its tournament.** An `EvalJob` takes no `name`: the
  benchmark's or round robin's name is the job's.
- **The tournament is a `Benchmark` or `RoundRobin` you have already built.**
  Keep a function such as `arena(config: ArenaConfig) -> Benchmark` if a
  config helps you build it, and call it yourself:
  `tournament=arena(ArenaConfig())`. Passing the function itself is refused
  with a `LauncherError` saying to call it.
- **`checkpoints=` are the checkpoints a benchmark is held for**, chosen from
  the state. `checkpoint=` names one. A round robin takes `enter=` players
  instead.
- **`experiment=` holds it for another experiment's runs.** Without it the
  job belongs to the file's experiment.

On the machine Etude opens `evaluating(tournament)` and calls `run.play(play)`,
with `play` imported by name from your checkout and every checkpoint member
delivered as an input verified by digest. The record keeps its checkpoint key,
and its delivered folder or file is each player's `checkpoint.path`, which `play`
opens. A single-file `Input` is also accepted. The job's environment needs
`etude-sdk[eval]`; the rest is as for any [typed job](launcher.md#typed-jobs).

Eval jobs that can run together share a launched run
([Experiments](#experiments)). Its tournaments play at once in one set of
game processes, and Etude decides how many games are active
([Agents](#agents)); an eval job has no setting for it.
Each writes its own eval run below the launched run's folder
(`capacity/evals-1a2b3c4d5e6f/capacity/ladder-3c4d5e6f7a8b/`). Their tables
reach the machine's storage with the run's other files and `statsdb` finds
them there, by tournament and match as before. A launched run reads no other
eval run: the pass that launched it decided which matches are new and gave it
exactly those.

### Agents

```python
# arena_game.py: the game. It imports no model library.
from collections.abc import Sequence

from pydantic import BaseModel

from etude.eval.agents import Agent
from etude.eval.records import Match
from etude.eval.result import Tables


class Deal(BaseModel):
    deal: int


def play(match: Match[Deal], agents: Sequence[Agent]) -> Tables:
    plays = []
    for game in range(match.games):
        state = deal(match.config.deal, seed=match.seed + game)  # your game
        while not state.over:
            # One row asks for one move. The row carries its own seed.
            row = {"view": state.view(), "seed": state.seed}
            [action] = agents[state.seat].act([row])
            state = state.step(action)
        plays += [
            {"game": game, "seat": seat, "score": state.score(seat)}
            for seat in range(len(match.players))
        ]
    return {"plays": plays}
```

```python
# arena_models.py: how a player is loaded. It may import Torch.
from collections.abc import Sequence
from typing import TypedDict, cast

import numpy as np
import numpy.typing as npt

import torch
from pydantic import BaseModel

from etude.eval.agents import Agent
from etude.eval.records import CheckpointPlayer, Code, Player


class Loading(BaseModel):
    device: str = "cuda"


class Observation(TypedDict):
    view: npt.NDArray[np.float32]
    seed: int


class Net:
    def __init__(self, player: CheckpointPlayer, config: Loading) -> None:
        self.device = config.device
        self.model = read_model(player.checkpoint.path).to(self.device)  # your code

    def act(self, rows: Sequence[object]) -> Sequence[int]:
        # The game constructs this pickled shape; its typing belongs at this boundary.
        observations = [cast(Observation, row) for row in rows]
        views = torch.stack([torch.from_numpy(row["view"]) for row in observations])
        with torch.no_grad():
            logits = self.model(views.to(self.device)).cpu()
        # Each row samples with its own seed, so its answer is the same in any batch.
        return [
            int(torch.multinomial(each.softmax(0), 1, generator=seeded(row["seed"])))
            for each, row in zip(logits, observations, strict=True)
        ]


def load(player: Player, config: Loading) -> Agent:
    if isinstance(player, Code):
        return Heuristic(player.name)  # code players are agents too
    return Net(player, config)
```

```python
# capacity.py, the experiment file under Experiments below, its two jobs declaring agents
from etude.jobs import Agents, EvalJob, ExperimentState

from arena_game import play
from arena_models import Loading, load


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    ...
    net = Agents(load=load, config=Loading(device="cuda"))
    return [
        EvalJob(play=play, tournament=ladder, checkpoints=rated, agents=net, max_hours=1),
        EvalJob(play=play, tournament=against_small, checkpoints=large, agents=net, max_hours=1),
    ]
```

```text
$ etude eval capacity.py --source s3://bucket/etude/launch
capacity: state from s3://bucket/etude/launch: 6 training runs, 2 eval runs
etude: agents: started for ladder, against small; 7 games at a time
etude: agents: loaded heuristic
etude: agents: loaded large@0/updates-00001000
...
etude: agents: stopped; 9 players loaded, 41280 rows in 1612 batches
eval ladder: 48 new, 0 running, 0 failed, 48 played
eval against small: 24 new, 0 running, 0 failed, 24 played
```

You write two functions: the game, and how a player is loaded. Etude runs
everything between them. Games are played in light processes that import only
`play`'s module. One agent process holds the loaded players and answers every
game's moves, batching the rows that ask for the same player, whichever game,
match or tournament they come from. The same happens wherever the games are
played: `etude eval` here, a smoke, or a launched run on its machine.

Declaring agents is optional. An `EvalJob` with no `agents=` calls
`play(match)`, which does all the playing itself in the same game processes,
each loading whatever weights it needs. That is the simpler choice for code
players and small models on CPUs. Agents are for a model worth loading once:
one on a GPU, or weights too large to hold in every game process.

- **An agent is anything with `act(rows)`.** It takes a sequence of rows and
  returns one answer per row, in order. `Agent` is a protocol, not a base
  class, and nothing else is asked of it.
- **Each row is answered as if alone.** Which rows share a batch depends on
  timing, so an answer must not: a row that needs randomness carries its own
  seed, and `act` uses that seed for that row only. Etude cannot check this.
  It keeps which rows shared each batch (see "Kept with the run" below), so
  a result that differs between two runs can be traced.
- **`agents` is one agent per seat**, in the order of `match.players`.
  `agents[seat].act(rows)` sends the rows straight to the agent process and
  waits for the answers. A game that needs several answers at once, such as
  the leaves of a search, sends them as one call. A player in two seats is
  one loaded agent.
- **Rows and answers are picklable values**: NumPy arrays, numbers, text,
  and tuples, lists and dicts of them. A type from a model library would make
  every game process import that library.
- **`load` is called when a player is first used**, in the agent process,
  with the config the job gave, and again only if that player was dropped
  for memory (below). It is called for every
  player seated, a `Code` player included. A checkpoint player's folder or
  file is at `player.checkpoint.path`: the agent process runs on the machine
  that plays the games, and has no setting for where it runs. Like `play`,
  `load` is imported by name, so define it at the top of a module.
- **Test a game without Etude.** Both sides are the same protocol, so
  `play(match, [load(player, Loading()) for player in match.players])` plays
  a match in one process.
- **Batching** is per player. A batch is answered when it holds `max_batch`
  rows (default 32) or when its oldest row has waited `max_delay_seconds`
  (default 0.005). One call's rows always arrive in one batch, even when
  there are more than `max_batch`. Set either on `Agents(...)`.
- **Memory.** `memory_gb` on `Agents(...)` bounds what the loaded agents may
  hold, counting each by its checkpoint's size on disk and a code player as
  nothing. The default is half the memory the run may use. Over the bound,
  the least recently used agent with no row waiting is dropped, and loaded
  again if a game asks for it. Matches are ordered so those sharing
  checkpoints are played together (see "Order" above). The bound counts host
  memory, not a GPU's: on a small device, set it to what the device holds.
- **Active games are Etude's to decide.** One game per CPU the run may use,
  by affinity and container quota, less one for the agent process; never
  more than there are matches left, or than fit in memory beside the agents
  at 256 MiB a game. A game process gets one thread, the agent process the
  rest. The line that starts the agents says how many.
- **Shared by jobs that declare the same agents.** Eval jobs of one pass
  whose `Agents` are equal share a run and its agent process. Jobs with
  different `Agents`, and jobs with none, go in separate eval runs.
- **`play` takes what the job declares.** `play(match)` with `agents=`, or a
  second parameter without it, is refused where the job is written.
- **Started only when needed.** A run with nothing to play starts no agent
  process. Each launched run starts its own: a second eval run placed later
  on the same machine does not reuse the first's.
- **Stopped whatever happens.** Etude stops the agent process after the last
  game, after a failure, and when the run is stopped or cancelled (SIGTERM).
- **Failures.** A game never has to notice a dead agent process: `act`
  raises for it, and Etude records the match.

  | What happened | Matches | Run |
  | --- | --- | --- |
  | the agent process exits | unfinished ones failed with `retry=True` | failed |
  | `load` raises | each match that asks for that player failed with `retry=False`; the player is not loaded again in this run | failed |
  | `act` raises, returns the wrong number of answers, or one that does not pickle | the matches with rows in that batch failed with `retry=False` | failed |
  | `load` or `act` raises `etude.errors.RetryRequested` | failed with `retry=True` | failed |
  | `play` raises | failed with `retry=False` | failed |
  | the run's time limit, SIGTERM, or a failed delivery | unfinished ones failed with `retry=True` | failed |

  The next pass plays `retry=True` matches again by itself; `retry=False`
  ones wait for `--retry-failed`. As for any failed match, a tournament
  stops at its first `retry=False` failure while the others finish
  ([Experiments](#experiments)), so a `load` that raises fails the first
  match of each tournament that asks for the player. Raise `RetryRequested`
  for a refusal that is the machine's fault, such as a device out of memory.
  Rows saying a game was invalid remain rows: raise instead of returning
  them. A run with a failed match is never recorded as succeeded.
- **Light game modules.** A game process imports the module that defines
  `play`, not the experiment file (unless `play` is defined there) and not
  `load`'s module. Keep the game in a small module, as `arena_game.py`
  above. For a job with agents, Etude counts what each game process imported
  at startup and warns once per run if that includes `torch`, `jax` or
  `tensorflow`, or exceeds 256 modules, naming the importing module and
  saying to move the import to `load`'s.
- **Kept with the run.** `artifacts/agents/batches.json` below the launched
  run lists each batch: its player, and the tournament and match of each
  row. The startup import counts are kept beside it. `ETUDE_SCRATCH_DIR` and
  `TMPDIR` point to temporary scratch (`/dev/shm` when available) in the
  game and agent processes.
- **Results do not depend on it.** A match is its seats, config and game
  count, however it was played: changing `Agents`, or adding or removing
  them, plays no delivered match again. Return what played a game in
  `Result(execution=...)` to keep it with the result.

## Experiments

An experiment is one file, and the experiment is named for it. The file
declares its work in three functions with fixed names:

```python
def training_jobs() -> list[TrainingJob]: ...  # may take state: ExperimentState
def eval_jobs(state: ExperimentState) -> list[EvalJob]: ...
def report() -> Report: ...
```

`eval_jobs(state)` says what to evaluate given what exists. A pass over the
file plays whatever that calls for and no eval run has delivered. Run one
while training goes, after it ends, or with a seating added later: each pass
looks again. `capacity.py` evaluates the training runs of experiment
`capacity`, and its eval runs are that experiment's; its `training_jobs()`
declares those training runs, so `track()` in the training code needs no
`experiment`.

```python
# capacity.py: jobs "small" and "large", a checkpoint every 200 updates
from etude.eval.records import Aligned, Benchmark, Earlier, Final, Fraction, Seating
from etude.jobs import EvalJob, ExperimentState

# direct is an Inference, heuristic Code, deals the configs
from arena import deals, direct, heuristic, play, world


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    rated = [c for run in state.runs for c in run.checkpoints.every(5)]  # every 1,000 updates
    ladder = Benchmark(
        name="ladder",
        environment=world,
        seatings=(
            Seating(name="vs heuristic", players=(direct, heuristic), configs=deals, games=8),
            Seating(
                name="vs previous",
                players=(direct, Earlier(clock="updates", by=1_000, inference=direct)),
                configs=deals,
                games=8,
            ),
            Seating(
                name="half-training",
                players=(
                    direct,
                    Fraction(clock="running_hours", value=0.5, at_or_before=True, inference=direct),
                ),
                configs=deals,
                games=8,
            ),
        ),
    )
    # Held only for large's checkpoints: held for small's, they would play small against itself.
    against_small = Benchmark(
        name="against small",
        environment=world,
        seatings=(
            Seating(
                name="large vs small",
                players=(direct, Aligned(clock="updates", job="small", inference=direct)),
                configs=deals,
                games=8,
            ),
            Seating(
                name="large vs final small",
                players=(direct, Final(job="small", inference=direct)),
                configs=deals,
                games=8,
            ),
        ),
    )
    large = [c for run in state.runs if run.job == "large" for c in run.checkpoints.every(5)]
    return [
        EvalJob(play=play, tournament=ladder, checkpoints=rated, max_hours=1),
        EvalJob(play=play, tournament=against_small, checkpoints=large, max_hours=1),
    ]
```

```sh
etude eval capacity.py --source s3://bucket/etude/launch               # play here
etude eval capacity.py --source s3://bucket/etude/launch --watch 600   # and again every ten minutes
etude launch capacity.py --rent cpu8                # get machines: training, then eval
etude launch capacity.py --rent cpu8 --watch 600    # and again every ten minutes
etude queue capacity.py --pool lab                  # record the work; it starts when a machine is free
```

```text
$ etude launch capacity.py --rent cpu8
capacity: state from s3://bucket/etude/launch: 6 training runs, 2 eval runs
capacity/small@0  finished
capacity/large@0  running
...
eval ladder: 48 new, 120 running, 0 failed, running capacity/evals-1a2b3c4d5e6f
eval against small: 24 new, 0 running, 0 failed, running capacity/evals-1a2b3c4d5e6f
```

The commands divide by who does the work:

| | Does the work on this computer | Gets machines to do it |
| --- | --- | --- |
| Training | `etude train FILE` | `etude launch FILE`, `etude queue FILE` |
| Eval | `etude eval FILE` | `etude launch FILE`, `etude queue FILE` |

`etude eval` only plays here: it takes `--source`, `--watch`,
`--retry-failed` and `--experiment`, and no pool or on-demand. `etude launch`
and `etude queue` read both declarations and submit what has not been run for
both stages, with the same three flags beside their own
([One pass over the file](launcher.md#one-pass-over-the-file)). A file with
only one of the declarations does only that stage.

`etude smoke capacity.py` runs the file's `training_jobs()`, `eval_jobs(state)` and `report()` in
smoke form on this computer first, one game per match against tiny
checkpoints. `etude launch --smoke` and `etude queue --smoke` do so
before they spend ([Smoke before you spend](launcher.md#smoke-before-you-spend)).

`etude queue` records the eval run a pass hands down even when the selected
pool or on-demand is busy. Queued matches stay in flight, so later passes do
not submit them again. The scheduler starts them when a machine has room,
with no later pass: keep `etude server run` or `etude wait` running so its
loop does. A queued run that fails stops a later pass that wants its
matches, as a launched one does, unless `--retry-failed`.

- **Where state is read.** `--source` when given. Otherwise where the work is
  delivered: this computer's runs directory for `etude eval`, `etude train`
  and a target without storage (the `local` pool), and the target's storage
  for a pool or on-demand that has one. A target whose machines name
  different storages, or a server reached over SSH, needs `--source`; the
  error says so. Each pass prints the source it read. The machines that play
  must deliver to that source, or a later pass reads their matches as
  unplayed.
- **State.** What the experiment's runs have stored, together with the
  scheduler's records of those queued or only just started, read fresh each
  pass and never stored itself. Summary names are local (`base@0`,
  `ladder-1a2b3c4d5e6f`) in both cases; `state.run("capacity/base@0")` and
  `state.run("base@0")` find the same training run. `state.eval_runs` is its eval runs, each with
  its name and whether it is `queued`, `running`, `stopped`, `finished` or `failed`. `state.runs` is
  its training runs under the source: each one's name, its `job` (the name the report gives it: the
  launch name, with its config digest when one name ran two configs),
  whether it is `queued`, `running`, `stopped`, `finished` or `failed`,
  and the checkpoints it stored, each with its ordinal, `at` (where its
  run's clocks stood), tags and `wall_ms`, when it was saved in milliseconds
  since the epoch.
  `stopped` follows the [same state rule as training](launcher.md#one-pass-over-the-file).
  A stopped eval is never resumed and needs no action: its undelivered matches
  are new on the next pass and play in a new run, without `--resume`.
  `Checkpoints` is a tuple, so a slice works too; `every(n)`, `at(clock,
  *values)` and `tagged(tag)` choose by ordinal, clock value or tag, and
  `final` is the last checkpoint once a run has finished.
- **What `eval_jobs` returns.** Every evaluation job the experiment should
  have run or can run by now, each holding a benchmark for the checkpoints it
  chose: the whole list on every call, not only what is new. The pass works
  out what is left to play. A relative seat finds its checkpoint among every
  run under the source, held or not: `Earlier(by=1_000)` above seats the
  checkpoint 1,000 updates back whether or not `every(5)` chose it. Seat a
  chosen checkpoint with `checkpoint.player(inference)` to make a seating of
  fixed players, played once rather than per checkpoint. `tags` mark
  seatings for readers, such as pairs a rating fit leaves out.
- **Other experiments' checkpoints.** A file may define only `eval_jobs`.
  Read a producer with `observe(source, "producer")` and give its checkpoint
  records to a benchmark's `checkpoints=`, or use
  `checkpoint.player(inference)` in a round robin's `enter=`.

  ```python
  def eval_jobs(state):
      checkpoint = observe(source, "producer").run("producer/base@0").final
      return EvalJob(tournament=ladder, play=play, checkpoints=(checkpoint,), max_hours=1)
  ```

  Import `observe` from `etude.experiment`. It lists the producer's source
  on every pass, including each `--watch` round.
  The evaluating experiment's state need not contain the producer. `eval`,
  `launch` and `queue` use the checkpoint artifact's location and verify
  its digest and byte length; folder checkpoints verify every member.
  Two different artifacts with the same checkpoint key are refused with
  `checkpoint_key_conflict`, naming the key and both locations, including
  when an earlier delivered evaluation seated the other artifact.
  Local evaluation inherits this computer's environment, including API keys;
  it fetches the checkpoints here and rents nothing.
- **A pass.** For each job, the matches exactly what `eval_jobs` returned calls
  for, the seatings it gave for the checkpoints it gave, that no eval run of
  the experiment in its environment delivered. `etude eval` plays them here
  and delivers them to the source. `etude launch` and `etude queue` submit
  one eval run to play exactly those, with each checkpoint they seat as an
  input from its training run's folder.
- **One run for the pass.** The new matches of eval jobs that can run
  together go to one machine as one run, `capacity/evals-1a2b3c4d5e6f`, so a
  pass with a few matches for each of several tournaments rents once, and the
  jobs share their game processes and their [agents](#agents). Every
  tournament's line names that run. The run's `max_hours` and `max_usd` are
  the sums of its jobs', giving the concurrent tournaments a conservative
  combined allowance. Jobs that could not run together get a run each: those
  that differ in `agents`, checkout or revision, `runtime`, `prepare`,
  `setup`, `cleanup`, environment variables, stop or cleanup allowance, or
  `max_attempts`.
- **Waiting for useful machine time.** `etude eval` always plays.
  `etude launch` and `etude queue` hand a group down when a machine that
  already exists has room after this pass's training runs, when its estimated
  work reaches `--min-eval-hours H` (default: 2 hours), or when no training
  run of the experiment is queued or running.

  The estimate is each tournament's recorded seconds per game, multiplied by
  its new games, summed over the run and divided by the CPUs of the machine
  the pool or on-demand would give it, since that many games play at once.
  Where the pass cannot tell the machine's CPUs it does not divide, so it
  hands the run down sooner. With no timing history for any member, there is
  no estimate and the
  pass hands it down so a new tournament can establish its pace. This is a
  work estimate, not a price quote or a guarantee across different hardware.

  Otherwise each tournament says what waits:

  ```text
  eval ladder: 12 new, 0 running, 0 failed, waiting: 24 eval matches, estimated 0.75 machine hours below --min-eval-hours 2; no machine has room and training is running
  eval against small: 12 new, 0 running, 0 failed, waiting: 24 eval matches, estimated 0.75 machine hours below --min-eval-hours 2; no machine has room and training is running
  ```

  Waiting matches are not queued: a later pass finds them new again.
  `--watch` makes those passes; `--quote` prints the same decision without
  submitting. `--min-eval-hours 0` admits any amount of work.
- **Not twice.** A pass decides from two places only: the state, and the
  launcher's own records of the experiment's runs, the ones `etude runs` and
  `etude status` show. A launched eval run is named for its matches
  (`capacity/evals-1a2b3c4d5e6f`) and its record carries them, so a later
  pass neither launches it again nor plays its matches while it is queued or
  running. Nothing else remembers a pass: there is no file to lose or repair.
  A pass leaves a failed run's undelivered matches alone, hands down
  unaffected training runs and matches, and prints every decision before
  stopping with `launched_pass_failed`. A watch ends there; look, then
  `--retry-failed`. What the failed run delivered is kept. `--quote` shows
  the held run and retry flag without failing.
- **While it runs.** `etude status RUN` gives the matches played, over all
  of the run's tournaments, the recent pace and the time left;
  `etude logs --run RUN` the job's recent output.
  Each completed match is sealed and offered to storage immediately, so `etude report` and
  `statsdb` read what a running pass has played and mark it partial (the
  report says so; `statsdb`'s `runs.state` is null until the run ends). A
  killed pass keeps every block it delivered and stays partial.
- **Failures.** A match that raised, was refused or stored no rows is
  failed, never played: `etude status RUN` counts it (`12/80 matches (1
  failed)`), its tournament stops there, and every later pass counts it
  (`ladder: 0 new, 1 failed`) and does not play it again, so a result is
  never quietly replaced. The other tournaments keep playing; the run then
  ends failed. `--retry-failed` plays the failed matches again. A match the
  machine failed rather than the game, such as one unfinished at the run's
  time limit or when its [agent process](#agents) exited, is failed with
  `retry=True` (`matches.retry`) and the next pass plays it again without
  the flag. A match an
  older run stored as done with no rows counts as failed too, so its games
  are asked for again rather than silently missing.
- **Without playing.** `etude.eval.tournament.played` answers what a pass
  would find: each match the definition calls for, as delivered (with the eval
  run that delivered it last, whose rows count), failed (with nothing
  delivered since) or new. `etude report`
  reads it.
- **Changing the design.** A new seating or benchmark is played for every
  checkpoint chosen, including those of finished runs, and a match another of
  the experiment's tournaments already played is reused. A seating `eval_jobs`
  stops returning is no longer played; what it played stays. Redefining a
  seating under the same name is refused (`seating_conflict`): give the new
  one a new name. A match is its seats, config and game count, so raising
  `games` plays every match of the seating again.
- **Older eval runs.** Benchmarks stored by etude-sdk 0.2, with `matchups`
  and each match's `matchup`, read as seatings with the same definitions, so
  nothing is played again.

`etude report capacity.py` draws the experiment's evals beside its training
([tracking](tracking.md#reports)), following the file's `report()`. Each
checkpoint player is drawn where its latest seat says it stood, else, found by its
checkpoint's key, where its training run stood when it saved it; a checkpoint
no training run recorded is drawn in its line's curve. Code and checkpoints
with no position are anchors, drawn once. A checkpoint `eval_jobs(state)` chose that no
eval run has delivered is marked not yet played, one nothing played is marked
not evaluated, and failed matches are listed. A seating with a relative seat
is drawn over the checkpoints it was held for; one whose seat has no
checkpoint yet is a gap that says so. Each match counts from the eval
run that delivered it last, whichever commit its code was at. Where the
matches were played at several commits (`seats.commit`), "Played at" offers
each: that commit's view counts every match as played at it. A delivered
match is not played again for a new commit, so a commit's view holds the
matches first played at it, and any held again there.

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
rollup, and never its blobs. A rollup's totals are added to, never summed
again from earlier blocks. `read_blobs` reads each pack's adjacent blobs in one range,
kept on this computer, so asking again reads nothing. The store stays the authority: `statsdb(..., rebuild=True)`
reads everything again, and a database from another Etude schema is rebuilt
on its own. One process opens it at a time; another gets `statsdb_in_use`.
Experiments are never joined: each has its own database.

`compare(db, table=, value=, per=, where=, by=)` sets each subject against
each opponent and against all: a seat's player against the other seats'
players, or a group against the match's other groups. The value is
`sum(value) / sum(per)`, or the mean without `per`. The 95% interval is a
percentile bootstrap over whole matches with a fixed seed, independent of
the order rows are read in; fewer than two matches have none.

`estimate` answers many ratios over the same resampling units, such as deals,
with one bootstrap, as a page of per-card rates over the same deals needs:

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
