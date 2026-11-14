"""Tiny cooperative Hanabi: a shared PyTorch policy, resumable episodes and team evaluation.

Install the optional example with `uv sync --extra train --extra eval --extra hanabi`.
See docs/hanabi.md. The SDK and its offline tests never import OpenSpiel.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import pyspiel
import torch
from pydantic import BaseModel, Field

from etude.eval.match import environment
from etude.eval.records import Benchmark, CheckpointPlayer, Code, Inference, Match, Seating
from etude.eval.run import Tables
from etude.experiment import ExperimentState
from etude.jobs import evaluation
from etude.spec import Job
from etude.train.records import Step
from etude.train.tracking import track


class Rules(BaseModel, frozen=True):
    players: int = 2
    colors: int = 1
    ranks: int = 3
    hand_size: int = 2
    max_information_tokens: int = 3
    max_life_tokens: int = 3


RULES = Rules()


class Config(BaseModel):
    updates: int = Field(default=20, ge=1)
    lr: float = Field(default=0.01, gt=0)


class Counts(BaseModel):
    transitions: int = 0


class Deal(BaseModel):
    seed: int


class ArenaConfig(BaseModel):
    deals: tuple[Deal, ...] = (Deal(seed=101), Deal(seed=102))
    games: int = Field(default=2, ge=1)


@dataclass(frozen=True)
class Episode:
    score: float
    transitions: int
    loss: torch.Tensor | None


def _game() -> pyspiel.Game:
    return pyspiel.load_game("hanabi", RULES.model_dump())


def _policy(game: pyspiel.Game) -> torch.nn.Linear:
    return torch.nn.Linear(game.observation_tensor_shape()[0], game.num_distinct_actions())


def _episode(
    game: pyspiel.Game,
    policies: Sequence[torch.nn.Linear | None],
    *,
    seed: int,
    learn: bool = False,
) -> Episode:
    # Each episode's randomness is keyed by its absolute coordinate, so resume
    # needs no process-global random stream or in-flight game in its save.
    chance = random.Random(seed)
    generator = torch.Generator().manual_seed(seed)
    state = game.new_initial_state()
    log_probs: list[torch.Tensor] = []
    entropies: list[torch.Tensor] = []
    transitions = 0
    while not state.is_terminal():
        if state.is_chance_node():
            outcomes = state.chance_outcomes()
            state.apply_action(
                chance.choices([a for a, _ in outcomes], [p for _, p in outcomes])[0]
            )
            continue
        player = state.current_player()
        legal = state.legal_actions()
        policy = policies[player]
        if policy is None:
            action = chance.choice(legal)
        else:
            observation = torch.tensor(state.observation_tensor(player), dtype=torch.float32)
            logits = policy(observation)
            mask = torch.full_like(logits, -torch.inf)
            mask[legal] = 0
            distribution = torch.distributions.Categorical(logits=logits + mask)
            selected = torch.multinomial(distribution.probs, 1, generator=generator).squeeze(0)
            action = int(selected.item())
            if learn:
                log_probs.append(distribution.log_prob(selected))
                entropies.append(distribution.entropy())
        state.apply_action(action)
        transitions += 1
    score = float(state.returns()[0])  # Hanabi's reward is shared by the whole team.
    loss = (
        -torch.stack(log_probs).mean() * score - 0.01 * torch.stack(entropies).mean()
        if log_probs
        else None
    )
    return Episode(score=score, transitions=transitions, loss=loss)


def train(config: Config) -> None:
    torch.set_num_threads(1)
    game = _game()
    with track(clocks=[Step("updates", "completed team episodes")], config=config) as run:
        torch.manual_seed(run.seed)
        policy = _policy(game)
        optimizer = torch.optim.Adam(policy.parameters(), lr=config.lr)
        counts = run.resume(policy=policy, optimizer=optimizer, extra=Counts())
        for update in run.steps("updates", to=config.updates, every=1):
            episode = _episode(game, (policy, policy), seed=run.seed + update, learn=True)
            if episode.loss is None:
                raise RuntimeError("Hanabi ended without a policy decision")
            optimizer.zero_grad()
            episode.loss.backward()
            optimizer.step()
            counts.transitions += episode.transitions
            run.log(
                {
                    "team_score": episode.score,
                    "loss": episode.loss.item(),
                    "transitions": counts.transitions,
                }
            )
            run.save()
            if update % 5 == 0:
                run.checkpoint(policy=policy)
            print(f"episode {update}: team score {episode.score:g}", flush=True)
        run.checkpoint(policy=policy)


def arena(config: ArenaConfig) -> Benchmark:
    # Environment config fixes the public game rules; no game is run or read here.
    return Benchmark(
        name="Hanabi team",
        environment=environment("tiny-hanabi", config=RULES),
        seatings=(
            Seating(
                name="self-play",
                players=(Inference(), Inference()),
                config=config.deals,
                games=config.games,
            ),
            Seating(
                name="random teammate",
                players=(Inference(), Code(name="random")),
                config=config.deals,
                games=config.games,
            ),
        ),
    )


def play(match: Match[Deal]) -> Tables:
    torch.set_num_threads(1)
    game = _game()
    policies: list[torch.nn.Linear | None] = []
    for player in match.players:
        if isinstance(player, CheckpointPlayer):
            policy = _policy(game)
            folder = Path(unquote(urlparse(player.checkpoint.artifact.uri).path))
            policy.load_state_dict(
                torch.load(folder / "policy.pt", map_location="cpu", weights_only=True)
            )
            policy.eval()
            policies.append(policy)
        else:
            policies.append(None)
    rows: list[dict[str, bool | int | float | str | None]] = []
    with torch.no_grad():
        for game_index in range(match.games):
            episode = _episode(game, policies, seed=match.config.seed + game_index)
            rows.append(
                {
                    "game": game_index,
                    "group": "all",
                    "score": episode.score,
                    "transitions": episode.transitions,
                    "deal": match.config.seed,
                }
            )
    return {"teams": rows}


def eval(state: ExperimentState) -> Job:
    checkpoints = tuple(
        c for run in state.runs for c in run.checkpoints if c.ordinal == 1 or c == run.final
    )
    return evaluation(
        arena, config=ArenaConfig(), play=play, checkpoints=checkpoints, max_hours=0.01
    )
