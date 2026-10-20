# Evaluation

Play agents against each other in parallel workers, hand Etude whatever tables
your game code produces, and query them with DuckDB. Tournaments arrange those
eval runs: a model's checkpoints against fixed benchmarks as it trains, or
hand-picked models in a round robin. Evaluation is independent of training.

```sh
uv add 'etude-sdk[eval]'   # DuckDB, for writing and querying tables
```

```python
from etude.eval.match import Tables, agent, environment
from etude.eval.query import connect
from etude.eval.records import Match
from etude.eval.tournament import benchmark, hold, round_robin, standings

world = environment("allies-vs-lessons", files=["game/"], config={"world": "w6"})
deals = [{"deal": 1_910_101_000 + d} for d in range(25)]
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


progress = benchmark(
    "ppo-25k-anchors", environment=world, conditions=deals, games=8, benchmarks=[heuristic, greedy]
)

# On the go: as each checkpoint is saved, one small eval run plays it against the benchmarks.
hold(
    progress,
    enter=[agent("ppo@3", checkpoint=ref, config={"player": "search"})],
    play=play,
    storages=[bucket],
)

# Batch: a benchmark added later plays every checkpoint entered so far.
hold(progress, add_benchmarks=[puct64], play=play, workers=8, storages=[bucket])

# Batch: hand-picked final models, each against every other.
league = round_robin("finals-1", environment=world, conditions=deals, games=8)
hold(league, enter=finals, play=play, workers=8, storages=[bucket])

db = connect(bucket)  # or "s3://my-bucket/runs", or a directory
for entry in standings(db, "ppo-25k-anchors", table="plays", value="score").entries:
    print(entry.name, entry.opponent, entry.value, entry.interval, entry.checkpoint)
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

- **Formats.** A `benchmark` pairs every entrant with every benchmark; a
  `round_robin` pairs every two entrants once. Each pairing is one match per
  condition, entrant first.
- **Stored.** A tournament lives under `tournaments/NAME/` beside the runs, as
  files written once and never replaced, so many eval runs can grow it at
  once. Holding a tournament of the same name with other rules is refused; no
  run may be named `tournaments`.
- **`hold`** stores the tournament and the agents given, then plays, as one
  eval run, the matches they add. With none given it plays every scheduled
  match not yet delivered. `share=(k, n)` plays one of n disjoint parts, for n
  machines. The same arguments resume the same eval run.
- **Cost.** A hold that enters a new agent reads only the tournament's
  listings, however many matches it holds. Otherwise it also lists each of its
  eval runs' matches, to skip what any of them delivered.
- **`standings`** counts each scheduled match once, from the eval run that
  recorded it last. A benchmark gives each entrant against each benchmark and
  against all, in training order, with the training run and clocks of a
  checkpoint entrant. A round robin gives the payoff matrix, best first.
  `complete` says whether every scheduled match is held.

## Querying

`connect` gives each of your tables across every eval run, beside Etude's own:
`runs`, `agents` (with a `config.KEY` column per scalar config key, dotted when
nested), `matches`, `seats`, `groups`, `tournaments`, `entrants` and
`training_checkpoints` (every checkpoint a training run under the source
recorded, joined to agents by `artifact`).

`compare(db, table=, value=, per=, where=, by=)` sets each subject against
each opponent and against all: a seat's agent against the other seats'
agents, or a group against the match's other groups. The value is
`sum(value) / sum(per)`, or the mean without `per`. The 95% interval is a
percentile bootstrap over whole matches with a fixed seed; fewer than two
matches have none.
