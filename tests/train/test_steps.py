"""A loop declares its clock and saves on steps, seconds, completion and stop."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from etude.errors import EtudeError
from etude.records import RunRecord
from etude.stopping import Stopped
from etude.train.read import read
from etude.train.records import Time
from etude.train.state import read_state
from etude.train.tracking import EtudeWarning, track


class Counts(BaseModel):
    updates: int = 0


def test_steps_declare_the_clock_and_resume_its_saved_position(tmp_path: Path) -> None:
    for resume, target in ((False, 2), (True, 4)):
        with track(experiment="e", run="r", directory=tmp_path, resume=resume) as run:
            counts = run.resume(extra=Counts())
            for update in run.steps("updates", to=target, save_every=1):
                counts.updates = update
                run.log({"score": update})
                run.save()
    data = read(tmp_path, "e", "r")
    assert data.points("score", "updates").x.tolist() == [1, 2, 3, 4]
    assert data.points("score", "updates").y.tolist() == [1, 2, 3, 4]
    assert [(clock.name, clock.meaning) for clock in data.clocks] == [("updates", "updates")]
    assert counts.updates == 4


@pytest.mark.parametrize("table", [False, True])
def test_a_row_without_a_clock_explains_how_to_declare_one(tmp_path: Path, table: bool) -> None:
    with track(experiment="e", run="r", directory=tmp_path) as run:
        with pytest.raises(EtudeError, match="declare.*clock.*run.steps"):
            if table:
                run.log_table("games", [{"score": 1}])
            else:
                run.log({"score": 1})
        for _ in run.steps("updates", to=1):
            run.log({"score": 2})
    assert read(tmp_path, "e", "r").points("score", "updates").y.tolist() == [2]


@pytest.mark.parametrize(
    ("save_every", "save_every_seconds", "seconds", "expected"),
    [
        (2, None, 1, [None, 2, 2, 4, 5]),
        (None, 10, 6, [None, 2, 2, 4, 5]),
        (None, None, 100, [None, None, 3, 3, 5]),
        (4, 10, 6, [None, 2, 2, 4, 5]),
    ],
)
def test_save_cadence_and_final_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save_every: int | None,
    save_every_seconds: float | None,
    seconds: int,
    expected: list[int | None],
) -> None:
    now = [0.0]
    monkeypatch.setattr("etude.train.tracking.time.monotonic", lambda: now[0])
    with track(experiment="e", run="r", directory=tmp_path) as run:
        run.resume(extra=Counts())
        saved: list[int | None] = []
        for update in run.steps(
            "updates", to=5, save_every=save_every, save_every_seconds=save_every_seconds
        ):
            now[0] = update * seconds
            run.save()
            state = read_state(run.out / "state")
            saved.append(None if state is None else int(state.record.at["updates"]))
        assert saved == expected


def test_implicit_clock_is_saved_before_a_planned_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
    with pytest.raises(Stopped), track(experiment="e", run="r", directory=tmp_path) as run:
        counts = run.resume(extra=Counts())
        for update in run.steps("updates", to=3):
            counts.updates = update
            run.log({"score": update})
            stop.touch()
            assert run.should_stop
            run.save()
    state = read_state(tmp_path / "e/r/out/state")
    assert state is not None and state.record.extra == {"updates": 1}
    outcome = read(tmp_path, "e", "r").outcome
    assert outcome is not None and outcome.state == "stopped"


def test_a_second_implicit_clock_preserves_rows_already_queued(tmp_path: Path) -> None:
    with track(experiment="e", run="r", directory=tmp_path) as run:
        run.log({"score": 0}, seconds=0)
        for update in run.steps("updates", to=2):
            run.log({"score": update}, seconds=update)
    data = read(tmp_path, "e", "r")
    assert data.rows == 3
    assert data.points("score", "updates").y.tolist() == [1, 2]
    assert data.points("score", "seconds").y.tolist() == [0, 1, 2]
    stored = RunRecord.model_validate_json((tmp_path / "e/r/run.json").read_bytes())
    assert [clock.name for clock in stored.clocks] == ["seconds", "updates"]


def test_log_keywords_declare_count_clocks_and_require_them_on_later_rows(tmp_path: Path) -> None:
    with track(experiment="e", run="r", directory=tmp_path, strict=True) as run:
        run.log({"loss": 0.5}, updates=1, learner_transitions=4)
        with pytest.raises(EtudeError, match="row_missing_clock"):
            run.log({"loss": 0.4}, updates=2)
        run.log({"loss": 0.3}, updates=2, learner_transitions=8)
    data = read(tmp_path, "e", "r")
    assert [(clock.name, clock.kind, clock.meaning) for clock in data.clocks] == [
        ("updates", "step", "updates"),
        ("learner_transitions", "step", "learner_transitions"),
    ]
    assert data.points("loss", "learner_transitions").y.tolist() == [0.5, 0.3]


def test_missing_used_clock_warns_outside_strict_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with track(experiment="e", run="r", directory=tmp_path) as run:
        run.log({"loss": 1}, updates=1, transitions=4)
        with pytest.warns(EtudeWarning, match="no value for clock 'transitions'"):
            run.log({"loss": 2}, updates=2)
    data = read(tmp_path, "e", "r")
    assert data.rows == 2
    assert data.points("loss", "transitions").y.tolist() == [1]


def test_table_keywords_declare_clocks_and_time_keeps_its_kind(tmp_path: Path) -> None:
    with track(
        experiment="e", run="r", directory=tmp_path, clocks=[Time("simulation_seconds"), "updates"]
    ) as run:
        run.log_table("games", [{"score": 1}], updates=1, simulation_seconds=10)
    data = read(tmp_path, "e", "r")
    assert [(clock.name, clock.kind, clock.meaning) for clock in data.clocks] == [
        ("simulation_seconds", "time", "simulation_seconds"),
        ("updates", "step", "updates"),
    ]
    assert data.rows == 1


@pytest.mark.parametrize("strict", [False, True])
def test_explicit_clock_names_close_the_set(tmp_path: Path, strict: bool) -> None:
    with track(
        experiment="e", run="r", directory=tmp_path, clocks=["updates"], strict=strict
    ) as run:
        run.log({"loss": 1}, updates=1)
        if strict:
            with pytest.raises(EtudeError, match="clock_undeclared.*updatess"):
                run.log({"loss": 2}, updatess=2)
        else:
            with pytest.warns(EtudeWarning, match="undeclared clocks: updatess"):
                run.log({"loss": 2}, updatess=2)
        with pytest.raises(EtudeError, match="clock_undeclared.*updatess"):
            list(run.steps("updatess", to=1))
        run.log({"loss": 3}, updates=3)
    data = read(tmp_path, "e", "r")
    assert [clock.name for clock in data.clocks] == ["updates"]
    assert data.points("loss", "updates").y.tolist() == [1, 3]


def test_metrics_are_sparse_while_rows_keep_their_clock(tmp_path: Path) -> None:
    with track(experiment="e", run="r", directory=tmp_path, clocks=["updates"], strict=True) as run:
        run.log({"loss": 0.5}, updates=1)
        run.log({"accuracy": 0.9}, updates=2)
        run.log({"loss": 0.25, "accuracy": 0.95}, updates=3)
    data = read(tmp_path, "e", "r")
    rows = [columns.row(i) for columns in data.columns for i in range(columns.rows)]
    assert [row.clocks["updates"] for row in rows] == [1, 2, 3]
    assert [row.values.get("loss") for row in rows] == [0.5, None, 0.25]
    assert [row.values.get("accuracy") for row in rows] == [None, 0.9, 0.95]
