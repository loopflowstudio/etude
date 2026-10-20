"""Read eval runs back and compare agents on any declared measure, across runs.

`episodes` reads every eval run under a directory or bucket; `compare` joins
them by agent identity, never by name. A vs B is A's aggregate over the
episodes in which B took another position; against `None` it is A's aggregate
over all of its episodes. Groups compare the same way, a group being the
multiset of its agents' identities. Episode measures have no subject and are
split by labels only.

A mean measure averages its values; a rate divides total hits by total
opportunities. The 95% interval is a percentile bootstrap that resamples
blocks, so correlated episodes (one deal played both ways) move together. Its
seed is fixed, so the same files always give the same report.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np

from etude.eval.records import Agent, Count, Episode, EvaluationFile, Measure
from etude.run.errors import EtudeError
from etude.run.listing import as_storage, declared, places
from etude.run.records import Digest, Name, Record
from etude.run.storage import Storage

REPLICATES = 2000
SEED = 0


class EpisodeView(Record):
    """One stored episode with the run and declaration it belongs to."""

    experiment: str
    run: str
    episode: Episode
    declaration: EvaluationFile

    def identities(self) -> tuple[Digest, ...]:
        """The identity of the agent at each position."""
        agents = {agent.name: agent.identity for agent in self.declaration.agents}
        return tuple(agents[player.agent] for player in self.episode.players)


class Comparison(Record):
    """One measure's aggregate for a subject against an opponent, within label values."""

    measure: Name
    # Agent identities: one for a player, a group's sorted, none for an episode measure.
    subject: tuple[Digest, ...]
    # The same for the opponent; None means against every opponent.
    opponent: tuple[Digest, ...] | None
    labels: dict[Name, str | None]
    value: float
    # None with fewer than two blocks.
    interval: tuple[float, float] | None
    episodes: int
    blocks: int


class Conflict(Record):
    """A measure name declared differently by different runs; never combined."""

    measure: Name
    declarations: tuple[Measure, ...]
    runs: tuple[str, ...]


def episodes(source: Storage | Path | str, experiment: str | None = None) -> list[EpisodeView]:
    """Every episode of every eval run under a source, latest write of each key."""
    storage = as_storage(source)
    views: list[EpisodeView] = []
    for run, folders in sorted(places(storage).items()):
        run_file = declared(storage, folders)
        if run_file is None or experiment not in (None, run_file.experiment):
            continue
        declaration = _declaration(storage, folders)
        if declaration is None:
            continue
        # One run may be copied to several places; a file name is the same in each.
        files: dict[str, str] = {}
        for place in folders:
            for key in storage.keys(place + "episodes/"):
                files.setdefault(key[len(place) :], place)
        latest: dict[str, Episode] = {}
        for name in sorted(files):
            for line in storage.get(files[name] + name).decode().splitlines():
                episode = Episode.model_validate_json(line)
                latest[episode.key] = episode
        views += [
            EpisodeView(
                experiment=run_file.experiment, run=run, episode=episode, declaration=declaration
            )
            for episode in latest.values()
        ]
    return views


def _declaration(storage: Storage, folders: Sequence[str]) -> EvaluationFile | None:
    for place in folders:
        try:
            return EvaluationFile.model_validate_json(storage.get(place + "evaluation.json"))
        except FileNotFoundError:
            continue
    return None


def conflicts(views: Sequence[EpisodeView]) -> list[Conflict]:
    """Measures whose declarations differ between the runs these episodes came from."""
    by_name: dict[str, dict[Measure, list[str]]] = {}
    for view in {view.run: view for view in views}.values():
        for measure in view.declaration.measures:
            by_name.setdefault(measure.name, {}).setdefault(measure, []).append(view.run)
    return [
        Conflict(
            measure=name,
            declarations=tuple(versions),
            runs=tuple(sorted(run for runs in versions.values() for run in runs)),
        )
        for name, versions in sorted(by_name.items())
        if len(versions) > 1
    ]


def measures(views: Sequence[EpisodeView]) -> list[Measure]:
    """Every measure these episodes' runs declared alike, by name."""
    clashing = {conflict.measure for conflict in conflicts(views)}
    found = {m.name: m for view in views for m in view.declaration.measures}
    return [found[name] for name in sorted(found) if name not in clashing]


