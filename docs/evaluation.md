# Evaluation

Play agents against each other in parallel workers, hand Etude whatever tables
your game code produces, and query them with DuckDB. Tournaments arrange those
eval runs: each checkpoint of a model through a benchmark's matchups as it trains, or
hand-picked models in a round robin. Evaluation is independent of training.

```sh
uv add 'etude-sdk[eval]'   # DuckDB, for writing and querying tables
```

```python
from etude.eval.match import Tables, agent, environment
from etude.eval.query import connect
from etude.eval.records import Benchmark, Match, Matchup, Model, RoundRobin
from etude.eval.tournament import curves, hold, standings

world = environment("allies-vs-lessons", files=["game/"], config={"world": "w6"})
deals = tuple({"deal": 1_910_101_000 + d} for d in range(25))
heuristic = agent("heuristic", code=["players/heuristic.py"])
greedy = agent("greedy", code=["players/greedy.py"])


def play(match: Match) -> Tables:  # importable by name: workers are fresh processes
    results = play_deal(
        match.agents, deal=match.conditions["deal"], games=match.games, seed=match.seed
    )
    return {
        "plays": [
            {
                "game": r.game,
                "player": r.seat,
                "score": r.score,
                "on_play": r.on_play,
                "opening_lands": r.lands,
            }
            for r in results
        ],
        "games": [{"game": r.game, "group": "all", "turns": r.turns} for r in results],
    }


# Model is the checkpoint being held, run by your inference code and playing with
# your config (a Pydantic model or JSON). Name the code, so a change to it is a new agent.
net = ["players/net.py"]
progress = Benchmark(
    name="anchors",
    environment=world,
    matchups=(
        Matchup(
            name="no search vs heuristic",
            players=(Model(code=net, config=Direct()), heuristic),
            conditions=deals,
            games=8,
        ),
        Matchup(
            name="no search vs greedy",
            players=(Model(code=net, config=Direct()), greedy),
            conditions=deals,
            games=8,
        ),
        Matchup(
            name="search vs no search",
            players=(
                Model(code=net, config=PUCT(simulations=64)),
                Model(code=net, config=Direct()),
            ),
            conditions=deals[:10],
            games=4,
        ),
    ),
)

# On the go: as each checkpoint is saved, one small eval run plays every matchup.
hold(progress, checkpoint=ref, play=play, storages=[bucket])

# Batch: add a matchup to the definition; holding with no checkpoint plays every
# missing match, so the new line is played for every checkpoint held so far.
hold(progress, play=play, workers=8, storages=[bucket])

# Batch: hand-picked models, each against every other.
league = RoundRobin(name="finals-1", environment=world, conditions=deals, games=8)
hold(league, enter=finals, play=play, workers=8, storages=[bucket])

db = connect(bucket)  # or "s3://my-bucket/runs", or a directory
for line in curves(db, "anchors", table="plays", value="score"):
    print(line.matchup, line.run, [(p.name, p.clocks, p.value) for p in line.points])
for entry in standings(db, "finals-1", table="plays", value="score").entries:
    print(entry.name, entry.opponent_name, entry.value, entry.interval)
```

```python
db.sql("""select a.name, avg(score) from plays
          join seats using (run, match, player) join agents a using (agent)
          where on_play and opening_lands >= 2 group by all""").show()
```

```sh
uv run etude reports eval --open   # every eval run under ~/.etude/track
uv run etude reports eval --watch 5 --open   # keep the page current as evals arrive
```

Watching waits for the first eval run when none exists yet, opens the page once,
and refreshes it at the requested interval. Ctrl-C stops regeneration. It reads
retained evaluation results; it does not launch jobs or allocate compute.

## Eval runs

`etude.eval.match.evaluate` is the general eval run: it plays the matches it
is given and knows nothing of tournaments.

```python
from etude.eval.match import evaluate

evaluate(
    experiment="mirror",
    environment=world,
    play=play,
    matches=[Match(agents=(a, b, c, d), groups={"north": (0, 2), "south": (1, 3)}, games=4)],
    workers=4,
    storages=[bucket],
)
```

- **Agents.** A checkpoint (by artifact `sha256`) or code (by its source
  files), plus a config: free JSON or a Pydantic model. A checkpoint agent
  should also name the code that runs it,
  `agent("net", checkpoint=ref, code=["players/net.py"], config=...)`. The
  identity covers all of them, so one checkpoint at two search budgets, or
  under changed inference code, is two agents. Names are for reading; joins
  use identity.
- **Source files.** A directory stands for the files below it. In a git
  checkout only tracked files count, named by their path in the checkout, so
  caches and scratch files never change an identity. A launched job's source
  has no `.git`; there files are named by their path below `ETUDE_SOURCE_DIR`,
  which the launcher sets, so the same commit gives the same identity on a
  rented machine as on yours.
- **Matches.** A seating of agents under fixed `conditions`, played for
  `games`: the unit Etude schedules, resumes and resamples. Seat swaps belong
  inside `play`. Group `all` holds every seat; an agent may sit in two seats.
- **Rows are yours.** `play` returns any tables. Each row names a `player`
  (a seat) or a `group` of its match; Etude adds `run` and `match`. Values are
  numbers, text, booleans or nulls.
- **Strict.** A row with neither or both of `player` and `group`, a seat or
  group the match lacks, or a column whose type changes during a run is
  refused. A refused or raising match is recorded as failed with its error,
  nothing else of it is stored, and the run stops.
- **Environments are never pooled.** An eval run has one environment;
  resuming with another is refused, and comparisons keep environments apart.
- **Resume.** Calling `evaluate` again with the same `run` skips every match
  already delivered to any of `storages`, even on a machine with an empty disk.
