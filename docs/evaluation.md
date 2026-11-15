# Evaluation and players

This guide is for authors writing games, choosing checkpoint opponents and composing players over model forwards.

Start with the [runnable prototype](getting-started.md); use
[Hanabi](hanabi.md) for a full game. [Training](training.md) publishes checkpoints,
[tracking](tracking.md) draws results, and [the launcher](launcher.md) places work.

## Write the game and forward

```python
# models.py: may import your model framework
from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward


def load(checkpoint: CheckpointRef) -> Forward:
    model = read_model(checkpoint.path)  # your code; put it in inference mode

    def forward(inputs: list[object]) -> list[object]:
        return model.predict(inputs)  # logits/values, one output per input

    return forward
```

```python
# game.py: keep framework imports out of this module
from collections.abc import Sequence
from random import Random

from etude.eval.agents import Forward
from etude.eval.records import Match
from etude.eval.result import Tables


def play(match: Match, agents: Sequence[Forward]) -> Tables:
    random = Random(match.seed)
    state = new_game(match.config, random)  # your game
    while not state.over:
        [output] = agents[state.seat]([state.observation()])
        action = choose(output, random)  # your action selection
        state.step(action)
    return {"scores": [{"seat": seat, "score": state.score(seat)} for seat in range(len(agents))]}
```

`play` gets a match and one value per seat, in `match.players` order. Without
a build hook each value is the checkpoint's callable forward. Inputs and outputs
must be picklable, preferably NumPy arrays or plain Python values. Return one
output per input, in order, independent of other inputs in the batch. Return
logits or values; choose actions in the game.

Each returned table is a list of records. Every record needs `seat` or `group`,
never both. Numeric scores are your choice. For multiple games add a `game`
column and respect `match.games`. Empty tables, unknown seats, and changing
column types fail the match. Raise when your game is invalid; do not return an
error as a score.

## Declare what to evaluate

```python
from etude.eval.match import environment
from etude.eval.records import Benchmark, Held, Seating
from etude.jobs import Agents, EvalJob, ExperimentState

from game import play
from models import load

arena = Benchmark(
    name="self-play",
    environment=environment("my-game"),
    seatings=(Seating(name="self", players=(Held(), Held()), configs=({},), games=4),),
)


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    return [
        EvalJob(
            tournament=arena,
            checkpoints=[checkpoint for run in state.runs for checkpoint in run.checkpoints],
            play=play,
            agents=Agents(load=load),
            max_hours=0.5,
        )
    ]
```

A benchmark fills each `Held()` with the checkpoint being evaluated. A seating
names the opponent rule, configs and game count. Use JSON-serializable Pydantic
configs for typed game inputs: `play(match: Match[Deal], agents: ...)`.
`match.seed` is stable for the match; derive game/search randomness from it.

```sh
uv run etude eval experiment.py
uv run etude eval experiment.py --source s3://bucket/runs
uv run etude eval experiment.py --retry-failed
```

Each pass plays only missing matches. Delivered successes survive a failed run;
ordinary failed matches wait for `--retry-failed`. Raise `RetryRequested` from
`etude.errors` for a transient failure that another pass should try automatically.
A loader failure affects every undelivered match using that checkpoint leaf;
unrelated matches continue. A caught inference error still fails its match.

## Choose opponents

Import these from `etude.eval.records`:

| Seat | Opponent |
| --- | --- |
| `Held()` | The checkpoint being evaluated |
| `Held().player(ref)` | One fixed `CheckpointRef` |
| `Earlier(clock="updates", by=100)` | Same learner, 100 updates earlier |
| `Fraction(clock="updates", value=0.5)` | Same learner at half the updates |
| `At(clock="updates", value=0)` | Initialization |
| `Final(job="small")` | Each seed of another job's final checkpoint |
| `Aligned(clock="updates", job="small")` | Another job at the same update |
| `Code(name="heuristic", source=["heuristic.py"])` | Author code; build it in your hook |

