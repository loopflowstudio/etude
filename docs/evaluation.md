# Evaluation

Play agents against each other in parallel workers, hand Etude whatever tables
your game code produces, and query each experiment's results as one DuckDB
database. Tournaments decide which matches to play: each checkpoint of a model
through a benchmark's matchups as it trains, or hand-picked models in a round
robin. Evaluation is independent of training.

```sh
uv add 'etude-sdk[eval]'   # DuckDB, for writing and querying tables
```

```python
from etude.eval.match import Tables, agent, environment
from etude.eval.query import statsdb
from etude.eval.records import (
    Benchmark,
    CheckpointKey,
    CheckpointRef,
    Match,
    Matchup,
    Model,
    RoundRobin,
)
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
# A checkpoint is its key, its run and file name, with the saved file to read.
ref = CheckpointRef(key=CheckpointKey(run="ppo-1", name="updates-500.pt"), artifact=saved)
hold(progress, experiment="ppo", checkpoint=ref, play=play, storages=[bucket])

# Batch: add a matchup to the definition; holding with no checkpoint plays every
# missing match, so the new line is played for every checkpoint held so far.
hold(progress, experiment="ppo", play=play, workers=8, storages=[bucket])

# Batch: hand-picked models, each against every other.
league = RoundRobin(name="finals-1", environment=world, conditions=deals, games=8)
hold(league, experiment="ppo", enter=finals, play=play, workers=8, storages=[bucket])

db = statsdb("ppo", bucket)  # or "s3://my-bucket/runs", a directory, or nothing: ~/.etude/track
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

- **Agents.** An agent's key (`AgentKey`) is the checkpoint it plays, by the
  checkpoint's own key, or a code agent's name, plus its config: free JSON or
  a Pydantic model. One checkpoint at two search budgets is two agents. A
  checkpoint's key (`CheckpointKey`) is its training run and file name
  (`ppo-1/checkpoints/updates-500.pt`) or, for any other, its address; never
  its bytes, so two runs warm-started from one file hold two checkpoints,
  each evaluated. `agent("net", checkpoint=ref, code=["players/net.py"],
  config=...)` also names the code that plays it: its commit and source digest
  are the agent's `Version`, recorded with each match (`seats.commit`,
  `seats.source`) but not part of the key, so changed code reuses what was
  played. Joins use the key, encoded (`agents.agent`, `seats.agent`).
- **Source files.** A directory stands for the files below it. In a git
  checkout only tracked files count, named by their path in the checkout, so
  caches and scratch files never change a version; an uncommitted edit does,
  at the same commit. A launched job's source has no `.git`; there files are
  named by their path below `ETUDE_SOURCE_DIR` and the commit is
  `ETUDE_COMMIT`, both of which the launcher sets, so the same commit gives the
  same version on a rented machine as on yours.
- **Older eval runs.** Agents recorded by etude-sdk 0.2 were identified by a
  digest. They load with that digest as their key (`AgentKey.recorded`), which
  no key since equals, so their results stay readable and nothing new counts
  them as played.
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
  `hold(benchmark, checkpoint=ref)` puts the checkpoint (by its key) in every
  model seat and plays each given matchup; with no checkpoint it plays every
  matchup ever defined for every checkpoint held, skipping what is delivered.
- **Pairs of checkpoints.** A matchup with no model seat is played once, not
  per checkpoint, so chosen checkpoints can meet: seat each as
  `Model(...).agent(ref)`, the agent a model seat would make of it. Build the
  benchmark from the checkpoints saved so far, such as each against the one
  before it, and hold it in batch whenever more appear; each pass plays only
  the pairs not yet delivered. A hold on the go leaves these matchups to batch.
  Their rows are `schedule` rows with no `checkpoint`; `curves` follows only
  matchups with a model seat.
- **Round robins.** `hold(round_robin, enter=agents)` plays every pair of
  the agents given once per condition; seat swaps belong inside `play`.
  `hold(round_robin)` then plays every pair of every entrant so far that no
  eval run delivered, such as a late entrant against the earlier ones.
- **Held for an experiment.** A tournament only decides which matches to
  play. `hold(..., experiment=...)` makes its eval run the experiment's, so
  its results sit beside the experiment's training runs and their
  checkpoints in the experiment's database.
- **Stored in its eval runs.** Each eval run a `hold` starts keeps
  `tournament.json` in its own folder: the definition it was held with, and
  the checkpoint or entrants it added. A tournament is every eval run naming
  it, so a hold on a rented machine delivers its part like any other run, and
  many eval runs can grow one tournament at once.
- **Reuse.** A match is the same wherever it is played, so within one
  experiment and environment its results are reused by key: `curves`,
  `standings` and a batch hold count a scheduled match that any of the
  experiment's eval runs in the environment holds, whether held for this
  tournament, another, or none. Each counts once, from the eval run that
  recorded it last.
- **Conflicts.** Another format, environment or round-robin rules under one
  name (`tournament_conflict`), or a matchup name defined two ways
  (`matchup_conflict`), is refused by a batch hold and by `curves` and
  `standings`; `statsdb` reports it in the `tournaments` table.
- **Run names.** Under `etude launch` the eval run is the launched run
  (`ETUDE_RUN`), so `etude logs` and `status` lead to it and a retry resumes
  it, finding the same checkpoint (by its key) wherever the retry put it. Elsewhere it is named for the tournament and what is given; the same
  call resumes it.
- **Splitting.** `share=(k, n)` plays one of n disjoint parts, for n
  machines: the scheduled matches in key order, dealt round, so parts differ
  by at most one match. Delivered matches are dealt too, so a retried share
  keeps its part.
- **Cost.** Holding a checkpoint or entrants reads nothing of the
  tournament: it plays their matches. Holding nothing new (batch) lists each
  place once and reads each eval run's `run.json` and, for the experiment's,
  its `tournament.json` (or its `environment.json` when it was held for no
  tournament), to play what the tournament calls for and none of the
  experiment's eval runs in its environment delivered.
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
        run="ppo-1",  # the checkpoint's key: its run, and the file's name
    )
    return evaluation(
        progress,
        experiment="ppo",
        play=play,
        checkpoint=weights,
        workers=8,
        max_hours=0.5,
        max_usd=Decimal(1),
    )
```

