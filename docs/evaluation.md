# Tournaments

Play agents against each other in parallel workers, hand Etude whatever tables
your game code produces, and query every tournament with DuckDB. A tournament
is an eval run, independent of training.

```sh
uv add 'etude-sdk[eval]'   # DuckDB, for writing and querying tables
```

```python
import os

from etude.eval.records import Match
from etude.eval.tournament import Tables, agent, environment, evaluate, round_robin

world = environment("duel", files=["game/"], config={"world": "w6"})
roster = [
    agent("ppo-25k-search", checkpoint=model, config={"player": "search", "simulations": 64}),
    agent("ppo-25k-sample", checkpoint=model, config={"player": "sample", "temperature": 1.0}),
    agent("heuristic", code=["players/heuristic.py"]),
]


def play(match: Match) -> Tables:  # importable by name: workers are fresh processes
    results = play_deal(match.agents, deal=match.conditions["deal"], seed=match.seed)
    return {
        "plays": [
            {
                "game": r.game,
                "player": r.seat,
                "won": r.won,
                "on_play": r.seat == 0,
                "opening_lands": r.lands,
            }
            for r in results
        ],
        "games": [{"game": r.game, "group": "all", "turns": r.turns} for r in results],
    }


evaluate(
    experiment="league",
    environment=world,
    play=play,
    matches=round_robin(roster, conditions=[{"deal": d} for d in range(25)], games=8),
    workers=len(os.sched_getaffinity(0)),
    storages=[bucket],
)
```

```python
from etude.eval.query import compare, connect

db = connect("s3://my-bucket/runs")  # or a directory; every tournament under it
db.sql("""select a.name, avg(won::int) from plays
          join seats using (tournament, match, player) join agents a using (agent)
          where on_play and opening_lands >= 2 group by all""").show()
for c in compare(db, table="plays", value="won", by=["on_play"]):
    print(c.subject, c.opponent, c.split, c.value, c.interval, c.matches)
```

```sh
uv run etude reports eval --open   # every tournament under ~/.etude/track
```

- **Agents.** A checkpoint (by artifact `sha256`) or code (by its source files,
  hashed relative to the repository), plus a config: free JSON or a Pydantic
  model. The identity covers both, so one checkpoint at two search budgets is
  two agents. Names are for reading; joins use identity.
- **Matches.** A seating of agents under fixed `conditions`, played for
  `games`. The unit Etude schedules, resumes and resamples. `round_robin`
  makes every ordered seating; build a `Match` directly for anything else,
  such as `Match(agents=(a, b, c, d), groups={"north": (0, 2), "south": (1, 3)})`.
  Group `all` holds every seat.
- **Rows are yours.** `play` returns any tables. Each row names a `player`
  (a seat) or a `group` of its match; Etude adds `tournament` and `match`.
  Values are numbers, text, booleans or nulls.
- **Strict.** A row with neither or both of `player` and `group`, a seat or
  group the match lacks, or a column whose type changes during a tournament is
  refused. A refused or raising match is recorded as failed with its error,
  nothing else of it is stored, and the tournament stops.
- **Environments are never pooled.** A tournament has one environment;
  resuming with another is refused, and `compare` keeps environments apart.
- **Resume.** Calling `evaluate` again with the same `run` skips every match
  already delivered to any of `storages`, even on a machine with an empty disk.
- **Storage.** Each match writes one Parquet file per table under
  `RUN/tables/` and its record under `RUN/matches/`, delivered after every
  match; `etude sync` finishes delivery later. On finish, a process's files
  for each table are merged into one, which replaces them once delivered.
- **Tables.** `connect` gives each of your tables across every tournament,
  beside `tournaments`, `agents` (with a `config.KEY` column per scalar config
  key, dotted when nested), `matches`, `seats` and `groups`.
- **Comparison.** `compare` sets each subject against each opponent and
  against all: a seat's agent against the other seats' agents, or a group
  against the match's other groups. The value is `sum(value) / sum(per)`, or
  the mean without `per`. The 95% interval is a percentile bootstrap over
  whole matches with a fixed seed; fewer than two matches have none.
