from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from manabot.belief.likelihood import (
    LikelihoodResult,
    PublicAction,
    PublicActionKind,
)
from manabot.belief.tracker import ExactRangeTracker, HiddenPoolSnapshot


@dataclass
class FakeEngine:
    counts: tuple[tuple[int, int], ...]
    hand_size: int
    library_size: int

    def hidden_pool_summary(
        self, viewer: int
    ) -> tuple[list[tuple[int, int]], int, int]:
        assert viewer in (0, 1)
        return list(self.counts), self.hand_size, self.library_size


class FixedLikelihood:
    def __init__(self, by_hand: dict[tuple[int, ...], float]) -> None:
        self.by_hand = by_hand
        self.calls: list[tuple[Any, int, PublicAction]] = []

    def evaluate(
        self,
        root_engine: Any,
        *,
        viewer: int,
        action: PublicAction,
        hand_range,
    ) -> LikelihoodResult:
        self.calls.append((root_engine, viewer, action))
        likelihoods = np.asarray(
            [self.by_hand[tuple(int(value) for value in key)] for key in hand_range.keys],
            dtype=np.float64,
        )
        return LikelihoodResult(
            likelihoods=likelihoods,
            legal_action_counts=np.full(hand_range.support_size, 2, dtype=np.int64),
            seconds=0.125,
        )


def test_action_update_changes_posterior_but_not_uniform_control() -> None:
    model = FixedLikelihood({(0, 1): 0.1, (1, 0): 0.9})
    engine = FakeEngine(((10, 1), (20, 1)), hand_size=1, library_size=1)
    tracker = ExactRangeTracker.from_engine(
        engine, viewer=0, likelihood=model, epsilon=0.0
    )

    tracker.observe(
        engine,
        action=PublicAction(PublicActionKind.PASS_PRIORITY),
        likelihood_root="public-root",
    )

    assert tracker.posterior.probability((1, 0)) == pytest.approx(0.9)
    assert tracker.uniform.probability((1, 0)) == pytest.approx(0.5)
    assert tracker.stats.action_updates == 1
    assert tracker.stats.likelihood_seconds == pytest.approx(0.125)
    assert model.calls == [
        ("public-root", 0, PublicAction(PublicActionKind.PASS_PRIORITY))
    ]


def test_hidden_draw_convolves_posterior_and_rebuilds_uniform() -> None:
    model = FixedLikelihood({(0, 1): 0.1, (1, 0): 0.9})
    before = FakeEngine(((10, 2), (20, 2)), hand_size=1, library_size=3)
    tracker = ExactRangeTracker.from_engine(
        before, viewer=0, likelihood=model, epsilon=0.0
    )
    tracker.observe(
        before,
        action=PublicAction(PublicActionKind.PASS_PRIORITY),
        likelihood_root="root",
    )

    after = FakeEngine(((10, 2), (20, 2)), hand_size=2, library_size=2)
    tracker.observe(after)

    assert tracker.stats.hidden_draws == 1
    assert tracker.posterior.hand_size == 2
    assert tracker.uniform.hand_size == 2
    assert tracker.posterior.probability((2, 0)) > tracker.uniform.probability((2, 0))


def test_known_exit_and_return_keep_public_pool_exact() -> None:
    snapshot = HiddenPoolSnapshot(
        card_def_ids=(10, 20),
        unseen_counts=(2, 2),
        hand_size=2,
        library_size=2,
    )
    tracker = ExactRangeTracker(
        snapshot, viewer=0, likelihood=None, epsilon=0.0
    )

    tracker.observe(FakeEngine(((10, 1), (20, 2)), hand_size=1, library_size=2))
    assert tracker.stats.known_exits == 1
    assert tracker.posterior.unseen_counts == (1, 2)

    tracker.observe(FakeEngine(((10, 2), (20, 2)), hand_size=2, library_size=2))
    assert tracker.stats.known_returns == 1
    assert tracker.posterior.unseen_counts == (2, 2)
    assert tracker.posterior.hand_size == 2


def test_tracker_reports_range_memory_and_effective_size() -> None:
    engine = FakeEngine(((10, 2), (20, 2)), hand_size=2, library_size=2)
    tracker = ExactRangeTracker.from_engine(
        engine, viewer=1, likelihood=None, epsilon=0.0
    )

    diagnostics = tracker.diagnostics()

    assert diagnostics["support_size"] == 3
    assert 1.0 <= diagnostics["effective_range_size"] <= 3.0
    assert diagnostics["range_bytes"] > 0
    assert tracker.stats.peak_range_bytes == diagnostics["range_bytes"]