```sh
etude launch eval.py --rent cpu8 --checkpoint updates-500 --sha256 … --size-bytes 1048576
```

An evaluation job holds a tournament on a launched machine, and nobody writes
its command: Etude calls `hold` there with `play`, imported by name from your
checkout, and the checkpoint, which reaches the machine as the job's input,
verified by digest. The input's run and file name are the checkpoint's key,
and its file is every model seat's `artifact.uri`, which `play` opens. A round robin takes `enter=`
agents instead. The eval run is the launched run, so its tables reach the
machine's storage with the run's other files and `statsdb` finds them there.
Holding nothing new (batch) is refused: a launched job reads no other eval
run; an experiment's pass decides what is new first ([Experiments](#experiments)).
The job's environment needs `etude-sdk[eval]`; the rest is as for any
[typed job](launcher.md#typed-jobs).

## Experiments

An experiment file says what to evaluate given what exists, and `etude eval`
plays whatever that calls for and no eval run has delivered. Run it while
training goes, after it ends, or with a matchup added later: each pass looks
again. The experiment is named for the file: `ppo.py` evaluates the training
runs of experiment `ppo`, and its eval runs are that experiment's. Define
`runs()` in it too and `etude launch ppo.py` starts training runs of `ppo`, so
`track()` in the training code needs no `experiment`.

```python
# ppo.py
from etude.eval.records import Benchmark, Matchup
from etude.experiment import ExperimentState
from etude.jobs import evaluation
from etude.launch.records import Job

# direct is a Model, heuristic an agent, deals the conditions
from arena import deals, direct, heuristic, play, world


def eval(state: ExperimentState) -> list[Job]:
    run = state.run("ppo-1")
    rated = run.checkpoints.every(5)  # every fifth checkpoint by ordinal
    pairs = [
        Matchup(
            name=f"{new.name} vs {old.name}",
            tags=("consecutive",),
            players=(new.agent(direct), old.agent(direct)),
            conditions=deals,
            games=8,
        )
        for old, new in zip(rated, rated[1:])
    ]
    if run.final is not None:  # once training has finished
        pairs += [
            Matchup(
                name=f"final vs {old.name}",
                tags=("endpoint",),
                players=(run.final.agent(direct), old.agent(direct)),
                conditions=deals,
                games=8,
            )
            for old in rated[:-1]
        ]
    anchors = Matchup(name="vs heuristic", players=(direct, heuristic), conditions=deals, games=8)
    ladder = Benchmark(name="ladder", environment=world, matchups=(anchors, *pairs))
    return [evaluation(ladder, play=play, checkpoints=rated, workers=8, max_hours=1)]
```

```sh
etude eval ppo.py --source s3://bucket/etude/launch               # play here
etude eval ppo.py --source s3://bucket/etude/launch --rent cpu8   # launch
etude eval ppo.py --source s3://bucket/etude/launch --rent cpu8 --watch 600
```

- **State.** `state.runs` is the experiment's training runs under the
  source: each one's name, whether it is `running`, `finished` or `failed`,
  and the checkpoints it stored, each with its ordinal, clocks and tags.
  `Checkpoints` is a tuple, so a slice works too; `every(n)`, `at(clock,
  *values)` and `tagged(tag)` choose by ordinal, coordinate or tag, and
  `final` is the last checkpoint once a run has finished.
- **What `eval` returns.** Evaluation jobs holding a benchmark for the
  checkpoints it chose. Seat a chosen checkpoint with `checkpoint.agent(model)`
  to make a matchup of fixed agents, played once rather than per checkpoint.
  `tags` mark matchups for readers, such as pairs a rating fit leaves out.
- **A pass.** For each job, the matches exactly what `eval` returned calls
  for, the matchups it gave for the checkpoints it gave, that no eval run of
  the experiment in its environment delivered. Without `--pool` or `--rent`
  they are played here and delivered to the source. With one, a job is
  launched to play exactly those, with each checkpoint they seat as an input
  from its training run's folder; the machine's storage must deliver to the
  source.
- **Not twice.** A launched pass is named for its matches and kept in
  `ETUDE_HOME/experiments/launched.json`, so a later pass neither launches it
  again nor plays its matches while it runs. A pass that finds one of them
  failed stops, so a watch never spends again unattended; look, then rerun.
- **Failures.** A match that failed is counted on every pass (`ladder: 0 new,
  1 failed`) and not played again, so a result is never quietly replaced.
  `--retry-failed` plays the failed matches again.
- **Without playing.** `etude.eval.tournament.played` answers what a pass
  would find: each match the definition calls for, as delivered (with the eval
  run that delivered it last, whose rows count), failed or new. `etude report`
  reads it.
- **Changing the design.** A new matchup or benchmark is played for every
  checkpoint chosen, including those of finished runs, and a match another of
  the experiment's tournaments already played is reused. A matchup `eval`
  stops returning is no longer played; what it played stays. Redefining a
  matchup under the same name is refused (`matchup_conflict`): give the new
  one a new name. A match is its seats, conditions and game count, so raising
  `games` plays every match of the matchup again.

`etude report ppo.py` draws the experiment's evals beside its training
([tracking](tracking.md#reports)), following the file's `report()`. Each
checkpoint agent is drawn where its training run stood when it saved that
checkpoint, found by its key; code agents and checkpoints from outside the
experiment are anchors, drawn once. A checkpoint `eval(state)` chose that no
eval run has delivered is marked not yet played, one nothing played is marked
not evaluated, and failed matches are listed. Each match counts from the eval
run that delivered it last, whichever commit its code was at. Where the
matches were played at several commits (`seats.commit`), "Played at" offers
each: that commit's view counts every match as played at it. A delivered
match is not played again for a new commit, so a commit's view holds the
matches first played at it, and any held again there.

## Querying

`statsdb(experiment, store)` gives each of your tables across every eval run of
the experiment, beside Etude's own:
`runs` (with the tournament each was held for), `agents` (with a `config.KEY`
column per scalar config key, dotted when nested), `matches`, `seats`,
`groups`, `tournaments` (with any `conflict` among its eval runs), `schedule`
(every match a tournament calls for, with its matchup and checkpoint) and
`training_checkpoints` (every checkpoint a training run under the source
recorded, joined to agents and the schedule by `checkpoint`, its key).

The database is kept in a file under `ETUDE_HOME/statsdb`, one per
experiment and store. Opening it lists the store once and reads only what
arrived since it was last opened, so a query never waits on every result
being read again. The store stays the authority: `statsdb(..., rebuild=True)`
reads everything again, and a database from another Etude schema is rebuilt
on its own. One process opens it at a time; another gets `statsdb_in_use`.
Experiments are never joined: each has its own database.

`compare(db, table=, value=, per=, where=, by=)` sets each subject against
each opponent and against all: a seat's agent against the other seats'
agents, or a group against the match's other groups. The value is
`sum(value) / sum(per)`, or the mean without `per`. The 95% interval is a
percentile bootstrap over whole matches with a fixed seed, independent of
the order rows are read in; fewer than two matches have none.
