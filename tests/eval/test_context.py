"""An evaluation context saves a planned stop and resumes only its unfinished matches."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from etude.errors import EtudeError
from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Benchmark, Code, Match, Rollup, Seating
from etude.eval.run import Tables
from etude.eval.tournament import evaluating
from etude.records import RunOutcome
from etude.stopping import Stopped


def test_evaluation_stop_resume_and_refused_attempt_preserve_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
    tournament = Benchmark(
        name="arena",
        environment=environment("duel"),
        seatings=(
            Seating(
                name="pair",
                players=(Code(name="a"), Code(name="b")),
                config=({"deal": 0}, {"deal": 1}),
                games=1,
            ),
        ),
    )
    played: list[str] = []

    def play(match: Match) -> Tables:
        played.append(match.key)
        stop.touch()
        return {"scores": [{"seat": 0, "score": 1}]}

    with (
        pytest.raises(Stopped),
        evaluating(tournament, experiment="pilot", run="arena", directory=tmp_path) as active,
    ):
        active.play(play)
    outcome = tmp_path / "pilot/arena/outcome.json"
    assert RunOutcome.model_validate_json(outcome.read_bytes()).state == "stopped"
    assert len(played) == 1
    stop.unlink()

    def finish(match: Match) -> Tables:
        played.append(match.key)
        return {"scores": [{"seat": 0, "score": 1}]}

    with evaluating(
        tournament, experiment="pilot", run="arena", directory=tmp_path, resume=True
    ) as active:
        result = active.play(finish)
    assert result.played == 1
    assert len(set(played)) == 2
    finished = outcome.read_bytes()
    with (
        pytest.raises(EtudeError, match="run_name_taken"),
        evaluating(tournament, experiment="pilot", run="arena", directory=tmp_path),
    ):
        pytest.fail("a taken run was opened")
    changed = tournament.model_copy(update={"environment": environment("another")})
    with (
        pytest.raises(EtudeError, match="tournament_conflict"),
        evaluating(changed, experiment="pilot", run="arena", directory=tmp_path, resume=True),
    ):
        pytest.fail("an incompatible run was opened")
    assert outcome.read_bytes() == finished


def test_a_tournaments_rollups_are_stored_once_and_checked_on_resume(tmp_path: Path) -> None:

    tournament = Benchmark(
        name="totals",
        environment=environment("duel"),
        seatings=(
            Seating(name="pair", players=(Code(name="a"), Code(name="b")), config=({},), games=1),
        ),
        rollups=(Rollup(name="totals", table="scores", by=("player",), sums={"score": "score"}),),
    )

    def play(match: Match) -> Tables:
        return {"scores": [{"seat": 0, "score": 7}]}

    with evaluating(tournament, experiment="pilot", run="totals", directory=tmp_path) as run:
        assert run.play(play).played == 1
    with evaluating(
        tournament, experiment="pilot", run="totals", directory=tmp_path, resume=True
    ) as run:
        assert run.play(play).played == 0
    db = statsdb("pilot", tmp_path)
    assert db.sql("select score, rows from totals").fetchall() == [(7, 1)]
    changed = tournament.model_copy(update={"rollups": ()})
    with (
        pytest.raises(EtudeError, match="tournament_conflict"),
        evaluating(changed, experiment="pilot", run="totals", directory=tmp_path, resume=True),
    ):
        pytest.fail("a run was reopened with different rollups")


class Deal(BaseModel):
    deal: int


def play_typed(match: Match[Deal]) -> Tables:
    return {"scores": [{"seat": 0, "score": match.config.deal}]}


@pytest.mark.parametrize("workers", [1, 2])
def test_author_match_config_reaches_play_and_keeps_its_historical_key(
    tmp_path: Path, workers: int
) -> None:
    players = (Code(name="a"), Code(name="b"))
    typed = Match[Deal](players=players, config=Deal(deal=7), games=1)
    inferred = Match(players=players, config=Deal(deal=7), games=1)
    assert isinstance(inferred.config, Deal) and inferred.key == typed.key
    old = Match.model_validate({"players": players, "conditions": {"deal": 7}, "games": 1})
    assert typed.key == old.key and typed.seed == old.seed
    assert typed.model_dump_json() == old.model_dump_json()
    seating = Seating(name="pair", players=players, config=(Deal(deal=7), Deal(deal=9)), games=1)
    assert isinstance(seating.config[0], Deal)
    restored = Seating.model_validate_json(seating.model_dump_json())
    assert seating.definition == restored.definition
    assert restored.matches()[0].key == old.key
    with evaluating(
        Benchmark(name="typed", environment=environment("duel"), seatings=(seating,)),
        experiment="typed",
        directory=tmp_path,
        workers=workers,
    ) as run:
        assert run.play(play_typed).played == 2
    db = statsdb("typed", tmp_path)
    assert db.sql("select score from scores order by score").fetchall() == [(7,), (9,)]


def test_old_condition_keyword_warns_but_keeps_the_same_match() -> None:
    players = (Code(name="a"), Code(name="b"))
    with pytest.warns(DeprecationWarning, match="conditions is now config"):
        old = Match(players=players, conditions={"deal": 7})  # type: ignore[call-arg]  # released keyword alias
    current = Match(players=players, config=Deal(deal=7))
    assert old.key == current.key
    with pytest.warns(DeprecationWarning, match="Match.conditions is now Match.config"):
        assert old.conditions == {"deal": 7}
