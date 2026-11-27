# Evaluation

This guide is for authors defining players, matches and the game that produces
results; [the first experiment](getting-started.md) supplies a runnable file.

```python
from etude.eval.match import environment
from etude.eval.records import Benchmark, CheckpointSlot, Code, Seating

PANEL = Benchmark(
    name="prediction",
    environment=environment("coin-prediction"),
    seatings=(
        Seating(
            name="fair baseline",
            players=(CheckpointSlot(), Code(name="fair")),
            config={"heads": 0.7},
            base_seed=42,
            games=100,
        ),
    ),
)
```

A **match** is the unit: players in, tables out, counted once by its key.
`Benchmark` generates matches by filling `CheckpointSlot()` with each supplied
checkpoint. `RoundRobin` generates matches for every unordered pair of entrants.
Neither designates a player as the subject: both sides contribute results, and
your report chooses whose score to show.

An `EvalJob(tournament=PANEL, play=play, agents=Agents(load=load, build=build),
checkpoints=saved, max_hours=...)` declares the match generator and how to play.
For a round robin, pass `enter=(a, b, c)` instead of checkpoints. Compatible jobs
share compute automatically. The launcher prints one run address for the
group. Its directory holds the launch declaration, writer and outcome, with
the tournaments’ result directories beneath it. A retry can reuse their
sealed matches on a fresh machine. Queries and reports keep the original
tournament and match identifiers.

## Config and seeds

A seating has **one config**, a mapping or Pydantic model describing the game.
`games=N` generates indices `0` through `N-1`. Increasing N preserves existing
match keys and adds indices. `base_seed` defaults to zero.

For index i, `match.seed` is the first 60 bits of SHA256 of the UTF-8 canonical
JSON `[base_seed,i]`, with no spaces. Every seating and seat order with that base
seed gets the same seed at i. Players, config and execution device do not
influence it. The match key includes the seated player keys, groups, config,
base seed and index; the total panel size does not.

```python
from random import Random

from etude.eval.records import Match
from etude.eval.result import Tables


def play(match: Match, agents: list[object]) -> Tables:
    config_random = Random(f"{match.seed}:config")
    engine_random = Random(f"{match.seed}:engine")
    player_random = [Random(f"{match.seed}:player:{i}") for i in range(len(agents))]
    # Resolve random decks from config_random, then play using engine_random
    # and each player's independent stream. Return your own table rows.
    return run_game(match.config, agents, config_random, engine_random, player_random)
```

Use `Match[Rules]` to receive your Pydantic config as a typed model. Otherwise
`match.config` is JSON data. There is no separate deal convention: random config
choices and engine draws follow the match index. The game owns seat swapping;
reseed its streams when playing the reverse order to reuse the same draws.

Released `conditions` panels and multi-game match records retain their original
keys and key-derived seeds. They stay readable; they do not become indexed
matches. Use a fresh tournament name when switching an existing stored panel to
one config. Released `configs`/`conditions` spellings exist for those records;
new declarations use `config`.

## Players and composition

```python
from etude.eval.records import checkpoint_player, compose
from etude.records import Record
from etude.eval.records import Seat


class Sampled(Record):
    over: Seat
    temperature: float = 1.0


fixed = checkpoint_player(saved.ref)
sampled = compose(Sampled(over=CheckpointSlot(), temperature=0.5))
```

A slot belongs to a generator. `checkpoint_player(ref)` directly seats a fixed
checkpoint; there is no placeholder factory. `Code(name=..., config=...)` names
code or API players. An author's frozen Pydantic record can wrap checkpoints,
code and other records, including multiple model leaves in search or ensembles.
Use `compose(record)` for a portable resolved player, such as a round-robin entrant.
Seatings also accept the author record itself.

The qualified record type and its fields identify a composition; checkpoint
leaves contribute their keys, not byte locations. Results attach to the whole
composition, never divided among its leaves. Moving checkpoint files does not
change the player or match. Source versions are provenance; change an identifying
field when a code change should describe a different player.

Relative slots `Earlier(clock="updates", by=200)`, `Fraction(clock="updates",
value=0.5)`, `At(clock="updates", value=400)`, `Final()` and `Aligned()` select
checkpoints relative to the generator's current checkpoint. Give `policy=` to
name another experiment/job/seed/role. They need experiment training lines to
resolve, supplied automatically by experiment declarations. Missing checkpoints
show as coverage gaps instead of fabricated scores.

## Agents

There is one game signature: `play(match, agents)`. Each seated player gets one
author object in `agents`, in seat order.

```python
# models.py: keep model imports here so game processes stay light
import torch

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward


def load(checkpoint: CheckpointRef) -> Forward:
    device = (
        "cuda"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    model = load_model(checkpoint.path).to(device).eval()

    def forward(inputs: list[object]) -> list[object]:
        with torch.inference_mode():
            return model(torch.as_tensor(inputs, device=device)).tolist()

    return forward
```

