"""Watched eval reports follow retained matches without launching any compute."""

import time
import webbrowser
from pathlib import Path

import pytest

from etude.cli import main
from etude.eval.match import agent, environment, evaluate
from etude.eval.records import Match
from tests.eval.games import play


def _record(directory: Path, deal: int) -> None:
    script = directory / "player.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def act(): return 0\n")
    evaluate(
        experiment="live-report",
        environment=environment("live-duel"),
        play=play,
        matches=[
            Match(
                agents=(
                    agent("strong", code=[script], config={"strength": 3}),
                    agent("weak", code=[script], config={"strength": 1}),
                ),
                conditions={"deal": deal},
                games=2,
            )
        ],
        run=f"live-{deal}",
        directory=directory,
    )


def test_watch_waits_for_results_and_refreshes_open_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "runs"
    target = tmp_path / "reports" / "eval" / "live-report.html"
    opened: list[str] = []
    pages: list[str] = []
    polls = 0

    def sleep(seconds: float) -> None:
        nonlocal polls
        assert seconds == 0.1
        polls += 1
        if target.exists():
            pages.append(target.read_text())
        if polls < 3:
            _record(source, polls)
        else:
            raise KeyboardInterrupt

    def open_page(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(webbrowser, "open", open_page)
    assert (
        main(
            [
                "reports",
                "eval",
                "--source",
                str(source),
                "--out",
                str(tmp_path / "reports"),
                "--watch",
                "0.1",
                "--open",
            ]
        )
        == 0
    )
    assert polls == 3
    assert opened == [target.as_uri()]
    assert "1 matches" in pages[0]
    assert "2 matches" in pages[1]
    assert '<meta http-equiv="refresh" content="0.1">' in pages[1]
    assert capsys.readouterr().out.strip() == str(target)


def test_single_report_retains_no_results_error(tmp_path: Path) -> None:
    assert main(["reports", "eval", "--source", str(tmp_path)]) == 1


def test_single_report_does_not_reload(tmp_path: Path) -> None:
    _record(tmp_path, 1)
    assert main(["reports", "eval", "--source", str(tmp_path)]) == 0
    assert "http-equiv" not in (tmp_path / "reports" / "eval" / "live-report.html").read_text()
