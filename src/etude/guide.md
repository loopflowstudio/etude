# Etude guide

Offline reference for experiment authors and coding agents.

## Workflow

Save and commit the example below as `experiment.py` in a uv project with
`etude-sdk[eval]` installed. For the rental commands, use your configured
on-demand name in place of `gpu`. Validate the file offline, quote the pass,
launch, then wait for runs to finish and rentals to be deleted.

```sh
uv run etude check experiment.py
uv run etude launch experiment.py --rent gpu --max-usd 5 --quote
uv run etude launch experiment.py --rent gpu --max-usd 5
uv run etude wait
```

For local iteration on this computer:

```sh
uv run etude check experiment.py
uv run etude smoke experiment.py
uv run etude train experiment.py
uv run etude eval experiment.py
uv run etude report experiment.py --open
```

- `etude check` executes the declarations locally and prints planned runs
  and matches. Errors name the source line and include a fix when available.
  Pass `--source DIR` to count remaining work from local results.
- `etude launch --quote` reads storage and provider availability to price the
  pass without submitting or renting.
- `etude launch` starts what fits and queues the rest; `etude queue` records
  intent without waiting for a provider. Running either again submits only
  missing work.
- Every command is one of three kinds: **read** (answers locally, never waits
  on another machine), **ask** (records intent and returns ids) or **wait**
  (contacts another machine and accepts an optional time limit).

## Conventions

- **Python 3.12 and uv.** Run commands with `uv run etude ...`. Import
  supported names from their defining modules (`__all__`).
- **Experiment files.** Define `training_jobs()` (optionally taking
  `state: ExperimentState`), `eval_jobs(state: ExperimentState)` and optionally
  `report() -> Report`. A plain file with only `train(config)` is a local
  recipe for `etude train`. A file with only `report()` can be used with
  `etude report`.
- **Addresses and seeds.** Every declared run's address is
  `EXPERIMENT/JOB@SEED` (`EXPERIMENT` defaults to the file stem; `--experiment`
  overrides it). Training `seeds` must be nonempty and distinct; each makes an
  independent run. Eval `games=N` generates match indices `0..N-1` from
  `base_seed`; raising `N` preserves completed matches.
- **Training.** `train(config: Config) -> None` takes a Pydantic config model
  and opens `with track(config=config) as run:`. Call `run.resume(...)` and
  `run.save()` to survive retries and spot preemption, and `run.checkpoint(...)`
  to publish immutable models for evaluation. Give every launched job a finite
  `max_hours` (and `max_usd` for rentals) and a `smoke=` config for
  `etude smoke`.
- **Evaluation.** Write `async def play(match, agents) -> Tables` (or an async
  generator `play_many(matches)` for vectorized engines), never a synchronous
  `def play`. Game functions, loaders, `build` hooks and player wrapper records
  must be importable top-level definitions. Put `load(checkpoint)` and heavy
  model-library imports (such as `torch`) in a separate module from `play` so
  shared-inference game processes stay light.
- **Checkpoints and players.** `load(checkpoint: CheckpointRef) -> ModelForward`
  returns a deterministic list-in/list-out callable. Sampling belongs in game
  code, seeded from `match.seed`.

## Minimal experiment

```python
from collections.abc import Mapping, Sequence
from decimal import Decimal
from random import Random

from pydantic import BaseModel, Field

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward, ModelForward
from etude.eval.match import environment
from etude.eval.records import Benchmark, CheckpointPlayer, CheckpointSlot, Code, Match, Seating
from etude.eval.result import Tables
from etude.jobs import Agents, EvalJob, ExperimentState, TrainingJob
from etude.report import Curve, Report, Score
from etude.train.tracking import track


class Config(BaseModel):
    updates: int = Field(default=10, ge=1)
    heads: float = Field(default=0.7, ge=0, le=1)


class Rules(BaseModel, frozen=True):
    heads: float = Field(default=0.7, ge=0, le=1)


class Counts(BaseModel):
    heads: int = 0
    tosses: int = 0


def train(config: Config) -> None:
    with track(config=config) as run:
        counts = run.resume(extra=Counts())
        for update in run.steps("updates", to=config.updates, save_every=1):
            counts.heads += Random(f"{run.seed}:{update}").random() < config.heads
            counts.tosses += 1
            run.log({"heads": counts.heads / counts.tosses})
            run.save()
        path = run.out / "coin.json"
        path.write_text(counts.model_dump_json())
        run.checkpoint(path)


# Keep heavy model-library imports (e.g. torch) in load's own module, not play's.
def load(checkpoint: CheckpointRef) -> ModelForward:
    counts = Counts.model_validate_json(checkpoint.path.read_text())
    probability = counts.heads / counts.tosses

    def forward(inputs: list[object]) -> list[object]:
        return [probability for _ in inputs]

    return forward


async def _fair(inputs: list[object], *, rows: int | None = None) -> list[object]:
    return [0.5 for _ in inputs]


def build(player: object, leaves: Mapping[str, Forward]) -> Forward:
    if isinstance(player, CheckpointPlayer):
        return leaves[player.checkpoint.key]
    if isinstance(player, Code):
        return _fair
    raise TypeError(f"unknown player {type(player).__name__}")


async def play(match: Match[Rules], agents: Sequence[Forward]) -> Tables:
    target = Random(f"{match.seed}:game").random() < match.config.heads
    scores: list[dict[str, bool | int | float | str | None]] = []
    for seat, agent in enumerate(agents):
        [probability] = await agent([None])
        guess = Random(f"{match.seed}:player:{seat}").random() < float(str(probability))
        scores.append({"seat": seat, "won": guess == target})
    return {"scores": scores}


PANEL = Benchmark(
    name="prediction",
    environment=environment("coin-prediction"),
    seatings=(
        Seating(
            name="fair baseline",
            players=(CheckpointSlot(), Code(name="fair")),
            config=Rules(),
            base_seed=42,
            games=8,
        ),
    ),
)


def training_jobs() -> list[TrainingJob[Config]]:
    return [
        TrainingJob(
            name="coin",
            train=train,
            config=Config(),
            smoke=Config(updates=1),
            seeds=(0, 1),
            max_hours=0.1,
            max_usd=Decimal("0.50"),
        )
    ]


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    saved = [c for run in state.runs for c in run.checkpoints]
    return [
        EvalJob(
            tournament=PANEL,
            play=play,
            agents=Agents(load=load, build=build, devices=("cpu",)),
            checkpoints=saved,
            max_hours=0.1,
            max_usd=Decimal("0.50"),
        )
    ]


def report() -> Report:
    return Report(score=Score(table="scores", value="won", reference=0.5), evals=(Curve(),))
```
