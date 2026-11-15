# Etude

Etude helps people building learning systems run their Python training and evaluation code on local or rented machines and inspect the results.

It records metrics, carries saved state between attempts, evaluates checkpoints,
and generates HTML reports in storage you own. You supply the learning algorithm,
game, model forward and player behavior.

```sh
uv add 'etude-sdk[eval]'  # Python 3.12 and uv
```

An experiment file declares ordinary Python functions as jobs:

```python
from etude.jobs import TrainingJob
from train import Config, train


def training_jobs() -> list[TrainingJob]:
    return [TrainingJob(name="base", train=train, config=Config(), max_hours=1)]
```

[Start with a runnable train/eval prototype](docs/getting-started.md), then read
[training](docs/training.md), [evaluation and players](docs/evaluation.md),
[tracking and reports](docs/tracking.md), or [machines and budgets](docs/launcher.md).
[Limits](docs/limits.md) explains recovery, cost and inference boundaries.
[Hanabi](docs/hanabi.md) is a larger game example.

For maintainers: [developer references](docs/dev/README.md),
[testing](TESTING.md), [conventions](AGENTS.md). [MIT licence](LICENSE).
