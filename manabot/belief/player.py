"""Matched exact-range and uniform-determinization manabot players."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import resource
import sys
import time
from typing import Any

import numpy as np

from manabot.belief.likelihood import PublicAction, PublicActionKind
from manabot.belief.range import RangeError
from manabot.belief.tracker import (
    ActionLikelihood,
    ExactRangeTracker,
    HiddenPoolSnapshot,
)
from manabot.env import Env
from manabot.sim.flat_mc import (
    DEFAULT_MAX_PLAYOUT_STEPS,
    DEFAULT_ROLLOUTS_PER_WORLD,
    SearchStats,
)
from manabot.sim.teacher1_evidence import build_command, build_viewer_frame


class RangeSampling(str, Enum):
    BELIEF = "belief"
    UNIFORM = "uniform"


@dataclass(slots=True)
class BeliefPlayerStats:
    search: SearchStats = field(default_factory=SearchStats)
    end_to_end_seconds: float = 0.0
    end_to_end_decision_seconds: list[float] = field(default_factory=list)
    update_seconds: list[float] = field(default_factory=list)
    command_seconds: list[float] = field(default_factory=list)
    sampled_worlds: int = 0
    unique_sampled_worlds: int = 0
    installed_hand_mismatches: int = 0
    commands_emitted: int = 0
    range_updates: int = 0
    action_updates: int = 0
    hidden_draws: int = 0
    known_exits: int = 0
    known_returns: int = 0
    likelihood_seconds: float = 0.0
    peak_range_bytes: int = 0
    peak_support_size: int = 0


class ExactRangePlayer:
    """A stateful exact-range player with a matched uniform sampling arm."""

    def __init__(
        self,
        sims: int,
        *,
        likelihood: ActionLikelihood,
        sampling: RangeSampling | str = RangeSampling.BELIEF,
        epsilon: float = 0.05,
        rollouts_per_world: int = DEFAULT_ROLLOUTS_PER_WORLD,
        max_steps: int = DEFAULT_MAX_PLAYOUT_STEPS,
        seed: int = 0,
    ) -> None:
        if sims < 1:
            raise ValueError("sims must be positive")
        self.sampling = RangeSampling(sampling)
        self.likelihood = likelihood
        self.epsilon = epsilon
        self.sims = sims
        self.rollouts = max(1, min(rollouts_per_world, sims))
        self.worlds = max(1, sims // self.rollouts)
        self.max_steps = max_steps
        self.seed = seed
        self.calls = 0
        self.seat: int | None = None
        self.tracker: ExactRangeTracker | None = None
        self.stats = BeliefPlayerStats()
        self.last_scores: np.ndarray | None = None
        self.last_command: dict[str, Any] | None = None
        self._pending_likelihood_root: Any | None = None
        self._prepared_space_kind: int | None = None
        self._decision_started: float | None = None
        self.completed_replays: list[dict[str, Any]] = []

    @property
    def search_stats(self) -> SearchStats:
        return self.stats.search

    def start_game(self, env: Env, seat: int) -> None:
        self.seat = seat
        self.tracker = ExactRangeTracker.from_engine(
            env._engine,
            viewer=seat,
            likelihood=self.likelihood,
            epsilon=self.epsilon,
        )
        self._pending_likelihood_root = None
        self._prepared_space_kind = None
        self._decision_started = None

    def finish_game(self, *, game_index: int, seed: int) -> None:
        if self.tracker is None:
            raise RuntimeError("start_game must run before finish_game")
        receipt = self.tracker.replay_receipt()
        receipt["game_index"] = game_index
        receipt["seed"] = seed
        self.completed_replays.append(receipt)

    def prepare_step(self, env: Env, acting: int, action: int) -> None:
        del action
        if self.seat is None or acting == self.seat:
            return
        viewer_root = env._engine.observation_for_player(self.seat)
        space_kind = int(viewer_root.action_space.action_space_type)
        self._prepared_space_kind = space_kind
        if space_kind == 1 and self._pending_likelihood_root is None:
            self._pending_likelihood_root = env._engine.clone_env()

    def observe_step(self, env: Env, acting: int) -> None:
        observe_started = time.perf_counter()
        if self.tracker is None or self.seat is None:
            raise RuntimeError("start_game must run before observe_step")
        before = self.tracker.snapshot
        after = HiddenPoolSnapshot.from_engine(
            env._engine,
            self.seat,
            card_def_ids=before.card_def_ids,
        )
        after_map = dict(zip(after.card_def_ids, after.unseen_counts))
        exits = [
            definition
            for definition, previous in zip(before.card_def_ids, before.unseen_counts)
            for _ in range(max(0, previous - after_map.get(definition, 0)))
        ]
        public_action: PublicAction | None = None
        likelihood_root: Any | None = None
        after_view = env._engine.observation_for_player(self.seat)
        after_space_kind = int(after_view.action_space.action_space_type)
        if acting != self.seat and exits:
            if len(exits) != 1:
                raise RangeError(
                    "one public commitment changed multiple hidden definitions"
                )
            public_action = PublicAction(
                PublicActionKind.COMMIT_DEFINITION, card_def_id=exits[0]
            )
            likelihood_root = self._pending_likelihood_root
            if likelihood_root is None:
                raise RangeError("public hand exit has no retained pre-commitment root")
            self._pending_likelihood_root = None
        elif (
            acting != self.seat
            and self._prepared_space_kind == 1
            and after_space_kind != 4
        ):
            public_action = PublicAction(PublicActionKind.PASS_PRIORITY)
            likelihood_root = self._pending_likelihood_root
            if likelihood_root is None:
                raise RangeError("public pass has no retained pre-commitment root")
            self._pending_likelihood_root = None
        if public_action is None and after == before:
            # Intermediate target/combat prompts are intentionally invisible
            # to the fixed-viewer history. Preserve a pending cast root, but
            # do not expose the number or shape of private prompt fragments.
            self._prepared_space_kind = None
            return
        tracker_before = (
            self.tracker.stats.action_updates,
            self.tracker.stats.hidden_draws,
            self.tracker.stats.known_exits,
            self.tracker.stats.known_returns,
            self.tracker.stats.likelihood_seconds,
        )
        self.tracker.observe(
            env._engine,
            action=public_action,
            likelihood_root=likelihood_root,
        )
        tracker_after = self.tracker.stats
        self.stats.range_updates += 1
        self.stats.action_updates += tracker_after.action_updates - tracker_before[0]
        self.stats.hidden_draws += tracker_after.hidden_draws - tracker_before[1]
        self.stats.known_exits += tracker_after.known_exits - tracker_before[2]
        self.stats.known_returns += tracker_after.known_returns - tracker_before[3]
        self.stats.likelihood_seconds += (
            tracker_after.likelihood_seconds - tracker_before[4]
        )
        self.stats.peak_range_bytes = max(
            self.stats.peak_range_bytes, tracker_after.peak_range_bytes
        )
        self.stats.peak_support_size = max(
            self.stats.peak_support_size, tracker_after.peak_support_size
        )
        self._prepared_space_kind = None
        self.stats.update_seconds.append(time.perf_counter() - observe_started)

    def act(self, env: Env, obs: dict[str, np.ndarray]) -> int:
        del obs
        if self.tracker is None:
            raise RuntimeError("start_game must run before act")
        started = time.perf_counter()
        self._decision_started = started
        self.calls += 1
        call_seed = (self.seed * 1_000_003 + self.calls) & 0xFFFFFFFFFFFFFFFF
        hand_range = (
            self.tracker.posterior
            if self.sampling is RangeSampling.BELIEF
            else self.tracker.uniform
        )
        sampled_keys = hand_range.sample(self.worlds, seed=call_seed)
        hands = [hand_range.as_definition_counts(hand) for hand in sampled_keys]
        seed_rng = np.random.default_rng(call_seed ^ 0x9E3779B97F4A7C15)
        world_seeds = [
            int(value)
            for value in seed_rng.integers(
                0, np.iinfo(np.uint64).max, size=self.worlds, dtype=np.uint64
            )
        ]
        search_started = time.perf_counter()
        scores, simulations, cap_hits, installed = env._engine.flat_mc_scores_for_hands(
            hands,
            world_seeds,
            self.rollouts,
            self.max_steps,
        )
        search_elapsed = time.perf_counter() - search_started
        expected = [
            sorted(definition for definition, count in hand for _ in range(int(count)))
            for hand in hands
        ]
        mismatches = sum(
            sorted(actual) != wanted for actual, wanted in zip(installed, expected)
        )
        if mismatches:
            self.stats.installed_hand_mismatches += mismatches
            raise RuntimeError(
                "managym installed a different hand than the sampled range key"
            )
        self.stats.search.decisions += 1
        self.stats.search.seconds += search_elapsed
        self.stats.search.simulations += int(simulations)
        self.stats.search.cap_hits += int(cap_hits)
        self.stats.search.decision_seconds.append(search_elapsed)
        self.stats.sampled_worlds += len(sampled_keys)
        self.stats.unique_sampled_worlds += len(set(sampled_keys))
        self.last_scores = np.asarray(scores, dtype=np.float32)
        return int(np.argmax(scores))

    def command_for_action(
        self,
        raw: Any,
        action: int,
        *,
        match_id: str,
        revision: int,
        content_hash: str,
        asset_manifest_hash: str,
    ) -> dict[str, Any]:
        command_started = time.perf_counter()
        frame = build_viewer_frame(
            raw,
            match_id=match_id,
            revision=revision,
            content_hash=content_hash,
            asset_manifest_hash=asset_manifest_hash,
        )
        command = build_command(frame, action)
        if int(command["offer_id"]) not in {
            int(offer["id"]) for offer in frame["offers"]
        }:
            raise RuntimeError("command references an offer outside the viewer frame")
        self.last_command = command
        self.stats.commands_emitted += 1
        self.stats.command_seconds.append(time.perf_counter() - command_started)
        if self._decision_started is not None:
            elapsed = time.perf_counter() - self._decision_started
            self.stats.end_to_end_seconds += elapsed
            self.stats.end_to_end_decision_seconds.append(elapsed)
            self._decision_started = None
        return command

    def evidence_stats(self) -> dict[str, Any]:
        decision_seconds = np.asarray(
            self.stats.search.decision_seconds, dtype=np.float64
        )
        end_to_end_seconds = np.asarray(
            self.stats.end_to_end_decision_seconds, dtype=np.float64
        )
        update_seconds = np.asarray(self.stats.update_seconds, dtype=np.float64)
        command_seconds = np.asarray(self.stats.command_seconds, dtype=np.float64)
        diagnostics = self.tracker.diagnostics() if self.tracker is not None else {}
        return {
            "sampling": self.sampling.value,
            "decisions": self.stats.search.decisions,
            "simulations": self.stats.search.simulations,
            "cap_hits": self.stats.search.cap_hits,
            "search_seconds": self.stats.search.seconds,
            "end_to_end_seconds": self.stats.end_to_end_seconds,
            "p50_search_latency_ms": (
                float(np.quantile(decision_seconds, 0.50)) * 1000
                if decision_seconds.size
                else None
            ),
            "p95_search_latency_ms": (
                float(np.quantile(decision_seconds, 0.95)) * 1000
                if decision_seconds.size
                else None
            ),
            "p50_end_to_end_latency_ms": _quantile_ms(end_to_end_seconds, 0.50),
            "p95_end_to_end_latency_ms": _quantile_ms(end_to_end_seconds, 0.95),
            "p50_belief_update_latency_ms": _quantile_ms(update_seconds, 0.50),
            "p95_belief_update_latency_ms": _quantile_ms(update_seconds, 0.95),
            "p50_command_latency_ms": _quantile_ms(command_seconds, 0.50),
            "p95_command_latency_ms": _quantile_ms(command_seconds, 0.95),
            "rollouts_per_second": (
                self.stats.search.simulations / self.stats.search.seconds
                if self.stats.search.seconds
                else None
            ),
            "sampled_worlds": self.stats.sampled_worlds,
            "unique_sampled_worlds": self.stats.unique_sampled_worlds,
            "sample_collision_rate": (
                1.0 - self.stats.unique_sampled_worlds / self.stats.sampled_worlds
                if self.stats.sampled_worlds
                else 0.0
            ),
            "installed_hand_mismatches": self.stats.installed_hand_mismatches,
            "commands_emitted": self.stats.commands_emitted,
            "range_updates": self.stats.range_updates,
            "action_updates": self.stats.action_updates,
            "hidden_draws": self.stats.hidden_draws,
            "known_exits": self.stats.known_exits,
            "known_returns": self.stats.known_returns,
            "likelihood_seconds": self.stats.likelihood_seconds,
            "peak_range_bytes": self.stats.peak_range_bytes,
            "peak_support_size": self.stats.peak_support_size,
            "peak_rss_bytes": _peak_rss_bytes(),
            "replay_games": len(self.completed_replays),
            "current_range": diagnostics,
        }

    def replay_receipts(self) -> list[dict[str, Any]]:
        return list(self.completed_replays)


class UniformRangePlayer(ExactRangePlayer):
    def __init__(
        self, sims: int, *, likelihood: ActionLikelihood, **kwargs: Any
    ) -> None:
        super().__init__(
            sims,
            likelihood=likelihood,
            sampling=RangeSampling.UNIFORM,
            **kwargs,
        )


def _quantile_ms(values: np.ndarray, quantile: float) -> float | None:
    return float(np.quantile(values, quantile)) * 1000 if values.size else None


def _peak_rss_bytes() -> int:
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024
