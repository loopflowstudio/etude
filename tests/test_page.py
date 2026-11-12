"""`etude report FILE` follows the file's `report()` for the experiment named by the file."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

import pytest

from etude.cli import main
from etude.page import Index, Page, Pages, Server, build, experiment_files, index, render, target
from etude.report import (
    Anchors,
    Curve,
    Matches,
    Matrix,
    Ranking,
    Report,
    Schedule,
    SchedulePoint,
)
from etude.run.errors import EtudeError
from etude.run.storage import DirectoryStorage, Storage, storage_from_uri
from etude.train.records import Step
from etude.train.run import track
from tests.experiments import experiment_file
from tests.run.storage import MeasuredStorage

LAB = """
from etude.report import Matrix, Report, Score


def train(config: object) -> None: ...


def report() -> Report:
    return Report(
        score=Score(table="plays", value="won", reference=0.5),
        evals=(Matrix(rows="opponent_deck", columns="deck"),),
        baseline="uniform",
    )
"""


@pytest.fixture(autouse=True)
def _restore_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    # Running an experiment file puts its folder on the import path.
    monkeypatch.setattr(sys, "path", list(sys.path))


def trained(runs: Path, experiment: str, run: str) -> None:
    with track(experiment=experiment, run=run, clocks=[Step("updates", "updates")], directory=runs):
        pass


def test_the_report_is_the_files_and_the_experiment_is_named_for_it(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    trained(runs, "other", "b")
    page = build(runs, experiment_file(tmp_path / "experiments", "lab", LAB))
    assert (page.experiment, page.origin, page.notes) == ("lab", "lab.py:report", ())
    assert page.report.evals == (Matrix(rows="opponent_deck", columns="deck"),)
    assert [run.name for run in page.training.runs] == ["a"]


def test_checkpoints_with_no_etude_clock_are_named_with_the_reason(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run = track(experiment="lab", run="a", clocks=[Step("updates", "updates")], directory=runs)
    run.checkpoint("s3://models/10.pt", updates=10)
    run.finish(timeout=30)
    file = experiment_file(tmp_path / "experiments", "lab", LAB)
    assert build(runs, file).notes == ()
    # As an older client saved it, copied without its attempt records.
    for path in (runs / "a/checkpoints").glob("*.json"):
        record = json.loads(path.read_text())
        record["at"] = {"updates": record["at"]["updates"]}
        path.write_text(json.dumps(record))
    for path in (runs / "a/metrics").glob("*.json"):
        path.unlink()
    (note,) = build(runs, file).notes
    assert "No running_hours or hours_since_start for some checkpoints of a" in note
    assert "attempt records (metrics/WRITER.json) of a are not in this source" in note


def test_without_a_report_the_page_is_the_default_and_says_so(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run = track(experiment="lab", run="a", clocks=[Step("updates", "updates")], directory=runs)
    run.log({"custom/score": 1.0}, updates=1)
    run.finish(timeout=30)
    page = build(runs, experiment_file(tmp_path, "lab", "def train(config: object) -> None: ...\n"))
    assert page.report == Report(other_metrics=True) and page.origin is None
    assert page.notes == ("lab.py defines no report(), so this is the default report.",)
    assert page.report.evals == (Curve(), Anchors())
    assert "custom/score" in page.training.metrics  # nothing declared, so everything is drawn

    declared = "from etude.report import Report\n\ndef report() -> Report:\n    return Report()\n"
    page = build(runs, experiment_file(tmp_path / "declared", "lab", declared))
    assert "custom/score" not in page.training.metrics


def test_a_file_that_is_not_an_experiments_is_refused(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    with pytest.raises(EtudeError, match="experiment_report_returns_a_report"):
        build(runs, experiment_file(tmp_path, "lab", "def report():\n    return {}\n"))
    with pytest.raises(EtudeError, match="experiment_file_not_found"):
        build(runs, tmp_path / "missing.py")


def test_an_absent_experiment_names_the_ones_the_source_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trained(tmp_path / "runs", "lab", "a")
    elsewhere = experiment_file(tmp_path, "elsewhere")
    assert main(["report", str(elsewhere), "--source", str(tmp_path / "runs")]) == 1
    assert "it holds lab" in capsys.readouterr().err


def test_a_bucket_report_is_written_here_and_a_directorys_beside_its_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("boto3")
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    # Making a client reaches no network: only reading the bucket would.
    assert target(storage_from_uri("r2://bucket/runs"), "lab") == Path("reports/lab.html")
    assert target(storage_from_uri("s3://bucket/runs"), "lab") == Path("reports/lab.html")
    assert target(DirectoryStorage(tmp_path), "lab") == tmp_path / "reports/lab.html"
    assert target(DirectoryStorage(tmp_path), "lab", tmp_path / "out") == tmp_path / "out/lab.html"


def test_a_report_names_the_eval_columns_its_panels_split_by() -> None:
    report = Report(
        evals=(
            Curve(split="seat", matches=Matches(tag="anchor")),
            Matrix(rows="opponent_deck", columns="deck"),
            Ranking(by="seat"),
        )
    )
    assert report.columns() == ("seat", "opponent_deck", "deck")
    assert "rl/entropy" in Report().keys()


REPORTED = "from etude.report import Report\n\n\ndef report() -> Report:\n    return Report()\n"


def test_a_folder_holds_the_files_that_define_eval_or_report_and_none_is_run(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "experiments"
    experiment_file(folder, "lab", REPORTED)
    experiment_file(folder / "more", "ladder", "def eval(state):\n    return []\n")
    # A helper module and a job file are no experiments, and are never run to find out.
    experiment_file(folder, "games", "raise SystemExit('run')\n\ndef play(match): ...\n")
    experiment_file(folder, "job", "raise SystemExit('run')\n\ndef train(config): ...\n")
    experiment_file(folder / ".venv", "vendored", REPORTED)
    assert [f.relative_to(folder) for f in experiment_files(folder)] == [
        Path("lab.py"),
        Path("more/ladder.py"),
    ]
    experiment_file(folder / "copy", "lab", REPORTED)
    with pytest.raises(EtudeError, match="experiment_named_twice"):
        experiment_files(folder)


def test_a_folders_report_is_an_index_of_its_experiments_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs, folder = tmp_path / "runs", tmp_path / "experiments"
    trained(runs, "lab", "a")
    experiment_file(folder, "lab", REPORTED)
    # One file fails as it runs, and one experiment has no runs yet: each says so on its row.
    experiment_file(folder, "broken", "def report(): ...\n\nraise RuntimeError('typo')\n")
    experiment_file(folder, "fresh", REPORTED)
    built = Pages(runs).build_each(folder)
    found = index(runs, built, href=lambda stem: f"{stem}.html")
    rows = {entry.experiment: entry for entry in found.entries}
    assert rows["broken"].page is None
    assert rows["broken"].error is not None and "RuntimeError: typo" in rows["broken"].error
    assert rows["fresh"].error is not None and "no runs of 'fresh'" in rows["fresh"].error
    lab, whole = rows["lab"].page, built[folder / "lab.py"]
    assert lab is not None and isinstance(whole, Page)
    # The index keeps the page but its line data, which it never draws.
    assert lab.training.runs == whole.training.runs and lab.training.series == {}
    assert rows["lab"].href == "lab.html"
    # Written, it is an index beside a page for each experiment that has one.
    assert main(["report", str(folder), "--source", str(runs)]) == 0
    assert capsys.readouterr().out.strip() == str(runs / "reports/index.html")
    assert sorted(p.name for p in (runs / "reports").iterdir()) == ["index.html", "lab.html"]
    # Served, it is the index at / and each experiment's page at /STEM.
    server = Server(runs, folder, ("127.0.0.1", 0))
    try:
        assert '"href": "/lab"' in (server.page("/") or "")
        assert "lab.py:report" in (server.page("/lab") or "")
        assert server.page("/elsewhere") is None
    finally:
        server.server_close()


COUNTED = """
from pathlib import Path

