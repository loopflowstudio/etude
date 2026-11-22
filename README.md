# Etude

Etude is for people building training and multi-agent evaluations: run your
Python on local, SSH or rented machines, keep results in your own storage,
and resume unfinished work. You own the learning algorithm and game rules.

```sh
uv add 'etude-sdk[eval]'                 # Python 3.12, in your project
uv run etude train experiment.py        # your train(config), locally
uv run etude eval experiment.py         # your play(match, agents), locally
uv run etude report experiment.py --open
uv run etude view experiment.py         # serve live runs, reports and machines
```

The smallest training file (`experiment.py`) is:

```python
from pydantic import BaseModel
from etude.train.tracking import track


class Config(BaseModel):
    updates: int = 3


def train(config: Config) -> None:
    with track(config=config) as run:
        for update in run.steps("updates", to=config.updates):
            run.log({"example": update})
```

Replace the logged example with your learning step. Etude supplies identity,
checkpoints, deterministic match seeds and storage; your code supplies models
and games.

Leave the view running in one terminal. In another, `etude train` prints a link
to the run's curves, config and checkpoints.

Start with [a complete runnable experiment](docs/getting-started.md). Continue
with [training](docs/training.md), [evaluation](docs/evaluation.md),
[tracking, live view and reports](docs/tracking.md), [renting machines](docs/launcher.md),
and [limits](docs/limits.md). [Hanabi](docs/hanabi.md) is a cooperative PyTorch
example. Import supported names from their defining modules; each module's
`__all__` lists its public surface.

For Etude development, read [the architecture](docs/dev/launcher.md#layout-and-dependency-rules),
[testing](TESTING.md) and [conventions](AGENTS.md). Licensed under [MIT](LICENSE).
