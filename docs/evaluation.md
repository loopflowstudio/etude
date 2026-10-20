# Eval runs

Play agents against each other, hand Etude the episodes, and read them back
compared across every eval run. An eval run is independent of training: its
inputs are checkpoints, by artifact, and code.

```python
from etude.eval.records import Count, Episode, Measure, Player
from etude.eval.run import checkpoint_agent, code_agent, record

# `model` and `anchor` are ArtifactRefs with digests, as run.checkpoint(...) returns.
agents = [
    checkpoint_agent("ppo-20k", model),
    checkpoint_agent("frozen-10k", anchor, role="anchor"),
    code_agent("heuristic", ["manabot/agents/heuristic.py"]),  # identified by its source
]
measures = [
    Measure(name="win", meaning="won the game", kind="rate", of="player"),
    Measure(name="turns", meaning="turns until the game ended", kind="mean", of="episode"),
]
with record(experiment="arena", agents=agents, measures=measures) as evaluation:
    for deal in deals:
        if deal.key in evaluation.keys():  # stored by an earlier attempt
            continue
        evaluation.add(
            Episode(
                key=f"{deal.key}-{seats}",
                block=deal.key,  # both seatings of a deal are resampled together
                labels={"cell": deal.cell},
                players=tuple(
                    Player(
                        agent=name,
                        labels={"on_play": str(seat == 0)},
                        measures={"win": Count(hits=won, opportunities=1)},
                    )
                    for seat, (name, won) in enumerate(game.results)
                ),
                measures={"turns": game.turns},
            )
            for seats, game in play(deal)
        )
```

```sh
uv run etude reports eval --open   # every eval run under ~/.etude/track, joined
```

```python
from etude.eval.read import compare, episodes

views = episodes("s3://my-bucket/runs")
for c in compare(views, "win", by=["cell"]):
    print(c.subject, c.opponent, c.labels, c.value, c.interval, c.blocks)
```

- **Players and agents.** A player is a position in one episode; an agent is
  what takes it. Any number of players; a `Group` lists positions measured
  together, such as a team, with measures of its own.
- **Identity joins runs.** An agent is its digest: a checkpoint's artifact
  `sha256`, or the digest of its source files' paths and bytes. Names are local
  to a run, so two runs may call the same agent differently and still compare.
- **No privileged measure.** Every measure is declared: a `mean` averages its
  values, a `rate` divides total hits by total opportunities. Each belongs to
  one level, `player`, `group` or `episode`.
- **Strict.** `add` raises on an undeclared agent or measure, a measure at the
  wrong level or of the wrong kind, or a key given twice, and stores nothing
  from that call. Resuming a run requires the same agents and measures.
- **Episodes replace by key.** Each `add` writes one immutable file under
  `RUN/episodes/`; a later file replaces a key an earlier one stored. They are
  delivered to each storage with `evaluation.json`, `run.json` and
  `state.json`; `etude sync` finishes delivery later. `keys()` reads what this
  computer stored.
- **Comparison.** A against B is A's aggregate over the episodes where B took
  another position; against all, over every episode of A's. The 95% interval
  is a percentile bootstrap over blocks with a fixed seed, so the same files
  give the same numbers. Fewer than two blocks have no interval.
- **Conflicts.** A measure name declared with a different kind, level or
  meaning in two runs is listed as a conflict and never combined.
- **Gameplay logs** are yours: `Episode.log` names one relative to the run;
  Etude does not read it.
