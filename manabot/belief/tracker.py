"""Stateful exact-range updates at a fixed acting-viewer boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Protocol

import numpy as np

from manabot.belief.likelihood import LikelihoodResult, PublicAction
from manabot.belief.range import ExactHandRange, RangeError


@dataclass(frozen=True, slots=True)
class HiddenPoolSnapshot:
    card_def_ids: tuple[int, ...]
    unseen_counts: tuple[int, ...]
    hand_size: int
    library_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_def_ids": list(self.card_def_ids),
            "unseen_counts": list(self.unseen_counts),
            "hand_size": self.hand_size,
            "library_size": self.library_size,
        }

    @classmethod
    def from_engine(
        cls,
        engine: Any,
        viewer: int,
        *,
        card_def_ids: tuple[int, ...] | None = None,
    ) -> HiddenPoolSnapshot:
        raw_counts, hand_size, library_size = engine.hidden_pool_summary(viewer)
        count_map = {int(definition): int(count) for definition, count in raw_counts}
        ids = card_def_ids or tuple(sorted(count_map))
        unknown = set(count_map) - set(ids)
        if unknown:
            raise RangeError(
                f"hidden pool introduced unknown definitions: {sorted(unknown)}"
            )
        return cls(
            card_def_ids=ids,
            unseen_counts=tuple(count_map.get(definition, 0) for definition in ids),
            hand_size=int(hand_size),
            library_size=int(library_size),
        )


class ActionLikelihood(Protocol):
    def evaluate(
        self,
        root_engine: Any,
        *,
        viewer: int,
        action: PublicAction,
        hand_range: ExactHandRange,
    ) -> LikelihoodResult: ...


@dataclass(slots=True)
class TrackerStats:
    updates: int = 0
    action_updates: int = 0
    hidden_draws: int = 0
    known_exits: int = 0
    known_returns: int = 0
    update_seconds: float = 0.0
    update_durations: list[float] = field(default_factory=list)
    likelihood_seconds: float = 0.0
    peak_range_bytes: int = 0
    peak_support_size: int = 0


@dataclass(frozen=True, slots=True)
class RangeTransitionRecord:
    """One replay-verifiable transition containing only viewer-safe facts."""

    sequence: int
    action: PublicAction | None
    before: HiddenPoolSnapshot
    after: HiddenPoolSnapshot
    hidden_draws: int
    known_exits: int
    known_returns: int
    posterior_digest: str
    uniform_digest: str
    posterior_normalization_error: float
    uniform_normalization_error: float
    support_size: int
    effective_range_size: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "action": self.action.to_dict() if self.action is not None else None,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "hidden_draws": self.hidden_draws,
            "known_exits": self.known_exits,
            "known_returns": self.known_returns,
            "posterior_digest": self.posterior_digest,
            "uniform_digest": self.uniform_digest,
            "posterior_normalization_error": self.posterior_normalization_error,
            "uniform_normalization_error": self.uniform_normalization_error,
            "support_size": self.support_size,
            "effective_range_size": self.effective_range_size,
        }


class ExactRangeTracker:
    """Exact posterior and matched current-snapshot uniform control."""

    def __init__(
        self,
        snapshot: HiddenPoolSnapshot,
        *,
        viewer: int,
        likelihood: ActionLikelihood | None,
        epsilon: float,
    ) -> None:
        self.viewer = viewer
        self.likelihood = likelihood
        self.epsilon = epsilon
        self.snapshot = snapshot
        self.posterior = ExactHandRange.uniform(
            snapshot.card_def_ids, snapshot.unseen_counts, snapshot.hand_size
        )
        self.uniform = self.posterior
        self.stats = TrackerStats()
        self.initial_snapshot = snapshot
        self.initial_posterior_digest = self.posterior.digest
        self.records: list[RangeTransitionRecord] = []
        self._record_size()

    @classmethod
    def from_engine(
        cls,
        engine: Any,
        *,
        viewer: int,
        likelihood: ActionLikelihood | None,
        epsilon: float,
    ) -> ExactRangeTracker:
        return cls(
            HiddenPoolSnapshot.from_engine(engine, viewer),
            viewer=viewer,
            likelihood=likelihood,
            epsilon=epsilon,
        )

    def observe(
        self,
        after_engine: Any,
        *,
        action: PublicAction | None = None,
        likelihood_root: Any | None = None,
    ) -> None:
        started = time.perf_counter()
        before = self.snapshot
        before_stats = (
            self.stats.hidden_draws,
            self.stats.known_exits,
            self.stats.known_returns,
        )
        if action is not None:
            if self.likelihood is None or likelihood_root is None:
                raise RangeError(
                    "public action update requires a likelihood root and model"
                )
            likelihood = self.likelihood.evaluate(
                likelihood_root,
                viewer=self.viewer,
                action=action,
                hand_range=self.posterior,
            )
            self.posterior = self.posterior.condition_action(
                likelihood.likelihoods,
                likelihood.legal_action_counts,
                epsilon=self.epsilon,
            )
            self.stats.action_updates += 1
            self.stats.likelihood_seconds += likelihood.seconds
        after = HiddenPoolSnapshot.from_engine(
            after_engine,
            self.viewer,
            card_def_ids=self.snapshot.card_def_ids,
        )
        self._reconcile(after)
        self.stats.updates += 1
        elapsed = time.perf_counter() - started
        self.stats.update_seconds += elapsed
        self.stats.update_durations.append(elapsed)
        self._record_size()
        self.records.append(
            RangeTransitionRecord(
                sequence=len(self.records),
                action=action,
                before=before,
                after=self.snapshot,
                hidden_draws=self.stats.hidden_draws - before_stats[0],
                known_exits=self.stats.known_exits - before_stats[1],
                known_returns=self.stats.known_returns - before_stats[2],
                posterior_digest=self.posterior.digest,
                uniform_digest=self.uniform.digest,
                posterior_normalization_error=self.posterior.normalization_error,
                uniform_normalization_error=self.uniform.normalization_error,
                support_size=self.posterior.support_size,
                effective_range_size=self.posterior.effective_range_size,
            )
        )

    def _reconcile(self, after: HiddenPoolSnapshot) -> None:
        if after.card_def_ids != self.snapshot.card_def_ids:
            raise RangeError("hidden-pool card definition order changed")
        deltas = tuple(
            current - previous
            for current, previous in zip(
                after.unseen_counts, self.snapshot.unseen_counts
            )
        )
        exits = [(index, -delta) for index, delta in enumerate(deltas) if delta < 0]
        returns = [(index, delta) for index, delta in enumerate(deltas) if delta > 0]
        for index, count in exits:
            for _ in range(count):
                self.posterior = self.posterior.remove_known(
                    self.snapshot.card_def_ids[index]
                )
                self.stats.known_exits += 1
        for index, count in returns:
            for _ in range(count):
                self.posterior = self.posterior.add_known(
                    self.snapshot.card_def_ids[index]
                )
                self.stats.known_returns += 1

        hand_after_known = (
            self.snapshot.hand_size
            - sum(count for _, count in exits)
            + sum(count for _, count in returns)
        )
        hidden_draws = after.hand_size - hand_after_known
        if hidden_draws < 0:
            raise RangeError(
                "unsupported hidden hand-size decrease without a public definition"
            )
        if after.library_size != self.snapshot.library_size - hidden_draws:
            raise RangeError(
                "unsupported selected-matchup transition: library delta does not match hidden draws"
            )
        for _ in range(hidden_draws):
            self.posterior = self.posterior.draw_unknown()
            self.stats.hidden_draws += 1
        if self.posterior.unseen_counts != after.unseen_counts:
            raise RangeError("posterior unseen pool diverged from the viewer snapshot")
        if self.posterior.hand_size != after.hand_size:
            raise RangeError("posterior hand size diverged from the viewer snapshot")
        self.uniform = ExactHandRange.uniform(
            after.card_def_ids, after.unseen_counts, after.hand_size
        )
        self.snapshot = after

    def diagnostics(self) -> dict[str, float | int]:
        probabilities = self.posterior.probabilities
        return {
            "support_size": self.posterior.support_size,
            "effective_range_size": self.posterior.effective_range_size,
            "effective_range_fraction": (
                self.posterior.effective_range_size / self.posterior.support_size
            ),
            "top_hand_mass": float(np.max(probabilities)),
            "range_bytes": self.posterior.allocated_bytes
            + self.uniform.allocated_bytes,
            "normalization_error": self.posterior.normalization_error,
        }

    def replay_receipt(self) -> dict[str, Any]:
        """Export public inputs and range digests without authority truth."""

        return {
            "schema_version": 1,
            "viewer": self.viewer,
            "initial_snapshot": self.initial_snapshot.to_dict(),
            "initial_posterior_digest": self.initial_posterior_digest,
            "transitions": [record.to_dict() for record in self.records],
            "final_posterior_digest": self.posterior.digest,
            "final_uniform_digest": self.uniform.digest,
        }

    def _record_size(self) -> None:
        self.stats.peak_range_bytes = max(
            self.stats.peak_range_bytes,
            self.posterior.allocated_bytes + self.uniform.allocated_bytes,
        )
        self.stats.peak_support_size = max(
            self.stats.peak_support_size,
            self.posterior.support_size,
            self.uniform.support_size,
        )
