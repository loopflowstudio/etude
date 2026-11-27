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
checkpoints=saved, max_hours=...)` declares the match generator and how to play
(`play=` for `async def play(match, agents)`, or `play_many=` for a vectorized
engine). For a round robin, pass `enter=(a, b, c)` instead of checkpoints.
Compatible jobs share compute automatically. The launcher prints one run address
for the group. Its directory holds the launch declaration, writer and outcome,
with the tournaments’ result directories beneath it. A retry can reuse their
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
from collections.abc import Sequence
from random import Random

from etude.eval.agents import Forward
from etude.eval.records import Match
from etude.eval.result import Tables


async def play(match: Match, agents: Sequence[Forward]) -> Tables:
    config_random = Random(f"{match.seed}:config")
    engine_random = Random(f"{match.seed}:engine")
    player_random = [Random(f"{match.seed}:player:{i}") for i in range(len(agents))]
    # Resolve random decks from config_random, then await agents[seat]([obs])
    # using engine_random and each player's independent stream.
    return await run_game(match.config, agents, config_random, engine_random, player_random)
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

```python
# models.py: may import Torch and choose the model's device
from etude.checkpoints import CheckpointRef
from etude.eval.agents import ModelForward


def load(checkpoint: CheckpointRef) -> ModelForward:
    model = load_weights(checkpoint.path)

    def forward(inputs: list[object]) -> list[object]:
        return model.predict(inputs)

    return forward
```

```python
# game.py: keep model libraries out of this module
from collections.abc import Sequence
from random import Random

from etude.eval.agents import Forward
from etude.eval.records import Match
from etude.eval.result import Tables


async def play(match: Match, agents: Sequence[Forward]) -> Tables:
    rng = Random(match.seed)
    [logits] = await agents[0]([initial_observation(match.config)])
    action = sample(logits, rng)
    return {"scores": [{"seat": 0, "score": score(action)}]}
```

Per-match games use `async def play(match, agents)`. Each seated player gets one
author object in `agents`, in seat order. A synchronous `def play` is refused
when the job is declared.

- `load(checkpoint)` returns a **deterministic** `ModelForward` from a list of
  inputs to a list of outputs, preserving length and order independently of
  batch composition. Load model weights from `checkpoint.path`; a named-object
  checkpoint is a folder with `checkpoint.json`.
- `build(player, leaves)` returns what the game uses. `leaves` maps each
  checkpoint key to its awaitable `Forward`. Within a game process, every match
  seating a checkpoint shares that checkpoint's `Forward` object. Sampling and
  action choice belong in game code, seeded from `match.seed`.
- A bare checkpoint with no build hook receives its `Forward` directly. Code and
  API players use `build` too; without checkpoint leaves they run one game per
  process, start no model-loading process and perform no GPU probe. Your build
  hook owns API credentials and clients.

[`agent_game.py`](../examples/agent_game.py) shows `Sampled(temperature=...)`
as identity and `Sampler.choose` as the action method.
[`agent_models.py`](../examples/agent_models.py) shows a loader. Both are used
by the runnable [`agents.py`](../examples/agents.py) experiment.

Each game process runs many `play` tasks on one `asyncio` event loop. When no
running game in the process can add another input, pending calls flush together
with no timer. Honor Etude's thread environment in the loader and choose the
model's device there: hardware capacity does not place tensors for you. Keep
model imports in the loading module so shared-process GPU game processes remain
light, and avoid blocking the event loop between `await`s.
`Agents(max_batch=..., memory_gb=...)` tunes batch rows and checkpoint memory;
author code keeps `load`'s forward deterministic for every batch size. See
[How a forward travels](#how-a-forward-travels) for direct vs shared-process
placement and array rules.

## Vectorized engines

Run the [vectorized example](../examples/vectorized.py):

```sh
uv run etude eval examples/vectorized.py --source /tmp/vectorized
```

A `play_many` tournament with a loader runs inference directly inside one
game process on CPU, Apple Silicon, and NVIDIA GPU hosts. Etude prints the
placement to stderr.

```text
etude: agents: direct inference for vector (play_many); 1 game process, slots requested by play_many; 15 inference threads
```

Direct-mode `threads` sets the game process's model and BLAS thread environment
variables (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, etc.)
before author modules are imported.

For engines that step many games together and collate observations themselves,
write an async generator `play_many(matches)` instead of `play(match, agents)`:

```python
from collections.abc import AsyncIterator

