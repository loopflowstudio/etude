"""Exact opponent-hand ranges over card-definition count vectors.

The representation collapses physically indistinguishable copies while
retaining their exact hypergeometric multiplicity.  It is intentionally
unpruned: exceeding the selected matchup's memory budget is evidence that the
exact boundary must change, not permission to silently approximate it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Iterable, TypeAlias

import numpy as np
from numpy.typing import NDArray

HandKey: TypeAlias = tuple[int, ...]


class RangeError(ValueError):
    """Raised when a range transition would violate its exact support."""


def _logsumexp(values: NDArray[np.float64]) -> float:
    if values.size == 0:
        raise RangeError("exact range has empty support")
    maximum = float(np.max(values))
    if not math.isfinite(maximum):
        raise RangeError("exact range has no finite mass")
    return maximum + math.log(float(np.exp(values - maximum).sum()))


def _log_choose(total: int, selected: int) -> float:
    if selected < 0 or selected > total:
        return -math.inf
    return (
        math.lgamma(total + 1)
        - math.lgamma(selected + 1)
        - math.lgamma(total - selected + 1)
    )


def _bounded_compositions(counts: tuple[int, ...], total: int) -> list[HandKey]:
    keys: list[HandKey] = []
    prefix = [0] * len(counts)

    def visit(index: int, remaining: int) -> None:
        if index == len(counts) - 1:
            if remaining <= counts[index]:
                prefix[index] = remaining
                keys.append(tuple(prefix))
            return
        suffix_capacity = sum(counts[index + 1 :])
        lower = max(0, remaining - suffix_capacity)
        upper = min(counts[index], remaining)
        for selected in range(lower, upper + 1):
            prefix[index] = selected
            visit(index + 1, remaining - selected)

    if counts:
        visit(0, total)
    elif total == 0:
        keys.append(())
    return keys


@dataclass(frozen=True, slots=True)
class ExactHandRange:
    """A normalized exact distribution over opponent hand compositions."""

    card_def_ids: tuple[int, ...]
    unseen_counts: tuple[int, ...]
    hand_size: int
    keys: NDArray[np.int16]
    log_weights: NDArray[np.float64]

    def __post_init__(self) -> None:
        if tuple(sorted(self.card_def_ids)) != self.card_def_ids:
            raise RangeError("card_def_ids must be strictly sorted")
        if len(set(self.card_def_ids)) != len(self.card_def_ids):
            raise RangeError("card_def_ids must be unique")
        if len(self.unseen_counts) != len(self.card_def_ids):
            raise RangeError("unseen_counts must match card_def_ids")
        if any(count < 0 for count in self.unseen_counts):
            raise RangeError("unseen counts cannot be negative")
        if self.hand_size < 0 or self.hand_size > sum(self.unseen_counts):
            raise RangeError("hand_size is outside the unseen pool")
        if self.keys.ndim != 2 or self.keys.shape[1] != len(self.card_def_ids):
            raise RangeError("keys have the wrong shape")
        if self.log_weights.shape != (self.keys.shape[0],):
            raise RangeError("log_weights must have one entry per key")
        if self.keys.shape[0] == 0:
            raise RangeError("exact range has empty support")
        if np.any(self.keys < 0):
            raise RangeError("hand counts cannot be negative")
        if np.any(self.keys.sum(axis=1) != self.hand_size):
            raise RangeError("every key must match hand_size")
        if np.any(self.keys > np.asarray(self.unseen_counts, dtype=np.int16)):
            raise RangeError("a key exceeds the unseen pool")
        normalizer = _logsumexp(self.log_weights)
        if not math.isclose(normalizer, 0.0, abs_tol=1e-10):
            raise RangeError(f"log weights are not normalized: log_z={normalizer}")

    @classmethod
    def uniform(
        cls,
        card_def_ids: Iterable[int],
        unseen_counts: Iterable[int],
        hand_size: int,
    ) -> ExactHandRange:
        """Build the exact combinatorial range for one public snapshot."""

        ids = tuple(int(value) for value in card_def_ids)
        counts = tuple(int(value) for value in unseen_counts)
        if len(ids) != len(counts):
            raise RangeError("card_def_ids and unseen_counts must have equal length")
        if hand_size < 0 or hand_size > sum(counts):
            raise RangeError("hand_size is outside the unseen pool")
        key_list = _bounded_compositions(counts, hand_size)
        if not key_list:
            raise RangeError("no hand compositions satisfy the public snapshot")
        keys = np.asarray(key_list, dtype=np.int16)
        log_weights = np.asarray(
            [
                sum(
                    _log_choose(count, selected) for count, selected in zip(counts, key)
                )
                for key in key_list
            ],
            dtype=np.float64,
        )
        log_weights -= _logsumexp(log_weights)
        return cls(ids, counts, hand_size, keys, log_weights)

    @classmethod
    def _from_rows(
        cls,
        card_def_ids: tuple[int, ...],
        unseen_counts: tuple[int, ...],
        hand_size: int,
        rows: dict[HandKey, float],
    ) -> ExactHandRange:
        finite = {key: value for key, value in rows.items() if math.isfinite(value)}
        if not finite:
            raise RangeError("range transition eliminated every hypothesis")
        ordered = sorted(finite)
        keys = np.asarray(ordered, dtype=np.int16)
        log_weights = np.asarray([finite[key] for key in ordered], dtype=np.float64)
        log_weights -= _logsumexp(log_weights)
        return cls(card_def_ids, unseen_counts, hand_size, keys, log_weights)

    @property
    def support_size(self) -> int:
        return int(self.keys.shape[0])

    @property
    def probabilities(self) -> NDArray[np.float64]:
        return np.exp(self.log_weights)

    @property
    def effective_range_size(self) -> float:
        probabilities = self.probabilities
        return 1.0 / float(np.square(probabilities).sum())

    @property
    def normalization_error(self) -> float:
        """Absolute probability-mass error, retained as an evidence canary."""

        return abs(float(self.probabilities.sum()) - 1.0)

    @property
    def digest(self) -> str:
        """Deterministic replay identity for the complete normalized range."""

        digest = hashlib.sha256()
        digest.update(np.asarray(self.card_def_ids, dtype="<i8").tobytes())
        digest.update(np.asarray(self.unseen_counts, dtype="<i8").tobytes())
        digest.update(int(self.hand_size).to_bytes(8, "little", signed=False))
        digest.update(np.asarray(self.keys, dtype="<i2").tobytes())
        digest.update(np.asarray(self.log_weights, dtype="<f8").tobytes())
        return digest.hexdigest()

    @property
    def allocated_bytes(self) -> int:
        return int(self.keys.nbytes + self.log_weights.nbytes)

    def probability(self, hand: HandKey) -> float:
        candidate = np.asarray(hand, dtype=np.int16)
        if candidate.shape != (len(self.card_def_ids),):
            return 0.0
        matches = np.flatnonzero(np.all(self.keys == candidate, axis=1))
        if matches.size == 0:
            return 0.0
        return float(math.exp(float(self.log_weights[int(matches[0])])))

    def log_loss(self, hand: HandKey) -> float:
        probability = self.probability(hand)
        return -math.log(probability) if probability > 0.0 else math.inf

    def rank(self, hand: HandKey) -> int | None:
        """One-based best rank of a hand under descending posterior mass."""

        probability = self.probability(hand)
        if probability <= 0.0:
            return None
        return 1 + int(np.count_nonzero(self.probabilities > probability))

    def inclusion_probabilities(self) -> NDArray[np.float64]:
        """Probability that each registered definition appears at least once."""

        return (self.probabilities[:, None] * (self.keys > 0)).sum(axis=0)

    def sample(self, count: int, seed: int) -> list[HandKey]:
        if count < 1:
            raise RangeError("sample count must be positive")
        rng = np.random.default_rng(seed)
        indexes = rng.choice(self.support_size, size=count, p=self.probabilities)
        return [tuple(int(value) for value in self.keys[index]) for index in indexes]

    def condition_action(
        self,
        likelihoods: Iterable[float],
        legal_action_counts: Iterable[int],
        *,
        epsilon: float,
    ) -> ExactHandRange:
        """Apply one public-action likelihood with a legal-action floor.

        A likelihood of zero with a positive legal-action count is a
        behavioral zero and receives epsilon mass.  A legal-action count of
        zero is a logical impossibility and remains exactly zero.
        """

        if not 0.0 <= epsilon < 1.0:
            raise RangeError("epsilon must be in [0, 1)")
        likelihood = np.asarray(tuple(likelihoods), dtype=np.float64)
        legal_counts = np.asarray(tuple(legal_action_counts), dtype=np.int64)
        if likelihood.shape != self.log_weights.shape:
            raise RangeError("likelihoods must match the current support")
        if legal_counts.shape != self.log_weights.shape:
            raise RangeError("legal_action_counts must match the current support")
        if np.any((likelihood < 0.0) | (likelihood > 1.0)):
            raise RangeError("action likelihoods must be probabilities")
        if np.any(legal_counts < 0):
            raise RangeError("legal action counts cannot be negative")
        mixed = np.zeros_like(likelihood)
        legal = legal_counts > 0
        mixed[legal] = (1.0 - epsilon) * likelihood[legal] + epsilon / legal_counts[
            legal
        ].astype(np.float64)
        kept = mixed > 0.0
        if not np.any(kept):
            raise RangeError("public action is illegal in every hypothesis")
        rows = {
            tuple(int(value) for value in key): float(weight + math.log(mass))
            for key, weight, mass in zip(
                self.keys[kept], self.log_weights[kept], mixed[kept]
            )
        }
        return self._from_rows(
            self.card_def_ids, self.unseen_counts, self.hand_size, rows
        )

    def draw_unknown(self) -> ExactHandRange:
        """Convolve one hidden draw from the opponent's remaining library."""

        library_size = sum(self.unseen_counts) - self.hand_size
        if library_size <= 0:
            raise RangeError("cannot draw from an empty hidden library")
        rows: dict[HandKey, float] = {}
        for key_array, log_weight in zip(self.keys, self.log_weights):
            key = tuple(int(value) for value in key_array)
            for index, unseen in enumerate(self.unseen_counts):
                available = unseen - key[index]
                if available <= 0:
                    continue
                drawn = list(key)
                drawn[index] += 1
                target = tuple(drawn)
                contribution = float(log_weight) + math.log(available / library_size)
                rows[target] = float(
                    np.logaddexp(rows.get(target, -math.inf), contribution)
                )
        return self._from_rows(
            self.card_def_ids, self.unseen_counts, self.hand_size + 1, rows
        )

    def remove_known(self, card_def_id: int) -> ExactHandRange:
        """Condition on a known definition leaving the opponent hand."""

        index = self._definition_index(card_def_id)
        if self.unseen_counts[index] <= 0 or self.hand_size <= 0:
            raise RangeError("known hand exit is impossible in the public snapshot")
        counts = list(self.unseen_counts)
        counts[index] -= 1
        rows: dict[HandKey, float] = {}
        for key_array, log_weight in zip(self.keys, self.log_weights):
            key = [int(value) for value in key_array]
            if key[index] == 0:
                continue
            key[index] -= 1
            target = tuple(key)
            contribution = float(log_weight)
            rows[target] = float(
                np.logaddexp(rows.get(target, -math.inf), contribution)
            )
        return self._from_rows(
            self.card_def_ids, tuple(counts), self.hand_size - 1, rows
        )

    def add_known(self, card_def_id: int) -> ExactHandRange:
        """Move one publicly identified definition into the opponent hand."""

        index = self._definition_index(card_def_id)
        counts = list(self.unseen_counts)
        counts[index] += 1
        rows: dict[HandKey, float] = {}
        for key_array, log_weight in zip(self.keys, self.log_weights):
            key = [int(value) for value in key_array]
            key[index] += 1
            rows[tuple(key)] = float(log_weight)
        return self._from_rows(
            self.card_def_ids, tuple(counts), self.hand_size + 1, rows
        )

    def as_definition_counts(self, hand: HandKey) -> list[tuple[int, int]]:
        if len(hand) != len(self.card_def_ids) or sum(hand) != self.hand_size:
            raise RangeError("hand key does not match this range")
        return [
            (card_def_id, int(count))
            for card_def_id, count in zip(self.card_def_ids, hand)
            if count
        ]

    def _definition_index(self, card_def_id: int) -> int:
        try:
            return self.card_def_ids.index(int(card_def_id))
        except ValueError as error:
            raise RangeError(f"unknown card definition: {card_def_id}") from error