from etude.report import Report

with Path(__file__).with_suffix(".built").open("a") as built:
    built.write("built\\n")


def report() -> Report:
    return Report()
"""


def test_a_served_page_refreshes_on_demand_only_when_its_files_change(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    trained(runs, "other", "c")
    file = experiment_file(tmp_path / "experiments", "lab", COUNTED)
    builds = file.with_suffix(".built")  # the file runs once for every page built
    measured = MeasuredStorage(DirectoryStorage(runs))
    server = Server(measured, file, ("127.0.0.1", 0))
    try:
        assert '"name": "a"' in (server.page("/") or "")
        reads = measured.requests, measured.listings
        server.page("/")
        assert (measured.requests, measured.listings) == reads
        assert builds.read_text().count("built") == 1
        # Another experiment's run is not this page's input; a run of its own is.
        trained(runs, "other", "d")
        server.page("/", refresh=True)
        assert builds.read_text().count("built") == 1
        trained(runs, "lab", "b")
        assert '"name": "b"' not in (server.page("/") or "")
        assert '"name": "b"' in (server.page("/", refresh=True) or "")
        assert builds.read_text().count("built") == 2
        file.write_text(COUNTED + "# edited\n")
        server.page("/", refresh=True)
        assert builds.read_text().count("built") == 3
    finally:
        server.server_close()


def test_a_page_carries_its_charts_and_needs_no_network(tmp_path: Path) -> None:
    trained(tmp_path / "runs", "lab", "a")
    html = render(build(tmp_path / "runs", experiment_file(tmp_path, "lab", REPORTED)))
    assert "uPlot" in html and "<script src" not in html and "<link" not in html


def test_a_panels_matches_are_a_seating_or_a_tag_of_a_tournament_never_both() -> None:
    assert Curve(matches=Matches(tournament="ladder", tag="anchor")).matches is not None
    with pytest.raises(ValueError, match="matches_name_a_seating_or_a_tag_not_both"):
        Matches(seating="vs heuristic", tag="anchor")


def test_an_absent_experiments_error_names_the_source_as_it_was_given(tmp_path: Path) -> None:
    trained(tmp_path / "runs", "lab", "a")
    given = re.escape(f"no runs of 'fresh' in {tmp_path / 'runs'};")
    with pytest.raises(ValueError, match=given):
        build(tmp_path / "runs", experiment_file(tmp_path, "fresh"))


def test_refresh_failure_preserves_the_last_page_and_recovers(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    file = experiment_file(tmp_path, "lab", REPORTED)
    with Server(runs, file, ("127.0.0.1", 0)) as server:
        original = server.page()
        assert original is not None
        assert '"refresh": null' in original and '"served": true' in original
        file.write_text("raise RuntimeError('broken report')\n")
        with pytest.raises(RuntimeError, match="broken report"):
            server.page(refresh=True)
        assert server.page() == original  # includes its original generation timestamp
        file.write_text(REPORTED.replace("Report()", 'Report(labels={"a": "Readable"})'))
        refreshed = server.page(refresh=True)
        assert refreshed != original and '"Readable"' in (refreshed or "")
        assert server.page() == refreshed


def test_refresh_reads_an_edited_neighbour_the_file_imports(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    folder = tmp_path / "experiments"
    helper = experiment_file(folder, "lab_names", 'NAME = "First"\n')
    file = experiment_file(
        folder,
        "lab",
        "from etude.report import Report\nfrom lab_names import NAME\n\n\n"
        "def report() -> Report:\n    return Report(labels={'a': NAME})\n",
    )
    with Server(runs, file, ("127.0.0.1", 0)) as server:
        os.utime(helper, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        assert '"First"' in (server.page() or "")
        # Same size and second: Python's bytecode cache must not hide this edit.
        helper.write_text('NAME = "Other"\n')
        os.utime(helper, ns=(1_700_000_000_000_000_001, 1_700_000_000_000_000_001))
        assert '"Other"' in (server.page(refresh=True) or "")
        helper.write_text("NAME = (\n")
        with pytest.raises(SyntaxError):
            server.page(refresh=True)
        helper.write_text('NAME = "Repaired name"\n')
        assert '"Repaired name"' in (server.page(refresh=True) or "")


def test_concurrent_refreshes_join_one_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    file = experiment_file(tmp_path, "lab", REPORTED)
    started, release, joined = threading.Event(), threading.Event(), threading.Event()
    build_page = Pages.build
    generations = 0

    def blocked_build(self: Pages, path: Path) -> Page:
        nonlocal generations
        generations += 1
        started.set()
        if not release.wait(5):
            raise TimeoutError("test generation was not released")
        return build_page(self, path)

    class Joined(Future[str | None]):
        def result(self, timeout: float | None = None) -> str | None:
            # Only the waiter reaches result() while the owner is still building.
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr(Pages, "build", blocked_build)
    monkeypatch.setattr("etude.page.Future", Joined)
    with Server(runs, file, ("127.0.0.1", 0)) as server, ThreadPoolExecutor(2) as pool:
        first = pool.submit(server.page, refresh=True)
        try:
            assert started.wait(5)
            second = pool.submit(server.page, refresh=True)
            assert joined.wait(5)
        finally:
            release.set()
        assert first.result(5) == second.result(5) == server.page()
        assert generations == 1


def test_served_folder_index_reuses_output_until_refresh(tmp_path: Path) -> None:
    runs, folder = tmp_path / "runs", tmp_path / "experiments"
    trained(runs, "lab", "a")
    file = experiment_file(folder, "lab", REPORTED)
    measured = MeasuredStorage(DirectoryStorage(runs))
    with Server(measured, folder, ("127.0.0.1", 0)) as server:
        first = server.page()
        reads = measured.requests, measured.listings
        assert server.page() == first
        assert (measured.requests, measured.listings) == reads
        trained(runs, "lab", "b")
        assert '"name": "b"' not in (server.page() or "")
        assert '"name": "b"' in (server.page(refresh=True) or "")
        assert '"name": "b"' in (server.page("/lab") or "")
        assert '"refresh": null' in (first or "")
        detail = server.page("/lab")
        file.write_text(REPORTED + "\nraise RuntimeError('broken token=fixture-credential')\n")
        failed = server.page(refresh=True)
        assert failed is not None and "broken token=[redacted]" in failed
        assert "fixture-credential" not in failed
        assert server.page("/lab") == detail


def test_configured_schedule_round_trips_and_rejects_ambiguous_coordinates() -> None:
    schedule = Schedule(
        title="Learning rate",
        clock="updates",
        jobs={"large": (SchedulePoint(at=1000, value=0.1), SchedulePoint(at=2000, value=0.01))},
    )
    report = Report(ml=(schedule,))
    assert Report.model_validate_json(report.model_dump_json()) == report
    for points in ((), (SchedulePoint(at=1000, value=1), SchedulePoint(at=1000, value=2))):
        with pytest.raises(ValueError, match="schedule_points_must_increase"):
            Schedule(title="Bad schedule", clock="updates", jobs={"large": points})
    with pytest.raises(ValueError):
        SchedulePoint(at=float("nan"), value=1)


def test_http_folder_refresh_keeps_syntax_failures_and_recovers(tmp_path: Path) -> None:
    runs, folder = tmp_path / "runs", tmp_path / "experiments"
    trained(runs, "lab", "a")
    file = experiment_file(folder, "lab", REPORTED)
    # Remember recognised files even if their first build had no page to retain.
    empty = experiment_file(folder, "empty", REPORTED)
    experiment_file(folder, "helper", "def utility(): ...\n")
    with Server(runs, folder, ("127.0.0.1", 0)) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)

        def request(method: str, route: str) -> tuple[int, str]:
            client.request(method, route)
            reply = client.getresponse()
            return reply.status, reply.read().decode()

        def entries(html: str) -> Index:
            payload = html.split('<script type="application/json" id="data">', 1)[1]
            return Index.model_validate_json(payload.split("</script>", 1)[0], extra="ignore")

        try:
            status, html = request("GET", "/")
            assert status == 200
            assert {e.experiment for e in entries(html).entries} == {"lab", "empty"}
            status, original = request("GET", "/lab")
            assert status == 200
            file.write_text(REPORTED + "\ndef token=fixture-credential\n")
            empty.write_text("def report(\n")
            for _ in range(2):
                status, html = request("POST", "/")
                rows = {e.experiment: e for e in entries(html).entries}
                assert status == 200 and set(rows) == {"lab", "empty"}
                assert rows["lab"].href == "/lab"
                assert all(e.error and "SyntaxError" in e.error for e in rows.values())
                assert "fixture-credential" not in html
                assert request("GET", rows["lab"].href) == (200, original)
            status, error = request("POST", "/lab")
            assert status == 500 and "SyntaxError" in error
            assert "fixture-credential" not in error
            assert request("GET", "/lab") == (200, original)
            file.write_text(REPORTED.replace("Report()", 'Report(labels={"a": "Repaired"})'))
            empty.unlink()
            status, html = request("POST", "/")
            (row,) = entries(html).entries
            assert status == 200 and row.error is None and row.experiment == "lab"
            status, repaired = request("GET", row.href)
            assert status == 200 and '"Repaired"' in repaired and repaired != original
            file.unlink()
            status, html = request("POST", "/")
            assert status == 200 and entries(html).entries == ()
        finally:
            client.close()
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def test_http_refresh_reports_failures_without_replacing_the_cached_page(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    file = experiment_file(tmp_path, "lab", REPORTED)
    with Server(runs, file, ("127.0.0.1", 0)) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            client.request("GET", "/")
            reply = client.getresponse()
            assert reply.status == 200 and reply.getheader("Cache-Control") == "no-store"
            original = reply.read()
            file.write_text("raise RuntimeError('cannot generate token=fixture-credential')\n")
            client.request("POST", "/")
            reply = client.getresponse()
            error = reply.read()
            assert reply.status == 500 and b"cannot generate token=[redacted]" in error
            assert b"fixture-credential" not in error
            client.request("GET", "/")
            reply = client.getresponse()
            assert reply.status == 200 and reply.read() == original
            file.write_text(REPORTED)
            client.request("POST", "/")
            reply = client.getresponse()
            assert reply.status == 200 and b'"served": true' in reply.read()
        finally:
            client.close()
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def test_schedule_position_uses_the_current_continuation_not_an_older_overrun(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    file = experiment_file(tmp_path, "lab", REPORTED)
    with track(
        experiment="lab", run="a", clocks=[Step("updates", "updates")], directory=runs
    ) as run:
        run.log({"rl/loss": 1.0}, updates=1500)
    with track(
        experiment="lab",
        run="a",
        clocks=[Step("updates", "updates")],
        directory=runs,
        resumed_at={"updates": 1000},
    ) as run:
        run.log({"rl/loss": 0.5}, updates=1200)
    page = build(runs, file)
    assert page.training.runs[0].reached["updates"] == 1200
    with track(
        experiment="lab",
        run="a",
        clocks=[Step("updates", "updates")],
        directory=runs,
        resumed_at={"updates": 1100},
    ):
        pass
    assert build(runs, file).training.runs[0].reached["updates"] == 1100


def test_a_shared_report_module_of_the_repository_is_reread_on_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations: list[Path] = []

    def counted(storage: Storage | Path | str, file: Path) -> Page:
        generations.append(file)
        return build(storage, file)

    monkeypatch.setattr("etude.page.build", counted)
    runs = tmp_path / "runs"
    trained(runs, "frontier", "a")
    repository = tmp_path / "lab"
    repository.mkdir()
    (repository / "pyproject.toml").write_text("[project]\nname = 'lab'\n")
    experiment_file(repository / "lab_reports", "__init__")
    shared = experiment_file(
        repository / "lab_reports",
        "reports",
        "from etude.report import EvalPanel, Matrix, Report\n\n\n"
        "def standard_report(evals: tuple[EvalPanel, ...] = ()) -> Report:\n"
        "    return Report(labels={'a': 'Width 64'}, evals=(Matrix(title='Standard', rows='deck', columns='opponent'), *evals))\n",
    )
    # As an editable install of the repository puts it on the path.
    sys.path.insert(0, str(repository))
    file = experiment_file(
        repository / "experiments",
        "frontier",
        "from etude.report import Matrix, Report\nfrom lab_reports.reports import standard_report\n"
        "\n\ndef report() -> Report:\n"
        "    return standard_report(evals=(Matrix(title='Frontier only', rows='deck', columns='seat'),))\n",
    )
    with Server(runs, file, ("127.0.0.1", 0)) as server:
        first = server.page() or ""
        assert all(text in first for text in ('"Width 64"', '"Standard"', '"Frontier only"'))
        server.page(refresh=True)
        assert len(generations) == 1  # nothing changed, so nothing is rebuilt
        shared.write_text(shared.read_text().replace("Standard", "Edited standard"))
        assert '"Edited standard"' in (server.page(refresh=True) or "")
        assert len(generations) == 2