from etude.eval.agents import Forward, ForwardError
from etude.eval.game import Matches
from etude.eval.records import Match
from etude.eval.result import Tables
from etude.jobs import Agents, EvalJob


async def play_many(
    matches: Matches[Rules],
) -> AsyncIterator[tuple[Match[Rules], Tables | Exception]]:
    pool = RolloutPool(slots=256)
    while True:
        for match, agents in await matches.take(pool.free):
            pool.load(match, agents)
        if not pool.active:
            return
        for forward, batch, rows, slots in pool.ready_batches():
            try:
                [outputs] = await forward([batch], rows=rows)
            except ForwardError as error:
                for match in pool.drop(slots):
                    yield match, error
                continue
            for match, tables in pool.step(slots, outputs):
                yield match, tables


job = EvalJob(
    tournament=PANEL,
    play_many=play_many,
    agents=Agents(load=load, max_batch=256),
    checkpoints=saved,
    max_hours=1,
)
```

Give `EvalJob` either `play=` or `play_many=`, never both. Etude still owns
scheduling, checkpoint caching and eviction, durable match storage and failure
retention.

- `await matches.take(n)` pulls up to `n` pending `(match, agents)` pairs in one
  step. Returning `[]` for `n > 0` means no pending matches remain (or the run
  is stopping).
- `await forward(inputs, rows=...)` defaults `rows` to `len(inputs)`. Pass
  `rows=` for a pre-collated batch so batching never merges calls past
  `max_batch` rows and reports real row counts. A single call above `max_batch`
  rows runs alone. Because every match in the process shares one `Forward`
  object per checkpoint, engines can group ready rows by forward identity.
- Yield `(match, tables)` or `(match, Result(...))` with the match object
  returned by `take`. Yield as each slot finishes so it can backfill
  without waiting for stragglers.
- Yield `(match, error)` to fail one match. The remaining slots keep stepping
  and can take pending matches. An uncaught exception fails every match the
  generator holds and stops new grants for that tournament.
  Yielding an unknown or already-yielded match fails the generator and its held
  matches; returning with held matches unyielded fails each unyielded match.

## How a forward travels

In `play_many`, calling `forward(...)` submits immediately without waiting for
an event-loop barrier, so a vectorized engine can split its pool in two and
overlap stepping one half with the other half's forward:

```python
import numpy as np

from etude.eval.agents import Forward


async def advance_halves(
    forward: Forward,
    half_a: np.ndarray,
    half_b: np.ndarray,
    rows: int,
) -> tuple[object, object]:
    # half_a stays read-only until its reply arrives.
    pending_a = forward([half_a], rows=rows)
    step_half(half_b)  # runs while half_a's forward executes on the inference thread
    [out_a] = await pending_a  # half_a is writable again; out_a holds NumPy leaves
    pending_b = forward([half_b], rows=rows)
    step_half(half_a)
    [out_b] = await pending_b
    return out_a, out_b
