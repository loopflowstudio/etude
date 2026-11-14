# Train and evaluate a Hanabi team

```sh
uv sync --extra train --extra eval --extra hanabi
.venv/bin/etude train examples/hanabi.py --config '{"updates":20}'
# Ctrl-C once after a few episodes: the current episode completes and saves (exit 75).
.venv/bin/etude train examples/hanabi.py --resume
.venv/bin/etude eval examples/hanabi.py
.venv/bin/etude report examples/hanabi.py
```

Use `--experiment NAME` for a fresh variant on train and eval; `--resume --until updates=200` trains the same team further. `ETUDE_HOME=/path/to/demo` keeps these runs apart
from your other work; set it for every command. Run from this checkout's root.
The example dependency is optional and is absent from Etude's required
installation and offline test environment.

[`examples/hanabi.py`](../examples/hanabi.py) uses OpenSpiel's C++ Hanabi and
PyTorch on the CPU: two players, one color, three ranks, two cards per hand.
Both seats share a linear policy. Each optimizer update follows one complete
team episode, with its loss, score and cumulative decision count logged.
The default is twenty episodes, a demonstration of the API rather than a
competitive Hanabi learner. No JAX or PufferLib is used.

`run.resume` registers the policy, Adam state and cumulative transition count.
`run.steps` checks `run.should_stop` between episodes; `run.save()` publishes a
whole save each episode. Episode randomness follows the absolute update, so
there is no game in flight or random stream to reconstruct after a saved stop.
A second Ctrl-C interrupts immediately. Resume with no `--config` uses the
stored config, including any overrides from the first command.

`arena(config: ArenaConfig) -> Benchmark` is pure. It seats the checkpoint as
both teammates, then with a random teammate. `play(match: Match[Deal])` gets
an author-owned Pydantic config and loads each checkpoint folder's `policy.pt`.
Its rows name group `all`, the cooperative team, rather than claiming that one
seat won. Evaluation chooses the first and final checkpoint of each run and
plays only matches not yet stored. A repeated eval pass prints zero new matches.

```python
from etude.eval.query import statsdb
from etude.storage import default_root

db = statsdb("hanabi", default_root())
print(db.sql("select avg(score), count(*) from teams").fetchall())
```

OpenSpiel 2.0.2 provides macOS arm64 and Linux wheels on
[PyPI](https://pypi.org/project/open-spiel/2.0.2/). This example's Hanabi bindings
were imported on macOS arm64 and in a Linux container before using them here.
