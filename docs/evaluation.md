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

Each game process runs its `play` tasks on one `asyncio` event loop. Calls
queued in the same loop turn flush together. Avoid blocking the event loop between `await`s.

`Agents(load=load, devices=("cuda", "metal"))` shares inference on a visible
NVIDIA GPU or an Apple Silicon Mac running native arm64 Python. This is the
default. Use `("cuda",)` for a CUDA-only loader, `("metal",)` for an Apple GPU
loader, or `("cpu",)` for direct inference without a probe. Etude drops
`"cpu"` from mixed declarations.

With a matching accelerator, game processes send their pending calls in one
message to a shared agent process, which merges ready requests by checkpoint
up to `max_batch` rows and writes observations to `artifacts/agents/batches.json`.
Without a matching accelerator, each game process caches its own checkpoints,
uses one model-library thread, and gets a share of the checkpoint allowance
(half of host memory by default). Both modes split oversized requests with one
input per row and return one ordered reply.

Pending matches, CPUs and available host memory bound the game process count.
With `play`, shared inference divides `max_batch` across game processes,
rounding up. Direct checkpoint inference gives each process up to `max_batch`
matches. Shared inference leaves a core for the agent process when possible.
The startup line shows the process and thread counts, the inference mode and
its reason, and the initial `target_matches` for each play mode.

Choose tensor placement in `load` and honor Etude's thread environment there.
Keep model imports in the loading module so shared game processes stay light.
`devices`, `max_batch` and `memory_gb` require a checkpoint loader.

## Vectorized engines

Run the [vectorized example](../examples/vectorized.py) with
`uv run etude eval examples/vectorized.py --source /tmp/vectorized`.

For engines that step many games together and collate observations themselves,
write an async generator `play_many(matches)` instead of `play(match, agents)`:

```python
from collections.abc import AsyncIterator

from etude.eval.agents import ForwardError
from etude.eval.game import MatchStream
from etude.eval.records import Match
from etude.eval.result import Tables
from etude.jobs import Agents, EvalJob

ENGINE_MAX_SLOTS = 128  # Size for the engine's arena memory budget.


async def play_many(
    matches: MatchStream[Rules],
) -> AsyncIterator[tuple[Match[Rules], Tables | Exception]]:
    pool = RolloutPool(slots=min(ENGINE_MAX_SLOTS, matches.target_matches))
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

Etude schedules the matches, caches checkpoints and stores each outcome as
the generator yields it.

- `matches.target_matches` suggests how many matches this process should hold
  at once, from its initial share of the tournament, capped at `max_batch`
  when a loader is declared. Size an engine with several games per match
  accordingly.
  Call `await matches.take(n)` with the number of free slots to get up to `n`
  pending `(match, agents)` pairs. In both play modes, processes take their first
  matches after loading and setting up the game code. Etude shares pending
  matches among empty processes and accounts for held matches when refilling.
  An empty grant for `n > 0` means the tournament has no pending matches or
  the run is stopping.
- `await forward(inputs, rows=...)` defaults `rows` to `len(inputs)`. Pass
  `rows=` for a pre-collated batch so the agent process never merges calls past
  `max_batch` rows and reports real row counts. A single pre-collated call
  above `max_batch` rows runs alone. An engine can choose smaller batches
  when its model needs a hard limit. Group ready rows by their shared `Forward`
  object to batch calls for the same checkpoint. When Etude merges requests,
  the `ModelForward` returned by `load` can receive several pre-collated inputs
  in one list. Return one output per input, in the same order.
- Grouped `play` and `play_many` tournaments using the same `Agents(load=...)`
  share one agent process in shared inference, so their input elements must be
  compatible with the same `ModelForward`.
- Yield `(match, tables)` or `(match, Result(...))` with the match from `take` or
  a copy whose key identifies exactly one held match. Yield as each slot
  finishes so it can backfill without waiting for stragglers.
- Yield `(match, error)` for an ordinary game error and the remaining slots
  can keep stepping and take pending matches. An uncaught exception, an invalid match in a yield,
  or returning with held or pending matches fails the generator and its
  held matches. A transient infrastructure failure such as
  `RetryRequested` interrupts the group and retains unfinished matches for retry.

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
environment. An ordinary `play` error fails its match and other matches keep
playing. A refused table or an invalid outcome type in either play mode fails
its match and stops grants for the tournament while held matches finish.
Errors importing or setting up authored game code fail its tournament while
sibling tournaments continue. A loader import error fails its tournament in
direct mode and the group in shared mode. Completed results are retained,
and matches that were never granted remain pending.
Infrastructure failures retry, and ordinary failed matches require
`--retry-failed`. A forward failure raises
`ForwardError` (with `.checkpoint` naming the checkpoint key) at the awaiting
call. In shared inference, a game process sends each checkpoint's calls from
one event-loop turn as one request, so a serialization error in one input or
output raises a non-retryable `ForwardError` at every call in that request.
Other checkpoints, other turns and other processes continue. A lost connection interrupts the
group and leaves unfinished matches eligible for retry.
Etude records a failed match even when `play` catches `ForwardError`.
In `play_many`, catch `ForwardError`, drop affected slots and yield `(match,
error)` while unrelated matches complete. A checkpoint load failure applies
to unfinished matches seating
that checkpoint, including matches that yield fallback tables.
[Limits](limits.md) covers process, event-loop and API side effects.

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