```

Calls queued during inference can merge up to `max_batch` rows per checkpoint.
In `play`, Etude locks inputs on the call and batches concurrent matches at
the end of the event-loop turn.

- **NumPy boundary.** Across both direct and shared-process transports, array
  leaves at any depth of `tuple`, `list` and `dict` values reach `load`'s
  forward as `numpy.ndarray` and return to game code as owned writable
  `numpy.ndarray`. Non-NumPy or lazy outputs (such as MLX, Torch or JAX arrays)
  exporting the buffer protocol or CPU `__dlpack__` are materialized into owned
  `numpy.ndarray` on the inference thread before leaving (with their backing
  views released on that thread), and non-NumPy input leaves are converted
  before submit. Non-CPU device tensors (CUDA/MPS) in inputs or outputs fail
  with `ForwardError` asking `load` or game code to move them to CPU/NumPy.
  Etude imports no model library.
- **Zero-copy inputs on direct inference.** A top-level C-contiguous non-object
  `numpy.ndarray` passes to the inference thread by pointer with no copy and no
  pickle; nested C-contiguous NumPy leaves inside lists, tuples, dicts and
  dataclass instances also move without copying their payload bytes. Non-C-contiguous arrays, non-NumPy array leaves,
  mutable `bytearray`s and plain Python containers are copied; unpicklable
  inputs fail that call with `ForwardError`.
- **Read-only in flight, no aliasing.** In both `play` and `play_many`, a
  writeable NumPy array handed to `forward(...)` is marked read-only on the call
  until its answer arrives—writing to it while in flight raises `ValueError`—and
  restored afterwards; an array already read-only on entry stays read-only.
  Cancelling the await does not unlock the array early: it unlocks once the
  inference thread finishes that forward. The model receives a read-only view,
  so an in-place mutation such as `x /= 255` inside `load`'s forward raises
  `ValueError` (delivered to the game as `ForwardError`) without modifying the
  game's array. A loader calling `torch.from_numpy(x)` on CPU sees PyTorch's
  one-time read-only warning; copying to a device or calling `torch.tensor(x)`
  avoids it. Model outputs are copied into owned writable arrays before crossing
  back to the game loop, so outputs never alias an in-flight input array or a
  reused buffer inside `load`.

When a group contains both kinds of play function, Etude finishes its `play`
tournaments first and then runs its `play_many` tournaments on the same host.
Each phase gets the host's thread budget and the full checkpoint allowance
from `Agents.memory_gb`. A checkpoint used in both phases loads in each phase.

| Host | Play function | Inference transport |
|---|---|---|
| Any host (CPU, Mac, GPU) | `play_many` | Direct inference thread in one game process. It serves each tournament in turn, keeps checkpoints loaded across tournaments, and gets `max(1, cpus - 1)` inference threads. |
| No NVIDIA GPU seen | `play` | Direct inference thread in each game process. Each process serves the tournament queues in turn and reuses its loaded checkpoints. |
| Visible NVIDIA GPU | `play` | Shared agent process over a private Unix socket. |

Code-only players (no loader) start neither an inference thread nor an agent
process. Code-only `play_many` tournaments run in turn in one game process.

## Results and retry

Return `Tables`, a mapping of table names to rows (or `Result(tables=...,
blobs=..., execution=...)` from `etude.eval.result` when keeping blobs or
execution provenance). Use plain identifier names for tables/columns. A row may
name `seat` (zero-based player position), `game` (if your domain has subgames),
or `group` for a team result. Group `all` contains every seat. Etude adds `run`
and `match`; every successful match must return rows.

```python
return {"scores": [{"seat": 0, "won": True}, {"seat": 1, "won": False}]}
```

A successful match is reused across an experiment's tournaments in the same
environment. If a game process exits unexpectedly or a wait for results expires,
queued and in-flight matches can retry. A stop records `stopped` for the group
and the tournaments it interrupts. Tournaments that already finished or failed
keep their status. Matches dropped from the queue after an ordinary game
failure remain unplayed. Retrying the recorded failure requires `--retry-failed`.

An undecodable request or reply on shared inference closes that game process's
connection and fails its pending forwards. Other game processes keep playing.
A failed forward raises `ForwardError` at the awaiting call, with the checkpoint
key on `.checkpoint`. In `play`, Etude records the match as failed even if the
game catches the error. In `play_many`, catch it to drop affected slots and
yield `(match, error)` while unrelated matches complete. [Limits](limits.md)
covers process, event-loop and API side effects.

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
`run.play(play, agents=Agents(...))` or `run.play_many(play_many,
agents=Agents(...))` under `if __name__ == "__main__":`. Games run in fresh
spawned processes; `play`, `play_many`, `load`, `build` and wrapper records must
be importable module-level definitions.