def agent_names(views: Sequence[EpisodeView]) -> dict[Digest, str]:
    """What to call each identity: every name the runs gave it, joined by `/`."""
    names: dict[Digest, dict[str, None]] = {}
    for view in views:
        for agent in view.declaration.agents:
            names.setdefault(agent.identity, {})[agent.name] = None
    return {identity: "/".join(given) for identity, given in names.items()}


def agents(views: Sequence[EpisodeView]) -> list[Agent]:
    """One declaration per identity, the first seen."""
    found: dict[Digest, Agent] = {}
    for view in views:
        for agent in view.declaration.agents:
            found.setdefault(agent.identity, agent)
    return list(found.values())


type _Key = tuple[tuple[Digest, ...], tuple[Digest, ...] | None, tuple[str | None, ...]]


def compare(
    views: Sequence[EpisodeView], measure: str, *, by: Sequence[str] = ()
) -> list[Comparison]:
    """Every subject against every opponent, and against all, on one measure.

    `by` names labels to split on: an episode's labels, then the measured
    player's or group's, which win. Raises when runs declared the measure
    differently, or none declared it.
    """
    versions = {m for view in views for m in view.declaration.measures if m.name == measure}
    if len(versions) > 1:
        raise EtudeError("measure_declaration_conflict", 409)
    if not versions:
        raise EtudeError("undeclared_measure", 404)
    # Per key, per block: [numerator, denominator], and the episodes seen.
    sums: dict[_Key, dict[tuple[str, str, str], list[float]]] = {}
    seen: dict[_Key, set[tuple[str, str]]] = {}
    # `add` refused undeclared measures, so a run without this one yields no values.
    for view in views:
        episode = view.episode
        block = (view.run, *(("block", episode.block) if episode.block else ("key", episode.key)))
        for subject, opponents, labels, value in _measured(view, measure):
            split = tuple((episode.labels | labels).get(name) for name in by)
            # A mean is a ratio too: its denominator counts values.
            numerator, denominator = (
                (value.hits, value.opportunities) if isinstance(value, Count) else (value, 1)
            )
            for opponent in (None, *sorted(opponents)):
                key = (subject, opponent, split)
                total = sums.setdefault(key, {}).setdefault(block, [0.0, 0.0])
                total[0] += numerator
                total[1] += denominator
                seen.setdefault(key, set()).add((view.run, episode.key))
    found: list[Comparison] = []
    for key in sorted(sums, key=lambda k: (k[0], k[1] or (), [v or "" for v in k[2]])):
        totals = np.array(list(sums[key].values()))
        denominator = totals[:, 1].sum()
        if denominator == 0:
            continue  # a rate with no opportunities has no value
        found.append(
            Comparison(
                measure=measure,
                subject=key[0],
                opponent=key[1],
                labels=dict(zip(by, key[2])),
                value=float(totals[:, 0].sum() / denominator),
                interval=_interval(totals[:, 0], totals[:, 1]),
                episodes=len(seen[key]),
                blocks=len(totals),
            )
        )
    return found


def _measured(
    view: EpisodeView, measure: str
) -> Iterator[tuple[tuple[Digest, ...], set[tuple[Digest, ...]], dict[str, str], float | Count]]:
    """Each value of the measure in one episode: subject, opponents, labels, value."""
    episode = view.episode
    identities = view.identities()
    for position, player in enumerate(episode.players):
        if (value := player.measures.get(measure)) is not None:
            opponents: set[tuple[Digest, ...]] = {
                (other,) for o, other in enumerate(identities) if o != position
            }
            yield (identities[position],), opponents, player.labels, value
    teams = [tuple(sorted(identities[p] for p in group.players)) for group in episode.groups]
    for index, group in enumerate(episode.groups):
        if (value := group.measures.get(measure)) is not None:
            others = {team for other, team in enumerate(teams) if other != index}
            yield teams[index], others, group.labels, value
    if (value := episode.measures.get(measure)) is not None:
        yield (), set(), {}, value


def _interval(
    numerators: np.ndarray[tuple[int], np.dtype[np.float64]],
    denominators: np.ndarray[tuple[int], np.dtype[np.float64]],
) -> tuple[float, float] | None:
    """A 95% percentile bootstrap of the ratio of totals, resampling blocks."""
    n = len(numerators)
    if n < 2:
        return None
    weights = np.random.default_rng(SEED).multinomial(n, np.full(n, 1 / n), size=REPLICATES)
    totals = weights @ denominators
    kept = totals > 0
    ratios = (weights @ numerators)[kept] / totals[kept]
    low, high = np.percentile(ratios, [2.5, 97.5])
    return float(low), float(high)
