"""The order an eval run plays its matches in, so consecutive matches reuse policies.

A policy is a seated checkpoint, by its `CheckpointKey` (`policies`); code
agents need none. An author's inference runtime, such as Manabot's
`PolicyStore`, keeps the policies used most recently loaded and evicts the
least recently used. `ordered` lists matches in the order `evaluate` plays
them: next, the match that loads the fewest policies not yet used, then the one
reusing the most, then the one whose policies were used most recently. That
ranking by recency (stack distance) suits a runtime of any size, so Etude
models no byte allowance and counts no loads: the runtime owns those.

Ties keep the caller's order, so a queue of code agents plays as given. The
order changes no result: a match's games come from its key and seed.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence

from etude.eval.records import CheckpointKey, Match


def policies(match: Match) -> frozenset[CheckpointKey]:
    """The checkpoints a match seats; one checkpoint at two configs is one policy."""
    return frozenset(
        seated.key.checkpoint for seated in match.agents if seated.key.checkpoint is not None
    )


def ordered(matches: Sequence[Match]) -> list[Match]:
    """The matches in the order `evaluate` plays them, one worker at a time.

    A caller cutting its own batches should cut contiguous runs of this order.
    """
    return list(Queue(matches))


class Queue(Iterator[Match]):
    """The matches `ordered` lists, taken one at a time as workers free up."""

    def __init__(self, matches: Sequence[Match]) -> None:
        # Matches seating the same policies, each group in the caller's order.
        self._groups: dict[frozenset[CheckpointKey], deque[tuple[int, Match]]] = {}
        for index, match in enumerate(matches):
            self._groups.setdefault(policies(match), deque()).append((index, match))
        self._used: list[CheckpointKey] = []

    def __next__(self) -> Match:
        """The next match to play, counted as using its policies now."""
        if not self._groups:
            raise StopIteration
        # How many policies were used since each one: its stack distance.
        since = {policy: len(self._used) - 1 - at for at, policy in enumerate(self._used)}

        def cost(
            item: tuple[frozenset[CheckpointKey], deque[tuple[int, Match]]],
        ) -> tuple[int, ...]:
            group, waiting = item
            warm = [since[policy] for policy in group if policy in since]
            return (len(group) - len(warm), -len(warm), max(warm, default=0), waiting[0][0])

        group, waiting = min(self._groups.items(), key=cost)
        _, match = waiting.popleft()
        if not waiting:
            del self._groups[group]
        seated = [each.key.checkpoint for each in match.agents if each.key.checkpoint is not None]
        for policy in dict.fromkeys(seated):
            if policy in since:
                self._used.remove(policy)
            self._used.append(policy)
        return match