- `load(checkpoint)` returns a **deterministic** callable from a list of inputs
  to a list of outputs, preserving length and order. Load model weights from
  `checkpoint.path`; a named-object checkpoint is a folder with `checkpoint.json`.
- `build(player, leaves)` returns what the game uses. `leaves` maps each
  checkpoint key to its forward. It can build search, sampling or an ensemble.
  Sampling and action choice belong in game code, seeded from `match.seed`.
- A bare checkpoint with no build hook receives its forward directly. Code/API
  players use `build` too; without checkpoint leaves they start no model-loading
  process and perform no GPU probe. Your build hook owns API credentials and clients.

[`agent_game.py`](../examples/agent_game.py) shows `Sampled(temperature=...)`
as identity and `Sampler.choose` as the action method.
[`agent_models.py`](../examples/agent_models.py) shows a loader. Both are used
by the runnable [`agents.py`](../examples/agents.py) experiment.

On a visible NVIDIA GPU or an Apple Silicon Mac (native arm64 Python), calls
from active games batch through one shared agent process and
`artifacts/agents/batches.json` records each forward's batch composition:

```text
$ uv run etude eval experiment.py
etude: agents: shared inference for prediction (apple); 7 games at a time; 1 inference thread
```

The startup line names the mode and why it was chosen: the GPU found
(`apple`, `nvidia`), `shared=True`, `shared=False`, `ETUDE_EVAL_GPU=none` or
`no GPU`.

On NVIDIA hardware, active games fill `max_batch` (bounded by pending matches
and host memory). On an Apple Silicon Mac, active games are sized to host cores
minus one (leaving a core for the shared agent process), bounded by pending
matches and remaining unified memory, while each forward is still capped at
`max_batch`. On other hosts (Linux CPU, Intel Mac, or x86 Python under Rosetta),
each game process caches forwards directly with one model-library thread per
game and a share of the checkpoint-byte allowance.

Honor Etude's thread environment in the loader and choose the model's device
there (`cuda`, `mps` or MLX on Apple Silicon, `cpu` otherwise): hardware
detection does not place tensors for you. Pass `Agents(shared=False)` to skip
the GPU probe and run direct per-game inference (for example with a CPU-only
loader). To play the same file direct on one host, such as a Metal-less macOS
VM, set `ETUDE_EVAL_GPU=none`, the counterpart of `CUDA_VISIBLE_DEVICES=""`:

```text
$ ETUDE_EVAL_GPU=none uv run etude eval experiment.py
etude: agents: direct inference for prediction (ETUDE_EVAL_GPU=none); 8 games at a time; 1 inference thread each
```

Pass `Agents(shared=True)` to force one shared
agent process. Without a detected GPU, games use up to half the cores and
inference uses the rest, with at least one game process and one inference thread.
Keep model imports (`torch`, `jax`, `tensorflow`, `mlx`) in the loading module
so game processes remain light.
`Agents(max_batch=..., memory_gb=..., shared=...)` tunes batch size,
checkpoint-byte allowance (default: half of host memory) and inference mode;
author code keeps one forward deterministic for every batch size. These need
`load`, and `max_batch` and `max_delay_seconds` are refused with
`shared=False`, where nothing batches.

## Results and retry

Return `Tables`, a mapping of table names to rows. Use plain identifier names
for tables/columns. A row may name `seat` (zero-based player position), `game`
(if your domain has subgames), or `group` for a team result. Group `all` contains
every seat. Etude adds `run` and `match`; every successful match must return rows.

```python
return {"scores": [{"seat": 0, "won": True}, {"seat": 1, "won": False}]}
```

A successful match is reused across an experiment's tournaments in the same
environment. Errors retain completed results; infrastructure failures retry,
ordinary game failures require `--retry-failed`. Catching a forward error in play
does not hide it from Etude's failure retention. Unrelated matches can complete.
[Limits](limits.md) covers process and API side effects.

## Querying

```python
from etude.eval.query import compare, statsdb
from etude.storage import default_root

db = statsdb("prototype", default_root())
print(db.sql("select seat, avg(won::int) from scores group by seat").fetchall())
print(compare(db, table="scores", value="won"))
```

The database exposes your tables alongside `matches`, `seats`, `players`,
`runs`, `schedule`, `tournaments` and `training_checkpoints`. Player keys name
whole compositions. `compare` and `estimate` resample whole matches for intervals;
rows within one match are correlated. Define `Rollup` for additive summaries
of large raw tables. [Tracking](tracking.md#reports) covers report declarations;
[the developer reference](dev/evaluation.md) owns table schemas and persistence.

For explicit match lists outside declarations, open
`evaluating(environment, experiment=..., matches=matches)` and call
`run.play(play, agents=Agents(...))` under `if __name__ == "__main__":`.
Games run in fresh spawned processes; play, load, build and wrapper records must
be importable module-level definitions.
