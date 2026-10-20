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


# Model is the checkpoint being held, playing with your config (a Pydantic model or JSON).
progress = Benchmark(
    name="anchors",
    environment=world,
    matchups=(
        Matchup(
            name="no search vs heuristic",
            players=(Model(config=Direct()), heuristic),
            conditions=deals,
            games=8,
        ),
        Matchup(
            name="no search vs greedy",
            players=(Model(config=Direct()), greedy),
            conditions=deals,
            games=8,
        ),
        Matchup(
            name="search vs no search",
            players=(Model(config=PUCT(simulations=64)), Model(config=Direct())),
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
    print(line.matchup, line.run, [(p.ordinal, p.clocks, p.value) for p in line.points])
for entry in standings(db, "finals-1", table="plays", value="score").entries:
    print(entry.name, entry.opponent, entry.value, entry.interval)
```

```python
db.sql("""select a.name, avg(score) from plays
          join seats using (run, match, player) join agents a using (agent)
          where on_play and opening_lands >= 2 group by all""").show()
```

```sh
uv run etude reports eval --open   # every eval run under ~/.etude/track
```

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

- **Agents.** A checkpoint (by artifact `sha256`) or code (by its source files,
  hashed relative to the repository), plus a config: free JSON or a Pydantic
  model. The identity covers both, so one checkpoint at two search budgets is
  two agents. Names are for reading; joins use identity.
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
  entrants once per condition; seat swaps belong inside `play`.
- **Stored.** A tournament lives under `tournaments/NAME/` beside the runs, as
  files written once and never replaced, so many eval runs can grow it at
  once. Another format or environment under the same name is refused, as is
  a matchup name defined again differently. No run may be named `tournaments`.
- **Splitting.** `share=(k, n)` plays one of n disjoint parts, for n
  machines. The same arguments resume the same eval run.
- **Cost.** Every hold lists the tournament's folder once. Holding a new
  checkpoint or new entrants reads nothing more of it; any other hold also
  lists each of its eval runs' matches, to skip what any of them delivered.
- **`curves`** gives one line per matchup and training run: each checkpoint's
  ordinal, the clocks where its run stood when it saved it, and the first
  player's value with a 95% interval over whole matches.
- **`standings`** gives a round robin's payoff matrix, best first; `complete`
  says whether every scheduled match is held. Each match counts once, from
  the eval run that recorded it last.

## Querying

`connect` gives each of your tables across every eval run, beside Etude's own:
`runs`, `agents` (with a `config.KEY` column per scalar config key, dotted when
nested), `matches`, `seats`, `groups`, `tournaments`, `schedule` (every match
a tournament calls for, with its matchup and checkpoint) and
`training_checkpoints` (every checkpoint a training run under the source
recorded, joined to agents by `artifact`).

`compare(db, table=, value=, per=, where=, by=)` sets each subject against
each opponent and against all: a seat's agent against the other seats'
agents, or a group against the match's other groups. The value is
`sum(value) / sum(per)`, or the mean without `per`. The 95% interval is a
percentile bootstrap over whole matches with a fixed seed; fewer than two
matches have none.