- **Progress.** Each stored match prints one line to stderr:
  `etude: RUN: 12/36 matches played, 220s elapsed`.
- **Storage.** Each match writes one Parquet file per table under
  `RUN/tables/` and its record under `RUN/matches/`, delivered after every
  match; `etude sync` finishes delivery later. On finish, a process's files
  for each table are merged into one, which replaces them once delivered.

## Tournaments

- **Benchmarks.** Named matchups, each its own line over training: `players`
  in seat order, each a fixed agent or `Model(config=...)`, with its own
  `conditions` and `games`. A matchup's value is its first player's.
  `hold(benchmark, checkpoint=ref)` puts the checkpoint (by `sha256`) in every
  model seat and plays each given matchup; with no checkpoint it plays every
  matchup ever defined for every checkpoint held, skipping what is delivered.
- **Round robins.** `hold(round_robin, enter=agents)` plays every pair of
  the agents given once per condition; seat swaps belong inside `play`.
  `hold(round_robin)` then plays every pair of every entrant so far that no
  eval run delivered, such as a late entrant against the earlier ones.
- **Stored in its eval runs.** Each eval run a `hold` starts keeps
  `tournament.json` in its own folder: the definition it was held with, and
  the checkpoint or entrants it added. A tournament is every eval run naming
  it, so a hold on a rented machine delivers its part like any other run, and
  many eval runs can grow one tournament at once.
- **Reuse.** A match is the same wherever it is played, so within one
  environment its results are reused by key: `curves`, `standings` and a
  batch hold count a scheduled match that any eval run of the environment
  holds, whether held for this tournament, another, or none. Each counts
  once, from the eval run that recorded it last.
- **Conflicts.** Another format, environment or round-robin rules under one
  name (`tournament_conflict`), or a matchup name defined two ways
  (`matchup_conflict`), is refused by a batch hold and by `curves` and
  `standings`; `connect` reports it in the `tournaments` table.
- **Run names.** Under `etude launch` the eval run is the launched run
  (`ETUDE_RUN`), so `etude logs` and `status` lead to it and a retry resumes
  it, finding the same checkpoint (by digest) wherever the retry put it. Elsewhere it is named for the tournament and what is given; the same
  call resumes it.
- **Splitting.** `share=(k, n)` plays one of n disjoint parts, for n
  machines: the scheduled matches in key order, dealt round, so parts differ
  by at most one match. Delivered matches are dealt too, so a retried share
  keeps its part.
- **Cost.** Holding a checkpoint or entrants reads nothing of the
  tournament: it plays their matches. Holding nothing new (batch) lists each
  place once and reads each eval run once (its `tournament.json`, or its
  `environment.json` when it was held for no tournament), to play what the
  tournament calls for and no eval run of its environment delivered.
- **`curves`** gives one line per matchup and training run: each checkpoint's
  name, ordinal, the clocks where its run stood when it saved it, and the
  first player's value with a 95% interval over whole matches. Checkpoints
  no training run under the source recorded make one line per matchup,
  ordered by name, numbers in order (`updates-900` before `updates-1000`).
- **`standings`** gives a round robin's payoff matrix, best first, each entry
  naming its agent and opponent; `complete` says whether every scheduled
  match is held.

## Evaluation jobs

```python
# eval.py
from decimal import Decimal

from etude.jobs import evaluation
from etude.launch.records import Input, Job

from arena import play, progress  # play(match: Match) -> Tables; progress is a Benchmark


def runs(checkpoint: str, sha256: str, size_bytes: int) -> Job:
    weights = Input(
        name=checkpoint,
        key=f"etude/launch/ppo-1/checkpoints/{checkpoint}",  # PREFIX/RUN/checkpoints/NAME
        sha256=sha256,
        size_bytes=size_bytes,
    )
    return evaluation(
        progress, play=play, checkpoint=weights, workers=8, max_hours=0.5, max_usd=Decimal(1)
    )
```

```sh
etude launch eval.py --rent cpu8 --checkpoint updates-500 --sha256 … --size-bytes 1048576
```

An evaluation job holds a tournament on a launched machine, and nobody writes
its command: Etude calls `hold` there with `play`, imported by name from your
checkout, and the checkpoint, which reaches the machine as the job's input,
verified by digest. The input's name names the checkpoint, and its file is the
`uri` of every model seat, which `play` opens. A round robin takes `enter=`
agents instead. The eval run is the launched run, so its tables reach the
machine's storage with the run's other files and `connect` finds them there.
Holding nothing new (batch) is refused: a launched job reads no other eval
run. The job's environment needs `etude-sdk[eval]`; the rest is as for any
[typed job](launcher.md#typed-jobs).

Next: evaluating as training goes, declared on the training job as
`training(..., evaluations=[evaluation(...)], every=5)`, with the controller
launching one evaluation job for every fifth checkpoint.

## Querying

`connect` gives each of your tables across every eval run, beside Etude's own:
`runs` (with the tournament each was held for), `agents` (with a `config.KEY`
column per scalar config key, dotted when nested), `matches`, `seats`,
`groups`, `tournaments` (with any `conflict` among its eval runs), `schedule`
(every match a tournament calls for, with its matchup and checkpoint) and
`training_checkpoints` (every checkpoint a training run under the source
recorded, joined to agents by `artifact`).

`compare(db, table=, value=, per=, where=, by=)` sets each subject against
each opponent and against all: a seat's agent against the other seats'
agents, or a group against the match's other groups. The value is
`sum(value) / sum(per)`, or the mean without `per`. The 95% interval is a
percentile bootstrap over whole matches with a fixed seed, independent of
the order rows are read in; fewer than two matches have none.
