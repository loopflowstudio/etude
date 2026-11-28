"""Advance a small panel of variable-length games in two overlapping halves."""

from collections.abc import AsyncIterator, Awaitable
from typing import cast

import numpy as np

from etude.eval.agents import Forward, ForwardError
from etude.eval.game import MatchStream
from etude.eval.records import Match
from etude.eval.result import Tables

type _Slot = tuple[Match, Forward, int]


def _submit_half(half: list[_Slot]) -> Awaitable[list[object]] | None:
    if not half:
        return None
    # This panel seats the same checkpoint in every match; submit eagerly.
    forward = half[0][1]
    batch = np.array([left for _, _, left in half], dtype=np.int64)
    return forward([batch], rows=len(half))


async def play_many(matches: MatchStream) -> AsyncIterator[tuple[Match, Tables | Exception]]:
    halves: list[list[_Slot]] = [[], []]
    half_capacity = 2
    while True:
        for half in halves:
            if len(half) < half_capacity:
                for match, agents in await matches.take(half_capacity - len(half)):
                    half.append((match, cast(Forward, agents[0]), 1 + match.seed % 3))
        if not halves[0] and not halves[1]:
            return
        # Submit half A eagerly so inference runs while half B prepares and submits.
        pending = [_submit_half(halves[0]), _submit_half(halves[1])]
        for index, awaitable in enumerate(pending):
            if awaitable is None:
                continue
            ready = halves[index]
            halves[index] = []
            try:
                [output] = await awaitable
            except ForwardError as error:
                for match, _, _ in ready:
                    yield match, error
                continue
            scores = cast(np.ndarray, output)
            for (match, agent, left), score in zip(ready, scores.tolist(), strict=True):
                if left > 1:
                    halves[index].append((match, agent, left - 1))
                else:
                    yield (
                        match,
                        {"scores": [{"seat": 0, "score": score}, {"seat": 1, "score": -score}]},
                    )
