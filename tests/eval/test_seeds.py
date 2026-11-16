"""Indexed panels retain completed matches and give every player identical draws."""

from pathlib import Path

from pydantic import BaseModel

from etude.eval.match import environment
from etude.eval.records import Benchmark, Code, Match, RoundRobin, Seating, TournamentRun, assemble
from etude.eval.tournament import evaluating
from tests.eval.games import play


class Rules(BaseModel, frozen=True):
    cards: int = 5


def test_seeds_are_shared_by_seatings_and_seat_orders() -> None:
    a, b, c = Code(name="a"), Code(name="b"), Code(name="c")
    one = Seating(name="ab", players=(a, b), config=Rules(), base_seed=19, games=4).matches()
    swapped = Seating(name="ba", players=(b, a), config=Rules(), base_seed=19, games=4).matches()
    other = Seating(name="ac", players=(a, c), config={"cards": 5}, base_seed=19, games=4).matches()
    assert [m.index for m in one] == [0, 1, 2, 3]
    assert len({m.seed for m in one}) == 4
    assert [m.seed for m in one] == [m.seed for m in swapped] == [m.seed for m in other]
    assert {m.key for m in one}.isdisjoint(m.key for m in swapped)
    assert all(m.games == 1 for m in one)
    assert all(Match.model_validate_json(m.model_dump_json()).key == m.key for m in one)


def test_extending_the_panel_preserves_completed_matches() -> None:
    players = (Code(name="a"), Code(name="b"))
    short: Seating = Seating(name="pair", players=players, config={}, games=2)
    longer = short.model_copy(update={"games": 4})
    assert [m.key for m in short.matches()] == [m.key for m in longer.matches()[:2]]
    changed = short.model_copy(update={"base_seed": 1})
    assert {m.key for m in changed.matches()}.isdisjoint(m.key for m in short.matches())
    assert [m.seed for m in changed.matches()] != [m.seed for m in short.matches()]


def test_round_robin_uses_the_same_seed_panel_for_each_pair() -> None:
    players = [Code(name=name) for name in ("a", "b", "c")]
    rr: RoundRobin = RoundRobin(name="league", environment=environment("cards"), config={}, games=3)
    matches = rr.matches(players)
    assert len(matches) == 9
    assert (
        [m.seed for m in matches[:3]]
        == [m.seed for m in matches[3:6]]
        == [m.seed for m in matches[6:]]
    )
    assert len({m.key for m in matches}) == 9


def test_released_multigame_match_keeps_its_key_and_seed() -> None:
    stored = '{"players":[{"kind":"code","name":"a"}],"conditions":{},"games":8}'
    match = Match.model_validate_json(stored)
    assert match.index is None
    assert match.games == 8
    assert match.seed == int(match.key[:15], 16)
    assert Match.model_validate_json(match.model_dump_json()).key == match.key


def test_extended_round_robin_only_plays_new_indices(tmp_path: Path) -> None:
    players = (Code(name="a", config={"strength": 1}), Code(name="b", config={"strength": 2}))
    rr: RoundRobin = RoundRobin(name="league", environment=environment("cards"), config={}, games=2)
    with evaluating(rr, experiment="panel", enter=players, directory=tmp_path) as run:
        run.play(play)
    extended = rr.model_copy(update={"games": 4})
    with evaluating(extended, experiment="panel", directory=tmp_path) as run:
        assert len(run.matches) == 2
        run.play(play)
    with evaluating(extended, experiment="panel", directory=tmp_path, resume=True) as run:
        assert not run.matches


def test_benchmark_assembly_preserves_the_largest_indexed_panel() -> None:
    players = (Code(name="a"), Code(name="b"))
    seating: Seating = Seating(name="pair", players=players, config={}, games=2)
    short = Benchmark(name="panel", environment=environment("cards"), seatings=(seating,))
    longer = short.model_copy(update={"seatings": (seating.model_copy(update={"games": 4}),)})
    combined = assemble([TournamentRun(tournament=longer), TournamentRun(tournament=short)])
    assert len(combined.schedule()) == 4
    assert [m.match.key for m in combined.schedule()[:2]] == [m.key for m in seating.matches()]
