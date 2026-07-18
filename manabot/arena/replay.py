"""Compact Command trace serialization and policy-free arena replay."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import gzip
import json
from pathlib import Path
from typing import Any

from etude.experience_protocol import Command
from etude.server import ASSET_MANIFEST_HASH, CONTENT_HASH
from manabot.env import Env, Match, ObservationSpace, Reward
from manabot.infra.hypers import MatchHypers, RewardHypers
from manabot.sim.teacher1_evidence import build_viewer_frame
from manabot.verify.util import INTERACTIVE_DECK, winner_from_info_or_obs

from .models import canonical_json, canonical_sha256, file_sha256


@dataclass(frozen=True)
class ArenaReplayReceipt:
    games: int
    decisions: int
    frame_mismatches: int
    command_mismatches: int
    state_mismatches: int
    outcome_mismatches: int
    private_exposures: int

    @property
    def passed(self) -> bool:
        return not any(
            (
                self.frame_mismatches,
                self.command_mismatches,
                self.state_mismatches,
                self.outcome_mismatches,
                self.private_exposures,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


def write_trace(path: Path, games: list[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(canonical_json(game) + b"\n" for game in games)
    path.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    return {"path": str(path), "sha256": file_sha256(path), "games": len(games)}


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()
    ]


def _environment(game: dict[str, Any]) -> Env:
    names = game["seat_players"]
    match = Match(
        MatchHypers(
            hero=str(names[0]),
            villain=str(names[1]),
            hero_deck=dict(INTERACTIVE_DECK),
            villain_deck=dict(INTERACTIVE_DECK),
        )
    )
    env = Env(
        match,
        ObservationSpace(),
        Reward(RewardHypers()),
        seed=int(game["deal_seed"]),
        auto_reset=False,
    )
    env.reset(seed=int(game["deal_seed"]))
    return env


def replay_games(games: list[dict[str, Any]]) -> ArenaReplayReceipt:
    counts = {
        "decisions": 0,
        "frame_mismatches": 0,
        "command_mismatches": 0,
        "state_mismatches": 0,
        "outcome_mismatches": 0,
        "private_exposures": 0,
    }
    for game in games:
        env = _environment(game)
        info: dict[str, Any] = {}
        done = False
        for revision, expected in enumerate(game["decisions"]):
            if done:
                counts["command_mismatches"] += 1
                break
            frame = build_viewer_frame(
                env.last_raw_obs,
                match_id=str(game["match_id"]),
                revision=revision,
                content_hash=CONTENT_HASH,
                asset_manifest_hash=ASSET_MANIFEST_HASH,
            )
            if canonical_sha256(frame) != expected["frame_sha256"]:
                counts["frame_mismatches"] += 1
            if frame["projection"]["opponent"].get("hand"):
                counts["private_exposures"] += 1
            if env._engine.state_digest() != expected["pre_state_digest"]:
                counts["state_mismatches"] += 1
            command = Command.model_validate(expected["command"])
            offer_ids = {int(offer["id"]) for offer in frame["offers"]}
            if (
                command.match_id != frame["match_id"]
                or command.expected_revision != revision
                or command.prompt_id != frame["prompt"]["id"]
                or command.offer_id not in offer_ids
            ):
                counts["command_mismatches"] += 1
            _, _, terminated, truncated, info = env.step(int(command.offer_id))
            done = bool(terminated or truncated)
            if env._engine.state_digest() != expected["post_state_digest"]:
                counts["state_mismatches"] += 1
            counts["decisions"] += 1
        winner = winner_from_info_or_obs(info, env.last_raw_obs) if done else None
        if winner != game["winner"]:
            counts["outcome_mismatches"] += 1
    return ArenaReplayReceipt(games=len(games), **counts)