Relative positions are exact unless `at_or_before=True`. A checkpoint not yet
available leaves a gap; Etude does not silently substitute another. `Final`
waits for the run to end. `experiment=`, `job=`, `seed=` and `role=` select a
policy; `policy=PolicyKey(...)` names one explicitly. `PolicyKey` and
`CheckpointRef` are defined in `etude.checkpoints`.

A benchmark splits `games` across selected seed pairs; add `seeds="one"` to a
seating to use one stable pair. For a fixed league use
`RoundRobin(name=..., environment=..., configs=(...), games=...)` and
`EvalJob(enter=players, ...)` instead of `checkpoints=`.

## Compose player behavior

```python
from collections.abc import Mapping
from typing import Literal

from etude.eval.agents import Forward
from etude.eval.records import CheckpointPlayer, Seat
from etude.records import Record


class Mirror(Record):
    over: Seat
    worlds: int = 8
    depth: int = 20
    choice: Literal["greedy", "sample"] = "sample"
    temperature: float = 1.0


def build(player: object, leaves: Mapping[str, Forward]) -> object:
    if isinstance(player, CheckpointPlayer):
        return leaves[player.checkpoint.key]
    if isinstance(player, Mirror):
        return MirrorPlayer(player, build(player.over, leaves))  # your implementation
    raise TypeError(f"unknown player {type(player).__name__}")
```

Declare `Agents(load=load, build=build)` and seat `Mirror(over=Held())`.
Wrappers are your frozen Pydantic records; define them at module scope.
Their type and fields enter player identity, with checkpoint leaves replaced
by checkpoint keys. Greedy/sample choice, temperature and search settings belong
here. A changed choice becomes a distinct player and gets distinct results.

The hook receives the reconstructed player tree and a mapping of checkpoint keys
to forwards. It returns whatever your game wants: an object with `choose`, a
callable, or another author type. Etude never calls the built object. Annotate
`play`'s second parameter for the objects your build hook returns.
Checkpoint leaves share loading and batches through every wrapper. Bare
checkpoints bypass the hook. Code players need a build hook too.

Search can send a whole list in one forward call. In Magic Mirror Descent,
advance every world/action branch at one ply together; this takes `depth` calls
per decision rather than `worlds × actions × depth`. Keep search randomness in
the game process, seeded from `match.seed`. Etude supplies no sampling.

## Batching, tuning and constraints

Compatible jobs (equal `Agents` declarations) share a run. Etude chooses loading
placement from the machine: on CPUs, each game process caches checkpoint
forwards and calls them directly; on visible NVIDIA hardware, one agent process
shares checkpoint loading and batches across games. The author API is the same.
The loader selects its model device; hardware detection does not move the model.

`Agents(max_batch=32, max_delay_seconds=0.005, load=load)` sets shared batch
targets. A single call's input list stays together even above `max_batch`.
CPU games can still send a whole search list in one call, without cross-game
batching. No author process-count or placement setting is needed. CPU capacity
and host memory bound CPU games; GPU games can fill the declared batch.

`memory_gb` bounds loaded checkpoint-file bytes, defaulting to half the host's
available memory. CPU game caches split that allowance. Idle checkpoints may
be evicted and loaded again. This does not measure tensor allocations or GPU
residency. Keep game modules light for GPU execution: spawn imports their
modules; CPU inference intentionally imports the loader in each game process.
Etude gives each CPU game one model-library thread; a shared loader must honor
the `OMP_NUM_THREADS` budget supplied before its module imports.

For code or an external API, use `Agents(build=build)` without a loader. The
hook can return your heuristic or API client; a group with no checkpoint leaves
starts no agent process. Hosted/LLM clients, rate limits, token budgets and API
credentials are your responsibility; Etude has no hosted-player orchestration layer. [Limits](limits.md) explains
where sharing and recovery stop. [Detailed reference](dev/evaluation.md) covers
standalone evaluations, identities, compatibility readers, records and queries.
