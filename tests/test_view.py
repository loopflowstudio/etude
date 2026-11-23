"""What the local view serves while the writer and report keep moving."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, closing
from datetime import UTC, datetime, timedelta, tzinfo
from html import unescape
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws
from pydantic import TypeAdapter

from etude.block_file import block_key, tail_key
from etude.checkpoints import CheckpointRecord
from etude.cli import main
from etude.errors import LauncherError
from etude.home import home
from etude.jobs import ExperimentState, group_evaluations
from etude.launch.database import Database
from etude.launch.logs import snapshot_stream
from etude.launch.records import GPU
from etude.launch.server import LauncherObservation, PythonClient
from etude.page import Headline, Page, Pages, _run_file
from etude.records import ArtifactRef, Clock, RunCounters, RunOutcome, RunRecord, RunWriter, Tally
from etude.run import sync
from etude.spec import RunRequest, Until
from etude.storage import DirectoryStorage, S3Storage, storage_from_uri
from etude.train.block import Builder
from etude.train.tracking import write_block
from etude.view import Server, _machines_table
from etude.view_link import advertise, print_view
from tests.experiments import experiment_file
from tests.launch.counting import Counting, Counts
from tests.launch.fake import bench
from tests.launch.test_scheduler import _reporting, job
from tests.launch.test_source import git
from tests.launch.test_source import repository as repository
from tests.run.storage import CountedClient, MeasuredStorage
from tests.test_eval_groups import group as eval_group
from tests.test_eval_groups import job as eval_job
from tests.test_page import REPORTED, trained
from tests.train.tracking import open_run

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
    with Server(measured, file, ("127.0.0.1", 0)) as first:
        first.refresh_files()
    with Server(measured, file, ("127.0.0.1", 0)) as server:
        indexed = builds.read_text()
        listings = measured.listings
        measured.got.clear()
        assert '"name": "a"' in (server.page("/experiments/lab/report") or "")
        assert measured.listings == listings + 1
        assert measured.got.count("lab/a/run.json") == measured.got.count("other/c/run.json") == 1
        reads = measured.requests, measured.listings
        server.page("/experiments/lab/report")
        assert (measured.requests, measured.listings) == reads
        assert builds.read_text() == indexed + "built\n"
        # A copied experiment and a delivery note beneath lab/a leave lab's report current.
        trained(runs / "lab/a", "other", "d")
        measured.put("lab/a/.shipped/note", b"delivered")
        server.page("/experiments/lab/report", refresh=True)
        assert builds.read_text() == indexed + "built\n"
        trained(runs, "lab", "b")
        assert '"name": "b"' not in (server.page("/experiments/lab/report") or "")
        assert '"name": "b"' in (server.page("/experiments/lab/report", refresh=True) or "")
        assert builds.read_text() == indexed + "built\n" * 2
        assert measured.listings == listings + 3
        assert measured.got.count("lab/a/run.json") == measured.got.count("lab/b/run.json") == 1
        file.write_text(COUNTED + "# edited\n")
        server.page("/experiments/lab/report", refresh=True)
        assert builds.read_text() == indexed + "built\n" * 3


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
        _ingest(server)
        os.utime(helper, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        assert '"First"' in (server.page("/experiments/lab/report") or "")
        # Same size and second: Python's bytecode cache must not hide this edit.
        helper.write_text('NAME = "Other"\n')
        os.utime(helper, ns=(1_700_000_000_000_000_001, 1_700_000_000_000_000_001))
        assert '"Other"' in (server.page("/experiments/lab/report", refresh=True) or "")
        helper.write_text("NAME = (\n")
        with pytest.raises(SyntaxError):
            server.page("/experiments/lab/report", refresh=True)
        helper.write_text('NAME = "Repaired name"\n')
        assert '"Repaired name"' in (server.page("/experiments/lab/report", refresh=True) or "")


@pytest.mark.parametrize("refresh", [False, True], ids=["first-visit", "refresh"])
def test_readers_share_the_first_report_build_and_each_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refresh: bool
) -> None:
    runs = tmp_path / "runs"
    backend = bench(home())
    (address,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
    backend.store.close()
    source = DirectoryStorage(runs)
    measured = MeasuredStorage(source)
    route = "/experiments/lab/report"
    started, release, joined = threading.Event(), threading.Event(), threading.Event()
    build_page = Pages.build

    def blocked_build(self: Pages, path: Path | None, *, experiment: str | None = None) -> Page:
        started.set()
        if not release.wait(5):
            raise TimeoutError("test generation was not released")
        return build_page(self, path, experiment=experiment)

    class Joined(Future[str]):
        def result(self, timeout: float | None = None) -> str:
            joined.set()
            return super().result(timeout)

    with Server(measured, None, ("127.0.0.1", 0)) as server, ThreadPoolExecutor(2) as pool:
        server.refresh_launcher()
        assert ">queued<" in _get(server, "/experiments/lab")
        for _ in range(2):
            assert "Waiting for run files" in _get(server, route)
            assert _version(_get(server, route + "?observe=1")) == "waiting"
        assert (measured.listings, measured.requests) == (0, 0)
        _start_run(source, address, 7, job="learn")
        _ingest(server)
        reads = measured.listings, measured.requests
        assert _version(_get(server, route + "?observe=1")) == "ready"
        assert (measured.listings, measured.requests) == reads
        previous = _get(server, route) if refresh else None
        expected = [7, 3] if refresh else [7]
        if previous:
            _seal(runs / address, 1, 3)
            server.refresh_files()
        listings = measured.listings
        monkeypatch.setattr(Pages, "build", blocked_build)
        monkeypatch.setattr("etude.view.Future", Joined)
        first = pool.submit(server.page, route, refresh=refresh)
        try:
            assert started.wait(5)
            second = pool.submit(server.page, route, refresh=refresh)
            assert joined.wait(5)
            if previous:
                assert _get(server, route) == previous
            assert f"steps {len(expected)}" in _get(server, "/experiments/lab/status")
            assert _loss(_get(server, "/runs/" + address)) == expected
        finally:
            release.set()
        original = first.result(5)
        assert original and original == second.result(5) == _get(server, route)
        assert _loss(original) == expected
        assert measured.listings == listings + 1


def test_http_refresh_reports_failures_without_replacing_the_cached_page(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    file = experiment_file(tmp_path, "lab", REPORTED)
    with Server(runs, file, ("127.0.0.1", 0)) as server:
        _ingest(server)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            route = "/experiments/lab/report"
            original = _request(server, route)
            assert '"refresh": null' in original and 'id="refresh-report"' in original
            assert runs.as_uri() in _request(server, "/runs/absent", status=404)
            file.write_text("raise RuntimeError('cannot generate token=fixture-credential')\n")
            error = _request(server, route, method="POST", status=500)
            assert "cannot generate token=[redacted]" in error and "fixture-credential" not in error
            assert _request(server, route) == original
            file.write_text(REPORTED.replace("Report()", 'Report(labels={"a": "Readable"})'))
            refreshed = _request(server, route, method="POST")
            assert '"Readable"' in refreshed and _request(server, route) == refreshed
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def test_a_shared_report_module_of_the_repository_is_reread_on_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.syspath_prepend(repository)
    file = experiment_file(
        repository / "experiments",
        "frontier",
        "from etude.report import Matrix, Report\nfrom lab_reports.reports import standard_report\n"
        "\n\ndef report() -> Report:\n"
        "    return standard_report(evals=(Matrix(title='Frontier only', rows='deck', columns='seat'),))\n",
    )
    source = MeasuredStorage(DirectoryStorage(runs))
    with Server(source, file, ("127.0.0.1", 0)) as server:
        _ingest(server)
        first = server.page("/experiments/frontier/report") or ""
        assert all(text in first for text in ('"Width 64"', '"Standard"', '"Frontier only"'))
        reads = source.requests
        server.page("/experiments/frontier/report", refresh=True)
        assert source.requests == reads
        shared.write_text(shared.read_text().replace("Standard", "Edited standard"))
        assert '"Edited standard"' in (
            server.page("/experiments/frontier/report", refresh=True) or ""
        )


def _start_run(
    source: DirectoryStorage,
    address: str,
    loss: float,
    *,
    job: str | None = None,
    seed: int | None = None,
) -> None:
    record = RunRecord(
        id=address,
        experiment=address.partition("/")[0],
        job_name=job,
        seed=seed,
        clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
    )
    source.put(address + "/run.json", record.model_dump_json().encode())
    _seal(source.root / address, 0, loss)


def _seal(
    root: Path, sequence: int, loss: float, *, writer: str = "123456abcdef", step: int | None = None
) -> str:
    builder = Builder(["steps"])
    step = sequence + 1 if step is None else step
    builder.add(999 + step, {"steps": float(step)}, {"loss": loss})
    name = tail_key(sequence, writer)
    write_block(root, name, builder.columns(writer, sequence))
    return name


def _page_data(html: str) -> Page:
    payload = html.split('<script type="application/json" id="data">')[1].split("</script>")[0]
    return Page.model_validate_json(payload, extra="ignore")


def _loss(html: str, clock: str = "steps") -> list[float | None]:
    return _page_data(html).training.series[clock]["loss"][0].mean


def _version(html: str) -> str:
    return html.split('data-version="', 1)[1].split('"', 1)[0]


def _ingest(view: Server) -> None:
    # Drive one pass of each independent poll, without waiting for the next cadence.
    view.refresh_launcher()
    view.refresh_files()


def _advance_view_clock(patch: pytest.MonkeyPatch, *, minutes: int) -> None:
    class Later(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return super().now(tz) + timedelta(minutes=minutes)

    patch.setattr("etude.view.datetime", Later)


def _request(view: Server, path: str, *, method: str = "GET", status: int = 200) -> str:
    with closing(HTTPConnection("127.0.0.1", view.server_port, timeout=5)) as client:
        client.request(method, path)
        with client.getresponse() as reply:
            assert reply.status == status and reply.getheader("Cache-Control") == "no-store"
            return reply.read().decode()


def _get(view: Server, path: str, *, status: int = 200) -> str:
    # Serve one real request while the fixture controls ingestion independently.
    with ThreadPoolExecutor(1) as listener:
        handled = listener.submit(view.handle_request)
        page = _request(view, path, status=status)
        handled.result(5)
        return page


def _publish_declarations(server: Server) -> None:
    # Tests drive the real ingestion loop one generation at a time.
    _, pending = wait([reading.future for reading in server.index._reports.values()], timeout=5)
    assert not pending
    _ingest(server)


def _publish_ssh(server: Server) -> None:
    if future := server.index._remote_read:
        assert wait((future,), timeout=10).done
        _ingest(server)


def test_report_edits_and_a_restart_leave_both_experiments_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    for name in ("lab", "other"):
        _start_run(source, name + "/base@0", 7)
    folder = tmp_path / "experiments"
    authored = (
        "from etude.report import Report, Series\n\n"
        "def report() -> Report:\n"
        "    return Report(ml=(Series(title='Lab loss', keys=('loss',)),))\n"
    )
    file = experiment_file(folder, "lab", authored)
    measured = MeasuredStorage(source)
    lab = "/runs/lab/base@0"
    with Server(measured, folder, ("127.0.0.1", 0)) as server:
        _ingest(server)
        _publish_declarations(server)
        assert '"Lab loss"' in (server.page(lab) or "")

    # Folder discovery must still recognise the previously accepted, now unparseable file.
    file.write_text("def report(\n")
    started, release, ended = threading.Event(), threading.Event(), threading.Event()

    def held(path: Path) -> dict[str, object]:
        # read_report holds the actual author-import lock while executing the file.
        started.set()
        try:
            if not release.wait(5):
                raise TimeoutError("report was not released")
            return _run_file(path)
        finally:
            ended.set()

    monkeypatch.setattr("etude.page._run_file", held)
    with Server(measured, folder, ("127.0.0.1", 0)) as server:
        _ingest(server)
        try:
            assert started.wait(5)
            for name in ("lab", "other"):
                _seal(source.root / name / "base@0", 1, 3)
            _ingest(server)
            reads = measured.listings, measured.requests, measured.bytes
            for name in ("lab", "other"):
                assert _loss(server.page(f"/runs/{name}/base@0") or "") == [7, 3]
                assert "steps 2" in (server.page(f"/experiments/{name}/status") or "")
            assert '"Lab loss"' in (server.page(lab) or "")
            assert (measured.listings, measured.requests, measured.bytes) == reads
            assert not ended.is_set()
        finally:
            release.set()
        _publish_declarations(server)
        assert "SyntaxError" in (server.page("/experiments/lab/status") or "")
        page = server.page(lab) or ""
        assert "SyntaxError" in page and '"Lab loss"' in page
        assert "SyntaxError" not in (server.page("/experiments/other/status") or "")
        assert "SyntaxError" not in (server.page("/runs/other/base@0") or "")
        for name in ("lab", "other"):
            _seal(source.root / name / "base@0", 2, 1)
        _ingest(server)
        for name in ("lab", "other"):
            assert _loss(server.page(f"/runs/{name}/base@0") or "") == [7, 3, 1]
            assert "steps 3" in (server.page(f"/experiments/{name}/status") or "")
        file.write_text(authored.replace("Lab loss", "Repaired loss"))
        # One read may already have observed the broken edit.
        _publish_declarations(server)
        _publish_declarations(server)
        page = server.page(lab) or ""
        assert "SyntaxError" not in page and '"Repaired loss"' in page and _loss(page) == [7, 3, 1]


def test_first_broken_report_shows_default_panels_and_a_sanitized_error(tmp_path: Path) -> None:
    file = experiment_file(tmp_path, "lab", "raise ValueError('token=fixture-value')\n")
    with open_run(
        experiment="lab", run="base@0", directory=tmp_path / "runs", clocks=["steps"]
    ) as run:
        run.log({"loss": 7}, steps=1)
    with Server(tmp_path / "runs", file, ("127.0.0.1", 0)) as server:
        _ingest(server)
        _publish_declarations(server)
        page = server.page("/runs/lab/base@0") or ""
        assert _loss(page) == [7] and "token=[redacted]" in page and "fixture-value" not in page
        route = "/experiments/lab/report"
        failed = _get(server, route, status=500)
        assert "Retry report" in failed and "token=[redacted]" in failed
        assert "fixture-value" not in failed and 'id="report-observation"' not in failed
        assert '<nav id="view-rail"' in failed
        file.write_text(REPORTED.replace("Report()", "Report(other_metrics=True)"))
        built = _get(server, route)
        assert "Refresh report" in built and _loss(built) == [7]


def test_ingestion_reads_only_the_new_block_and_publishes_its_last_point(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    root = source.root / "lab/base@0"
    _start_run(source, "lab/base@0", 7)
    measured = MeasuredStorage(source)
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        first = server.page("/runs/lab/base@0") or ""
        assert ">running<" in first and "Last file write" in first and _loss(first) == [7.0]
        assert "Console was not recorded." in first
        before = measured.requests, measured.listings
        measured.got.clear()
        added = _seal(root, 1, 3.0)
        _ingest(server)
        assert measured.got == ["lab/base@0/" + added]
        assert (measured.requests, measured.listings) == (before[0] + 1, before[1] + 1)
        assert _loss(server.page("/runs/lab/base@0") or "") == [7.0, 3.0]


def test_a_reopened_view_dates_open_files_without_rereading_sealed_data(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    address = "lab/base@0"
    with open_run(experiment="lab", run="base@0", directory=source.root, clocks=["steps"]) as run:
        run.log({"loss": 7}, steps=1)
    source.delete(address + "/outcome.json")
    for key in source.keys(""):
        os.utime(source.root / key, (100, 100))
    measured = MeasuredStorage(source)
    with Server(measured, None, ("127.0.0.1", 0)) as first:
        _ingest(first)
        page = first.page("/runs/" + address) or ""
        assert ">no end recorded<" in page and _loss(page) == [7]

    # Keep the published index across restart with an empty object cache.
    (home() / "cache").rename(home() / "evicted")
    source.put(address + "/pending.open", b"unsealed")
    # A nested run's metrics can arrive before its declaration. They belong to that run.
    _seal(source.root / address / "child", 0, 99)
    measured.got.clear()
    before = measured.listings, measured.requests
    with Server(measured, None, ("127.0.0.1", 0)) as reopened:
        _ingest(reopened)
        page = reopened.page("/runs/" + address) or ""
        assert ">running<" in page and "steps 1" in page and _loss(page) == [7]
        assert measured.got == [address + "/run.json"]
        assert (measured.listings, measured.requests) == (before[0] + 1, before[1] + 1)


def test_graph_requests_reuse_the_published_generation_after_interruption(tmp_path: Path) -> None:
    storage = DirectoryStorage(tmp_path / "runs")
    root = storage.root / "lab/base@0"
    with open_run(experiment="lab", run="base@0", directory=storage.root, clocks=["steps"]) as run:
        run.log({"loss": 1}, steps=1)
    measured = MeasuredStorage(storage)
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        assert _loss(server.page("/runs/lab/base@0") or "") == [1]
    # An unparseable replacement interrupts a cold writer before publication.
    outcome = root / "outcome.json"
    saved = outcome.read_bytes()
    outcome.write_text("{")
    with Server(measured, None, ("127.0.0.1", 0)) as restarted:
        _ingest(restarted)
        reads = measured.listings, measured.requests, measured.bytes
        page = restarted.page("/runs/lab/base@0") or ""
        assert _loss(page) == [1] and ">finished<" in page and "Run files:" in page
        status = restarted.page("/experiments/lab/status") or ""
        assert ">finished<" in status and "Run files:" in status and 'data-refresh="5000"' in status
        overview = restarted.page("/") or ""
        assert "1 finished" in overview and 'data-refresh="5000"' in overview
        assert "Run files:" in overview
        assert (measured.listings, measured.requests, measured.bytes) == reads
        outcome.write_bytes(saved)
        _ingest(restarted)
        assert "Run files:" not in _get(restarted, "/status")


def test_an_interrupted_writer_never_exposes_half_a_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    for name in ("a", "b"):
        _start_run(source, "lab/" + name, 5)

    measured = MeasuredStorage(source)
    with Server(measured, None, ("127.0.0.1", 0)) as writer:
        _ingest(writer)
        _seal(source.root / "lab/a", 1, 4)
        tail = "lab/b/" + _seal(source.root / "lab/b", 1, 3)
        get = source.get
        reader_points: list[float | None] = []
        with monkeypatch.context() as patch:

            def interrupted(key: str) -> bytes:
                if key != tail:
                    return get(key)
                # Another view reads the published pair while the next pair is incomplete.
                with Server(measured, None, ("127.0.0.1", 0)) as reader:
                    _ingest(reader)
                    reader_points.extend(_loss(reader.page("/runs/lab/a") or ""))
                raise OSError("interrupted reading the second chart")

            patch.setattr(source, "get", interrupted)
            before = measured.listings
            _ingest(writer)
        assert measured.listings == before + 1
        assert reader_points == [5]
        assert "interrupted reading the second chart" in _get(writer, "/status")
        assert _loss(writer.page("/runs/lab/a") or "") == [5]
        _ingest(writer)
        assert _loss(writer.page("/runs/lab/a") or "") == [5, 4]
        assert _loss(writer.page("/runs/lab/b") or "") == [5, 3]


def test_a_folded_block_keeps_its_runs_observation_while_other_runs_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    _start_run(source, "lab/a", 7)
    _start_run(source, "other/b", 5)
    measured = MeasuredStorage(source)
    now = datetime.now(UTC)

    class ViewClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(now.timestamp(), tz)

    monkeypatch.setattr("etude.view.datetime", ViewClock)
    tail = "lab/a/" + tail_key(1, "123456abcdef")
    recent = "lab/a/" + block_key(1, 1, 1, "123456abcdef")
    permanent = "lab/a/" + block_key(2, 1, 1, "123456abcdef")
    replacements = {tail: recent, recent: permanent}
    get = source.get

    def fold_before_read(key: str) -> bytes:
        if replacement := replacements.get(key):
            source.put(replacement, get(key))
            os.utime(source.root / replacement, (now.timestamp(), now.timestamp()))
            source.delete(key)
        return get(key)

    def check_held(view: Server, age: str) -> None:
        reads = measured.listings, measured.requests
        graph = _get(view, "/runs/lab/a")
        status = _get(view, "/experiments/lab/status")
        check = _get(view, "/runs/lab/a?observe=1")
        assert _version(check) == _version(graph) and _loss(graph) == [7]
        for page in (graph, status):
            assert ">running<" in page and ">steps 1<" in page
        for page in (graph, check, status):
            assert f"files observed {age}" in page
        overview = _get(view, "/status")
        assert "Run files listed 0 s ago" in status and "Run files listed 0 s ago" in overview
        assert "/experiments/lab" in overview.split("<h2>Completed</h2>")[0]
        sibling = _get(view, "/runs/other/b")
        assert _loss(sibling) == [5, 4] and "files observed 0 s ago" in sibling
        assert (measured.listings, measured.requests) == reads

    with Server(measured, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        now += timedelta(minutes=11)
        _seal(source.root / "lab/a", 1, 3)
        sibling_tail = _seal(source.root / "other/b", 1, 4)
        os.utime(source.root / "other/b" / sibling_tail, (now.timestamp(), now.timestamp()))
        monkeypatch.setattr(source, "get", fold_before_read)
        server.refresh_files()
        assert measured.got.count(tail) == 1
        assert "Run files:" not in _get(server, "/status")
        check_held(server, "11 min ago")

    with Server(measured, None, ("127.0.0.1", 0)) as reopened:
        check_held(reopened, "11 min ago")
        now += timedelta(minutes=1)
        reopened.refresh_files()
        assert measured.got.count(recent) == 1
        assert "Run files: lab/a:" in _get(reopened, "/status")
        check_held(reopened, "12 min ago")

        reopened.refresh_files()
        graph = _get(reopened, "/runs/lab/a")
        assert "Run files:" not in graph
        assert ">running<" in graph and ">steps 2<" in graph and _loss(graph) == [7, 3]
        assert "files observed 0 s ago" in graph
        assert "files observed" not in _get(reopened, "/experiments/lab/status")

        now += timedelta(minutes=10)
        reopened.refresh_files()
        assert ">no end recorded<" in _get(reopened, "/runs/lab/a")
        assert "/experiments/lab" in _get(reopened, "/status").split("<h2>Completed</h2>")[1]


@pytest.mark.parametrize("previously_read", [False, True], ids=["first-read", "unchanged-inputs"])
def test_a_run_accepts_the_next_listing_after_a_missing_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previously_read: bool
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    backend = bench(home())
    (address,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
    backend.store.close()
    _start_run(source, address, 7)
    with Server(source, None, ("127.0.0.1", 0)) as view:
        view.refresh_launcher()
        if previously_read:
            view.refresh_files()
        extra = address + "/" + _seal(source.root / address, 1, 3)
        get = source.get

        def disappear(key: str) -> bytes:
            if key == extra:
                source.delete(key)
            return get(key)

        monkeypatch.setattr(source, "get", disappear)
        _advance_view_clock(monkeypatch, minutes=11)
        view.refresh_files()
        graph = _get(view, "/runs/" + address)
        if previously_read:
            assert _loss(graph) == [7] and "files observed 11 min ago" in graph
        else:
            assert ">queued<" in graph and "File observation unavailable" in graph
            assert "Curves appear once the view has read the run files." in graph
            assert "Waiting for run files" in _get(view, "/experiments/lab/report")
        view.refresh_files()
        graph = _get(view, "/runs/" + address)
        assert _loss(graph) == [7] and "files observed 11 min ago" not in graph
        assert "files observed " in graph and "File observation unavailable" not in graph
        for key in source.keys(""):
            source.delete(key)
        view.refresh_files()
        graph = _get(view, "/runs/" + address)
        assert ">queued<" in graph and "No metric curves recorded." in graph


def test_view_links_follow_only_the_live_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    address = home() / "view"
    address.write_text("http://stale.invalid")
    print_view("lab/base@0")
    assert capsys.readouterr().out == ""
    with Server(tmp_path, None, ("127.0.0.1", 0)) as first:
        with Server(tmp_path, None, ("127.0.0.1", 0)) as second:
            _ingest(first)
            print_view("lab/base@0")
            assert capsys.readouterr().out.strip() == f"view: {second.origin}/runs/lab/base@0"
        _ingest(first)
        print_view("lab/base@0")
        assert capsys.readouterr().out.strip() == f"view: {first.origin}/runs/lab/base@0"
    assert not address.exists()


def test_bucket_listing_validates_held_objects_without_another_get(tmp_path: Path) -> None:
    source = tmp_path / "runs"
    with open_run(experiment="lab", run="base@0", clocks=["steps"], directory=source) as run:
        run.log({"loss": 2.0}, steps=1)
    local = DirectoryStorage(source)
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="runs")
        bucket = S3Storage("runs", client=client)
        for key in local.keys(""):
            if not key.endswith("outcome.json"):
                bucket.put(key, local.get(key))
        with Server(bucket, None, ("127.0.0.1", 0)) as server:
            _ingest(server)
            assert ">running<" in (server.page("/runs/lab/base@0") or "")
            reads = client.requests
            bucket.put("lab/base@0/outcome.json", local.get("lab/base@0/outcome.json"))
            _ingest(server)
            assert client.requests == reads + 1
            html = server.page("/runs/lab/base@0") or ""
            assert ">finished<" in html and _loss(html) == [2.0]
            _ingest(server)
            assert client.requests == reads + 1


def test_two_files_with_one_name_leave_runs_refreshing(tmp_path: Path) -> None:
    folder = tmp_path / "experiments"
    experiment_file(folder, "lab", REPORTED)
    experiment_file(folder / "copy", "lab", REPORTED)
    source = DirectoryStorage(tmp_path / "runs")
    root = source.root / "lab/base@0"
    _start_run(source, "lab/base@0", 7)
    with Server(source, folder, ("127.0.0.1", 0)) as server:
        _ingest(server)
        _seal(root, 1, 3.0)
        _ingest(server)
        assert _loss(server.page("/runs/lab/base@0") or "") == [7.0, 3.0]
        assert "experiment_named_twice" in (server.page("/status") or "")


def test_indexed_experiment_opens_its_default_report_separately(tmp_path: Path) -> None:
    with open_run(experiment="lab", run="base@0", clocks=["steps"], directory=tmp_path) as run:
        run.log({"loss": 8}, steps=1)
    source = MeasuredStorage(DirectoryStorage(tmp_path))
    with Server(source, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        before = (source.requests, source.listings)
        html = server.page("/experiments/lab") or ""
        assert (
            'data-src="/experiments/lab/report?embed=1"' in html
            and 'href="/runs/lab/base@0"' in html
        )
        assert (source.requests, source.listings) == before
        assert _loss(server.page("/experiments/lab/report") or "") == [8.0]


def test_run_overview_and_tab_polls_use_only_indexed_clock_cost_and_files(tmp_path: Path) -> None:
    backend = bench(home())
    backend.clock.now = datetime.now(UTC) - timedelta(hours=2)
    (address,) = backend.scheduler.queue(
        job(name="base", experiment="lab", until="steps=400"), rent="l4"
    )
    backend.scheduler.tick()
    backend.run(1)
    source = DirectoryStorage(tmp_path / "runs")
    _start_run(source, address, 7)
    # This run first wrote at step 100. Only the next 60 steps took this minute.
    writer = "123456abcdef"
    rows = Builder(["steps"])
    start = int(datetime(2026, 11, 22, 3, tzinfo=UTC).timestamp() * 1000)
    rows.add(start, {"steps": 100}, {"loss": 7, "validation": 6})
    rows.add(start + 60_000, {"steps": 160}, {"loss": 3, "validation": 4})
    write_block(source.root / address, tail_key(0, writer), rows.columns(writer, 0))
    mark = CheckpointRecord(ordinal=1, at={"steps": 120}, artifact=ArtifactRef(uri="weights.pt"))
    source.put(address + "/checkpoints/first.json", mark.model_dump_json().encode())
    measured = MeasuredStorage(source)
    route = "/runs/" + address
    with Server(measured, None, ("127.0.0.1", 0)) as view:
        _ingest(view)
        reads = measured.listings, measured.requests, measured.bytes
        overview = _get(view, route)
        assert 'href="?tab=overview" aria-current="page"' in overview
        assert set(_page_data(overview).training.series["steps"]) == {"loss"}
        for label, value in (
            ("Clock", "steps 160 / 400"),
            ("Latest score", "loss 3"),
            ("Mean pace", "1 steps/s"),
            ("Ends about", "2026-11-22 03:05 UTC"),
            ("Dollars", "~$0.59"),
            ("Machine rate", "$0.59/hour"),
        ):
            assert f"<dt>{label}</dt><dd>{value}</dd>" in overview
        assert (
            '<dt>Last file write</dt><dd id="last-write">' in overview and " s ago</dd>" in overview
        )
        assert "<h2>Console</h2>" in overview and "<h2>Checkpoints</h2>" in overview
        assert 'data-last-checkpoint="{&quot;steps&quot;: 120.0}"' in overview
        poll = _get(view, route + "?observe=1")
        assert _version(poll) == _version(overview) and "<script" not in poll
        for tab, expected in (
            ("charts", "<h2>Performance "),
            ("console", "<h2>Console</h2>"),
            ("config", "<h2>Config</h2>"),
            ("checkpoints", ">weights.pt<"),
            ("files", f">{address}/checkpoints/first.json<"),
        ):
            page = _get(view, route + f"?tab={tab}")
            assert f'href="?tab={tab}" aria-current="page"' in page and expected in page
            assert '<dl class="run-numbers">' not in page
            assert ('id="data"' in page) == (tab == "charts")
            if tab == "charts":
                assert set(_page_data(page).training.series["steps"]) == {"loss", "validation"}
            assert _version(_get(view, route + f"?observe=1&tab={tab}")) == _version(page)
        assert (measured.listings, measured.requests, measured.bytes) == reads
        files = _get(view, route + "?tab=files")
        source.put(address + "/progress.open", b"unsealed")
        view.refresh_files()
        assert _version(_get(view, route + "?observe=1&tab=files")) != _version(files)
        assert f">{address}/progress.open<" in _get(view, route + "?tab=files")
        assert _version(_get(view, route + "?observe=1")) == _version(overview)
        source.put(
            address + "/outcome.json",
            RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode(),
        )
        view.refresh_files()
        ended = _get(view, route)
        assert ">finished<" in ended and "<dt>Ends about</dt>" not in ended
    backend.store.close()


def test_compact_run_link_opens_with_charts_and_links_back_to_its_experiment(
    tmp_path: Path,
) -> None:
    source = DirectoryStorage(tmp_path)
    _start_run(source, "coin", 7)
    with Server(source, None, ("127.0.0.1", 0)) as view:
        view.refresh_files()
        page = _get(view, "/runs/coin")
        assert '<a href="/experiments/coin">Experiment overview</a>' in page
        charts = _get(view, "/runs/coin?tab=charts")
        assert charts.index("<h2>Charts ") < charts.index("<h2>Performance ")
        assert _loss(page) == [7]
        report = _get(view, "/experiments/coin/report")
        assert report.index("<h2>Perf ") < report.index("<h2>ML details ")
        assert 'id="refresh-report"' in report


def test_first_evaluation_files_enable_the_report_without_training_curves(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    record = RunRecord(id="lab/duel", experiment="lab", kind="evaluation")
    source.put("lab/duel/run.json", record.model_dump_json().encode())
    with Server(source, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        route = "/experiments/lab/report"
        assert _version(_get(server, route + "?observe=1")) == "ready"
        report = _get(server, route)
        assert "Refresh report" in report and "Partial results" in report


def test_experiment_status_tracks_jobs_placement_and_sealed_clocks(tmp_path: Path) -> None:
    backend = bench(home())
    spec = job(name="learn", experiment="lab", until="steps=40")
    (running,) = backend.scheduler.queue(RunRequest(id="learn@0", seed=0, job=spec), rent="l4")
    backend.scheduler.tick()
    backend.run(1)
    machine = backend.scheduler.read(running).machine
    assert machine
    (queued,) = backend.scheduler.queue(RunRequest(id="learn@1", seed=1, job=spec), rent="l4")
    (home() / "server-heartbeat").write_text((datetime.now(UTC) - timedelta(hours=2)).isoformat())
    source = DirectoryStorage(tmp_path / "runs")
    local = "lab/local@0"
    _start_run(source, local, 7, job="baseline")
    evaluation = "lab/duel"
    source.put(
        evaluation + "/run.json",
        RunRecord(id=evaluation, experiment="lab", kind="evaluation", job_name="score")
        .model_dump_json()
        .encode(),
    )
    outcome = RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode()
    source.put(evaluation + "/outcome.json", outcome)
    measured = MeasuredStorage(source)
    with Server(measured, None, ("127.0.0.1", 0)) as server:

        def status(path: str) -> str:
            reads = measured.listings, measured.requests, measured.bytes
            html = server.page(path)
            assert html is not None
            assert (measured.listings, measured.requests, measured.bytes) == reads
            return html

        def row(html: str, address: str) -> str:
            return next(r for r in html.split("</tr>") if f'title="{address}"' in r)

        _ingest(server)
        html = status("/experiments/lab")
        assert ">running<" in row(html, running) and machine in row(html, running)
        assert ">learn<" in row(html, queued) and ">queued<" in row(html, queued)
        reason = backend.scheduler.read(queued).reason
        assert reason and reason in row(html, queued) and "Target steps 40" in row(html, queued)
        assert ">baseline<" in row(html, local) and ">steps 1<" in row(html, local)
        assert ">score<" in row(html, evaluation) and ">finished<" in row(html, evaluation)
        assert "last loop 2 h ago" in html and 'data-src="/experiments/lab/report?embed=1"' in html
        eval_page = status("/runs/" + evaluation)
        assert f"Evaluation · {evaluation}" in eval_page and ">finished<" in eval_page
        assert 'href="/experiments/lab#report">lab report</a>' in eval_page
        assert "last file write" in eval_page
        assert all(
            text not in eval_page
            for text in ("No metric curves", "Checkpoints", "<h2>Config", "<h2>Console")
        )
        overview = status("/")
        opened, completed = overview.split("<h2>Completed</h2>")
        assert 'href="/experiments/lab"' in opened and "No completed experiments." in completed
        assert all(count in opened for count in ("1 queued", "2 running", "1 finished"))
        assert all(f'href="/runs/{address}"' in opened for address in (running, queued, local))
        assert machine in opened and "steps 1" in opened and "last loop 2 h ago" in overview
        _seal(source.root / local, 1, 3)
        _ingest(server)
        updated = status("/experiments/lab/status")
        assert ">steps 2<" in row(updated, local) and ">running<" in row(updated, local)
        assert 'data-refresh="5000"' in updated and "<iframe" not in updated
        assert "steps 2" in status("/status")
        source.put(local + "/outcome.json", outcome)
        _ingest(server)
        assert ">finished<" in row(status("/experiments/lab/status"), local)
        # A saved terminal outcome can lead the launcher while it releases the attempt.
        source.put(
            running + "/run.json",
            RunRecord(id=running, job_name="learn", seed=0, experiment="lab")
            .model_dump_json()
            .encode(),
        )
        source.put(running + "/outcome.json", outcome)
        _ingest(server)
        ending = row(status("/experiments/lab/status"), running)
        assert ">finished<" in ending and ">running<" not in ending and machine in ending
        # The local outcome alone cannot close an experiment with outstanding launcher work.
        assert "3 finished" in status("/status").split("<h2>Completed</h2>")[0]
        backend.scheduler.cancel(queued)
        backend.run(600)
        _ingest(server)
        settled = status("/status")
        opened, completed = settled.split("<h2>Completed</h2>")
        assert "No open experiments." in opened and 'href="/experiments/lab"' in completed
        assert "3 finished" in completed and "1 failed" in completed
        assert 'data-refresh="5000"' in settled
    backend.store.close()


@pytest.mark.parametrize("resume", [False, True], ids=["lost", "finished"])
def test_replacement_allocation_keeps_the_curve_and_supplies_the_phase(
    tmp_path: Path, resume: bool
) -> None:
    backend = bench(home())
    spec = job(name="learn", experiment="lab", until="steps=40", max_attempts=2)
    request = RunRequest(id="learn@0", seed=0, job=spec)
    (run,) = backend.scheduler.queue(request, rent="l4")
    backend.scheduler.tick()
    first = backend.scheduler.attempts(run)[0]
    source = DirectoryStorage(tmp_path / "runs")
    measured = MeasuredStorage(source)
    _start_run(source, run, 7, job="learn", seed=0)

    def writer(name: str, attempt: str, start: int) -> None:
        record = RunWriter(writer=name, host="lab", attempt=attempt, started_ms=start)
        source.put(f"{run}/metrics/{name}.json", record.model_dump_json().encode())

    def finish(name: str | None, *, stopped: bool = False) -> None:
        outcome = RunOutcome(
            state="stopped" if stopped else "finished", counters=RunCounters(), writer=name
        )
        source.put(f"{run}/outcome.json", outcome.model_dump_json().encode())

    def check(view: Server, phase: str, loss: list[int], *, occupied: bool = True) -> None:
        reads = measured.listings, measured.requests, measured.bytes
        graph = _get(view, "/runs/" + run)
        assert _loss(graph) == loss and ">weights.pt<" in graph
        pages = [
            graph,
            _get(view, "/experiments/lab/status"),
        ]
        overview = _get(view, "/status").split("<h2>Completed")[0]
        assert f'data-state="{phase}"' in overview and f"steps {len(loss)} /" in overview
        if occupied:
            pages.append(_get(view, "/machines/status"))
        for page in pages:
            assert f'<span class="pill">{phase}</span>' in page and f">steps {len(loss)} /" in page
        assert (measured.listings, measured.requests, measured.bytes) == reads

    checkpoint = CheckpointRecord(
        ordinal=1, at={"steps": 1}, wall_ms=1000, artifact=ArtifactRef(uri="weights.pt")
    )
    source.put(f"{run}/checkpoints/first.json", checkpoint.model_dump_json().encode())
    with Server(measured, None, ("127.0.0.1", 0)) as view:
        _ingest(view)
        check(view, "setting up", [7])  # a row without an allocation cannot confirm execution
        writer("123456abcdef", first.id, 999)
        view.refresh_files()
        check(view, "running", [7])
        backend.run(1)
        if resume:
            finish("123456abcdef")
            view.refresh_files()
            check(view, "finished", [7])
            backend.run(600)
            extended = spec.model_copy(update={"until": Until(clock="steps", value=80)})
            backend.scheduler.queue(
                RunRequest(id=run, seed=0, job=extended, resume=True), rent="l4"
            )
            view.refresh_launcher()
            check(view, "queued", [7], occupied=False)
            backend.scheduler.tick()
        else:
            backend.service.kill(first.session)
            backend.run(1)
        second = backend.scheduler.attempts(run)[1]
        view.refresh_launcher()
        check(view, "setting up", [7])

    with Server(measured, None, ("127.0.0.1", 0)) as restarted:
        check(restarted, "setting up", [7])
        if not resume:
            # A delayed legacy end and its only writer are still on the lost machine.
            finish(None)
            restarted.refresh_files()
            check(restarted, "setting up", [7])
        writer("abcdef123456", second.id, 1001)
        restarted.refresh_files()
        check(restarted, "running", [7])
        _seal(source.root / run, 0, 3, writer="abcdef123456", step=2)
        restarted.refresh_files()
        check(restarted, "running", [7, 3])
        finish("abcdef123456", stopped=not resume)
        restarted.refresh_files()
        check(restarted, "finished" if resume else "stopped", [7, 3])
        assert backend.scheduler.read(run).phase == "setting_up"
    backend.store.close()


def test_continuation_link_from_queue_to_delivery_survives_view_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = DirectoryStorage(tmp_path / "source")
    measured = MeasuredStorage(source)
    backend = bench(home())
    spec = job(name="talk", experiment="paced", until="updates=150")
    (parent,) = backend.scheduler.queue(RunRequest(id="paced/talk@0", seed=0, job=spec), rent="l4")
    backend.scheduler.tick()
    backend.run(601)
    with open_run(
        experiment="paced",
        run=parent,
        job="talk",
        seed=0,
        clocks=["updates"],
        directory=source.root,
    ) as writer:
        writer.log({"loss": 7}, updates=150)
        (writer.out / "saved").write_text("150")
    previous = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    for key in source.keys(parent + "/"):
        os.utime(source.root / key, (previous, previous))
    parent_page = "/runs/" + parent
    handoff = f'Continued from <a href="{parent_page}">{parent}</a> at updates 150'

    def check(view: Server, phase: str) -> str:
        before = measured.listings, measured.requests
        table = _get(view, "/experiments/paced/status")
        rows = [row for row in table.split("<tr>") if "<td>" in row]
        assert len(rows) == 2
        for path, label in ((parent_page, "finished"), (child_page, phase)):
            row = next(row for row in rows if f'title="{path.removeprefix("/runs/")}"' in row)
            assert f">{label}<" in row and (handoff in row) == (path == child_page)
        parent_graph = _get(view, parent_page)
        child_graph = _get(view, child_page)
        assert handoff in child_graph and "Continued from" not in parent_graph
        assert (
            f"etude logs --run {child}" in child_graph
            and f"etude logs --run {parent}" in parent_graph
        )
        assert _loss(parent_graph, "updates") == [7]
        assert "updates 150 / 150" in parent_graph and 'id="last-write">2 h ago' in parent_graph
        assert f">{phase}<" in child_graph
        if phase in ("queued", "setting up"):
            assert "Target updates 200" in child_graph and 'id="data"' not in child_graph
            assert child_page in _get(view, "/status").split("<h2>Completed")[0]
        if phase == "setting up":
            slots = _get(view, "/machines/status")
            assert child_page in slots and parent_page not in slots
            assert f"machine {attempt.machine}" in child_graph
        assert (measured.listings, measured.requests) == before
        return child_graph

    with Server(measured, None, ("127.0.0.1", 0)) as view:
        # Locating a local parent also supplies the listing for its checkpoint and out.
        with monkeypatch.context() as patch:
            patch.setenv("ETUDE_TRACK_DIR", str(source.root))
            patch.setattr(
                "etude.storage.storage_from_uri",
                lambda root: measured if root == source.root else storage_from_uri(root),
            )
            listings = measured.listings
            assert main(["continue", "--run", parent, "--until", "updates=200"]) == 0
            assert measured.listings == listings + 1
        output = capsys.readouterr().out.splitlines()
        child = output[0].split()[0]
        child_page = "/runs/" + child
        assert child != parent and output[-1] == f"view: {view.origin}{child_page}"
        _ingest(view)
        assert main(["status", child]) == 0
        assert f"view: {view.origin}{child_page}" in capsys.readouterr().out.splitlines()
        assert "no files yet" in check(view, "queued")
        backend.scheduler.tick()
        (attempt,) = backend.scheduler.attempts(child)
        (parent_attempt,) = backend.scheduler.attempts(parent)
        for allocation, message in ((parent_attempt, b"parent output"), (attempt, b"child output")):
            backend.store.retain_log(
                snapshot_stream(allocation.session, allocation.id, "job", message, True)
            )
        view.refresh_launcher()
        assert "no files yet" in check(view, "setting up")
        child_console = _get(view, child_page)
        parent_console = _get(view, parent_page)
        assert "child output" in child_console and "parent output" not in child_console
        assert "parent output" in parent_console and "child output" not in parent_console
        with monkeypatch.context() as patch:
            patch.setenv("ETUDE_ATTEMPT", attempt.id)
            for variable in backend.scheduler.record(child).launch.spec.environment:
                patch.setenv(variable.name, variable.value)
            with open_run(
                experiment="paced",
                run=child,
                job="talk",
                seed=0,
                clocks=["updates"],
                directory=source.root,
            ) as writer:
                writer.log({"loss": 3}, updates=151)
                writer.log({"loss": 2}, updates=200)
        get = source.get

        def interrupted(key: str) -> bytes:
            if key == child + "/outcome.json":
                raise FileNotFoundError(key)
            return get(key)

        with monkeypatch.context() as patch:
            patch.setattr(source, "get", interrupted)
            view.refresh_files()
            check(view, "setting up")

    with Server(measured, None, ("127.0.0.1", 0)) as reopened:
        check(reopened, "setting up")
        # Metrics and their writer can arrive before the committing outcome.
        outcome = source.root / child / "outcome.json"
        pending = outcome.rename(tmp_path / "child-outcome.json")
        reopened.refresh_files()
        # An older declaration can lack lineage. The selected launcher supplies it.
        declaration = source.root / child / "run.json"
        portable = RunRecord.model_validate_json(declaration.read_bytes())
        declaration.write_text(portable.model_copy(update={"continues": None}).model_dump_json())
        reopened.refresh_files()
        check(reopened, "running")
        endpoint = home() / "server"
        endpoint.write_text("other")
        assert "Continued from" not in _get(reopened, child_page)
        assert "Continued from" not in _get(reopened, "/experiments/paced/status")
        endpoint.unlink()
        declaration.write_text(portable.model_dump_json())
        reopened.refresh_files()
        active = check(reopened, "running")
        (trace,) = _page_data(active).training.series["updates"]["loss"]
        assert trace.x == [151, 200] and trace.mean == [3, 2]
        pending.rename(outcome)
        reopened.refresh_files()
        assert "updates 200 / 200" in check(reopened, "finished")
        backend.run(601)
        reopened.refresh_launcher()
        assert "2 finished" in _get(reopened, "/status").split("<h2>Completed")[1]
    backend.store.close()

    archive = DirectoryStorage(tmp_path / "archive")
    assert sync(source.root, storages=[archive], stopped=True) == {}
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "reader"))
    with Server(archive, None, ("127.0.0.1", 0)) as reader:
        _ingest(reader)
        assert "2 finished" in _get(reader, "/status")
        assert _loss(_get(reader, parent_page), "updates") == [7]
        child_graph = _get(reader, child_page)
        assert _loss(child_graph, "updates") == [3, 2] and handoff in child_graph
        assert handoff in _get(reader, "/experiments/paced/status")
        report = _get(reader, "/experiments/paced/report")
        (group,) = _page_data(report).training.jobs
        assert set(group.runs) == {"talk@0", child.split("/")[1]} and group.label == "talk · 1 seed"
        (archive.root / parent).rename(tmp_path / "absent-parent")
        reader.refresh_files()
        for page in (child_page, "/experiments/paced/status"):
            html = _get(reader, page)
            assert f"Continued from {parent} at updates 150" in html and parent_page not in html


def test_experiment_detail_links_and_polls_read_only_the_selected_indexed_run(
    tmp_path: Path,
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    file = experiment_file(
        tmp_path / "experiment files",
        "lab",
        """
from etude.report import Report

def report() -> Report:
    return Report(other_metrics=True)
""",
    )
    for seed in (0, 2):
        with open_run(
            experiment="lab",
            run=f"base@{seed}",
            config={"label": f"seed-{seed}"},
            clocks=["steps"],
            directory=source.root,
        ) as run:
            run.log({"loss": 8 - seed}, steps=3)
            run.log({"loss": 6 - seed}, steps=6)
        checkpoint = CheckpointRecord(
            ordinal=1, at={"steps": 6}, wall_ms=1000, artifact=ArtifactRef(uri=f"seed-{seed}.pt")
        )
        source.put(f"lab/base@{seed}/checkpoints/first.json", checkpoint.model_dump_json().encode())
    backend = bench(home())
    (queued,) = backend.scheduler.queue(job(name="queued", experiment="lab"), rent="l4")
    measured = MeasuredStorage(source)
    base = "/experiments/lab"
    with Server(measured, file, ("127.0.0.1", 0)) as view:
        _ingest(view)
        _publish_declarations(view)
        reads = measured.listings, measured.requests, measured.bytes
        for route in (base, base + "/status"):
            html = _get(view, route + "?run=base@2&tab=config")
            table, detail = html.split('<aside id="run-detail" class="run-detail">')
            assert table.count('<a title="lab/') == 3
            assert all(
                f"<th>{col}</th>" in table
                for col in ("Run", "State", "Clock", "Latest score", "Machine", "Dollars")
            )
            assert ">loss 4</td>" in table and ">loss 6</td>" in table
            assert 'href="/experiments/lab?run=base@0&amp;tab=config#run-detail"' in table
            assert all(
                f"etude {action} &#x27;{file}&#x27;" in table
                for action in ("launch", "eval", "report")
            )
            assert f"etude cancel {queued}</code>" in table
            assert 'href="/runs/lab/base@2"' in detail and "<h2>Config</h2>" in detail
            assert "seed-2" in detail and "seed-0" not in detail and "seed-2.pt" not in detail
            for tab in ("console", "curves", "config", "checkpoints"):
                assert f'href="{base}?run=base@2&amp;tab={tab}#run-detail"' in detail
        checkpoints = _get(view, base + "/status?run=base@0&tab=checkpoints")
        assert "seed-0.pt" in checkpoints and "seed-2.pt" not in checkpoints
        console = _get(view, base + "/status?run=base@2&tab=console")
        assert "Console was not recorded." in console
        curves = _get(view, base + "/status?run=base@2&tab=curves")
        token = curves.split('data-generation="')[1].split('"')[0]
        assert '"mean": [6.0, 4.0]' in curves and '"mean": [8.0, 6.0]' not in curves
        unchanged = _get(view, base + f"/status?run=base@2&tab=curves&curves={token}")
        assert f'data-generation="{token}"></div>' in unchanged and '"mean"' not in unchanged
        missing = _get(view, base + "/status?run=absent&tab=config")
        assert "selected run is unavailable" in missing and "seed-0" not in missing
        assert (measured.listings, measured.requests, measured.bytes) == reads
    backend.store.close()


def test_experiment_cards_follow_the_first_indexed_ml_panel_without_source_reads(
    tmp_path: Path,
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    file = experiment_file(
        tmp_path,
        "lab",
        """
from etude.report import Report, Series

def report() -> Report:
    return Report(clock="steps", ml=(
        Series(title="Return </script>", keys=("reward",), unit="points", scale="log"),
        Series(title="Loss", keys=("loss",)),
    ))
""",
    )
    for name in ("lab/a", "lab/b", "archive/base"):
        _start_run(source, name, 7)
    outcome = RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode()
    source.put("archive/base/outcome.json", outcome)
    backend = bench(home())
    # The retained read prices each open window through its one-hour deadline.
    backend.clock.now = datetime.now(UTC) - timedelta(hours=2)
    queued = backend.scheduler.queue(
        [job(name="left", experiment="next"), job(name="right", experiment="next")], rent="l4"
    )
    measured = MeasuredStorage(source)

    def cards(view: Server, route: str) -> list[str]:
        reads = measured.listings, measured.requests, measured.bytes
        html = _get(view, route)
        assert (measured.listings, measured.requests, measured.bytes) == reads
        return [
            part.split("</article>", 1)[0]
            for part in html.split('<article class="card experiment-card">')[1:]
        ]

    def curves(card: str) -> list[Headline]:
        encoded = card.split('<script type="application/json">')[1].split("</script>")[0]
        return TypeAdapter(list[Headline]).validate_json(encoded)

    with Server(measured, file, ("127.0.0.1", 0)) as view:
        _ingest(view)
        _publish_declarations(view)
        live, waiting, archived = cards(view, "/")
        assert 'href="/experiments/lab"' in live and 'href="/experiments/archive"' in archived
        assert live.count('data-state="running"') == 2 and "2 running" in live
        assert all(f'href="/runs/lab/{name}"' in live for name in ("a", "b"))
        assert [panel.title for panel in curves(live)] == ["Loss", "Loss"]
        assert 'data-state="finished"' in archived and "last write" in archived
        assert (
            all(f'href="/runs/{address}"' in waiting for address in queued)
            and "2 queued" in waiting
        )
        assert "no files yet" in waiting and "$0.00" in waiting and "card-charts" not in waiting
        assert "$" not in archived

        route = "/experiments/lab/status?run=b&tab=curves"
        initial = _get(view, route)
        token = initial.split('data-generation="')[1].split('"')[0]
        assert initial.count(">loss 7</td>") == 2
        builder = Builder(["steps"])
        builder.add(1002, {"steps": 2}, {"reward": 11, "loss": 4})
        write_block(
            source.root / "lab/b", tail_key(1, "123456abcdef"), builder.columns("123456abcdef", 1)
        )
        view.refresh_files()
        backend.scheduler.tick()
        backend.run(1)
        backend.run(120)
        view.refresh_launcher()
        changed, allocated, still_archived = cards(view, "/status")
        assert "~$1.18" in allocated and allocated.count('data-state="running"') == 2
        (panel,) = curves(changed)
        assert (panel.title, panel.clock, panel.unit, panel.log) == (
            "Return </script>",
            "steps",
            "points",
            True,
        )
        assert list(panel.curves) == ["reward"]
        reward = panel.curves["reward"]
        assert (reward.owner, reward.x, reward.mean) == ("b", [2], [11])
        assert "2 running" in changed and "Loss" not in changed
        assert curves(still_archived) == curves(archived)
        before = measured.listings, measured.requests, measured.bytes
        updated = _get(view, route + "&curves=" + token)
        assert ">reward 11 points</td>" in updated and ">loss 7</td>" not in updated
        assert f'data-generation="{token}"' not in updated and '"mean": [11.0]' in updated
        assert (measured.listings, measured.requests, measured.bytes) == before
    backend.store.close()


def test_card_keeps_one_report_panel_while_a_run_file_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    for name in ("a", "b"):
        _start_run(source, f"lab/{name}", 7)
    file = experiment_file(
        tmp_path,
        "lab",
        """
from etude.report import Report, Series

def report() -> Report:
    return Report(ml=(Series(title="Previous panel", keys=("loss",)),))
""",
    )
    with Server(source, file, ("127.0.0.1", 0)) as view:
        _ingest(view)
        _publish_declarations(view)
        assert "Previous panel" in _get(view, "/status")
        tail = "lab/a/" + _seal(source.root / "lab/a", 1, 4)
        get = source.get

        def missing(key: str) -> bytes:
            if key == tail:
                raise FileNotFoundError(key)
            return get(key)

        with monkeypatch.context() as patch:
            patch.setattr(source, "get", missing)
            file.write_text(file.read_text().replace("Previous panel", "Edited panel"))
            _publish_declarations(view)
            _publish_declarations(view)
            page = _get(view, "/status")
            assert "Edited panel" in page and "Previous panel" not in page
            assert '"owner":"b"' in page and '"owner":"a"' not in page and "2 running" in page
        view.refresh_files()
        assert '"owner":"a"' in _get(view, "/status")


def test_reports_index_and_poll_show_the_last_served_build_without_source_reads(
    tmp_path: Path, repository: tuple[Path, Path]
) -> None:
    repo, _ = repository
    file = experiment_file(
        repo,
        "lab",
        "from etude.report import Report, Series\n"
        "def report() -> Report:\n"
        "    return Report(ml=(Series(title='Return', keys=('reward',), unit='pts'),))\n",
    )
    git(repo, "add", file.name)
    git(repo, "commit", "-m", "Declare the report")
    commit = git(repo, "rev-parse", "--short=12", "HEAD")
    experiment_file(repo, "unused", COUNTED)
    source = DirectoryStorage(tmp_path / "runs")
    for seed, reward in enumerate((3, 12)):
        with open_run(
            experiment="lab", run=f"base@{seed}", clocks=["steps"], directory=source.root
        ) as run:
            run.log({"reward": 90}, steps=1)
            run.log({"reward": reward}, steps=2)
    _start_run(source, "active/base", 2)
    measured = MeasuredStorage(source)
    with Server(measured, repo, ("127.0.0.1", 0)) as view:
        _ingest(view)

        def rows() -> dict[str, str]:
            reads = measured.listings, measured.requests, measured.bytes
            indexed: dict[str, str] = {}
            for route in ("/reports", "/reports/status"):
                table = _get(view, route).split("<tbody>")[1].split("</tbody>")[0]
                found = {
                    row.split('href="/experiments/')[1].split('/report">')[0]: row
                    for row in table.split("</tr>")
                    if row
                }
                assert len(found) == table.count("<tr>") == 3
                assert set(found) == {"active", "lab", "unused"}
                if indexed:
                    assert found == indexed
                indexed = found
            assert (measured.listings, measured.requests, measured.bytes) == reads
            return indexed

        cold = rows()
        assert "<td>Never</td>" in cold["unused"]
        assert "<td>Open</td><td>Never</td><td>Default</td>" in cold["active"]
        assert "<td>Completed</td><td>Never</td>" in cold["lab"]
        route = "/experiments/lab/report"
        assert "base@1" in _get(view, route)
        built = rows()["lab"]
        stamp = datetime.fromisoformat(built.split('<time datetime="')[1].split('"')[0])
        assert f"{stamp:%Y-%m-%d %H:%M:%S UTC}</time>" in built
        assert f"<td>{commit}</td><td>reward 12 pts</td><td>2</td>" in built
        assert "Refresh report" in _get(view, "/experiments/active/report")
        assert "<td>Default</td><td>loss 2</td><td>1</td>" in rows()["active"]

        # A newly indexed run changes live state. Only a successful report refresh
        # replaces the revision, score and membership shown beside that state.
        _start_run(source, "lab/next", 5)
        view.refresh_files()
        opened = built.replace("<td>Completed</td>", "<td>Open</td>")
        assert rows()["lab"] == opened
        file.write_text("raise ValueError('broken report')\n")
        with pytest.raises(ValueError, match="broken report"):
            view.page(route, refresh=True)
        assert rows()["lab"] == opened
        file.write_text(COUNTED.replace("return Report()", "return Report(other_metrics=True)"))
        view.page(route, refresh=True)
        refreshed = rows()["lab"]
        assert "<td>Working tree</td>" in refreshed and refreshed.endswith("<td>3</td>")


def test_side_rail_follows_open_experiments_and_held_rentals_on_every_page(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    _start_run(source, "local/base", 7)
    _start_run(source, "archive/base", 3)
    finished = RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode()
    source.put("archive/base/outcome.json", finished)
    backend = bench(home())
    (active,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
    backend.scheduler.tick()
    backend.run(1)
    busy = backend.scheduler.read(active).machine
    assert busy
    idle = backend.scheduler.rent("l4", hours=1)
    (queued,) = backend.scheduler.queue(job(name="wait", experiment="next"), rent="l4")
    (home() / "server-heartbeat").write_text((datetime.now(UTC) - timedelta(hours=2)).isoformat())
    measured = MeasuredStorage(source)

    def rail(html: str) -> str:
        assert html.count('<nav id="view-rail"') == 1
        return html.split('<nav id="view-rail"', 1)[1].split("</nav>", 1)[0]

    with Server(measured, None, ("127.0.0.1", 0)) as view:
        _ingest(view)
        reads = measured.listings, measured.requests, measured.bytes
        reports = _get(view, "/reports")
        assert 'href="/experiments/archive/report"' in reports
        assert (measured.listings, measured.requests, measured.bytes) == reads
        # Building a report is explicit. Its frame and standalone page share that build.
        assert '<nav id="view-rail"' not in _get(view, "/experiments/local/report?embed=1")
        reads = measured.listings, measured.requests, measured.bytes
        for page, section in (
            ("/", "/"),
            ("/machines", "/machines"),
            ("/reports", "/reports"),
            ("/experiments/lab", "/"),
            ("/runs/local/base", "/"),
            ("/runs/" + queued, "/"),
            ("/experiments/local/report", "/reports"),
            ("/experiments/next/report", "/reports"),
        ):
            nav = rail(_get(view, page))
            assert all(f'href="{url}"' in nav for url in ("/", "/machines", "/reports"))
            assert f'href="{section}" aria-current="location"' in nav
            assert 'title="running"></i>local</a>' in nav
            assert 'title="queued"></i>next</a>' in nav
            assert all(f'href="/experiments/{name}"' in nav for name in ("local", "lab", "next"))
            assert "archive" not in nav
            assert f'href="/machines#machine-{idle}">{idle} <span class="idle">idle</span>' in nav
            assert f'href="/machines#machine-{busy}">{busy}</a>' in nav
            assert "$1.18 an hour" in nav and "last loop 2 h ago" in nav
        assert 'href="/experiments/next"' in rail(_get(view, "/runs/missing", status=404))
        assert f'id="machine-{busy}"' in _get(view, "/machines")
        assert (measured.listings, measured.requests, measured.bytes) == reads

        version = _version(_get(view, "/runs/" + active))
        source.put("local/base/outcome.json", finished)
        view.refresh_files()
        reads = measured.listings, measured.requests, measured.bytes
        observed = _get(view, "/runs/" + active + "?observe=1")
        assert _version(observed) == version
        for html in (
            observed,
            _get(view, "/status"),
            _get(view, "/experiments/local/report?observe=1"),
        ):
            assert 'href="/experiments/local"' not in rail(html)
            assert 'href="/experiments/lab"' in rail(html)
        assert (measured.listings, measured.requests, measured.bytes) == reads
    backend.store.close()


def test_machines_link_allocated_runs_and_refresh_their_sealed_clocks(tmp_path: Path) -> None:
    backend = bench(home(), slots=2)
    backend.scheduler.add_machine("bench", "bench")
    spec = job(name="learn", experiment="lab", until="steps=40")
    first, second = backend.scheduler.queue(
        [RunRequest(id=f"learn@{seed}", seed=seed, job=spec) for seed in (0, 1)], rent="l4"
    )
    backend.scheduler.tick()
    _reporting(backend, clocks={"steps": 2})
    backend.run(1)
    machine = backend.scheduler.read(first).machine
    assert machine and backend.scheduler.read(second).machine == machine
    idle = backend.scheduler.rent("l4", hours=1)
    (queued,) = backend.scheduler.queue(RunRequest(id="learn@2", seed=2, job=spec), rent="l4")
    with Counting(backend.store, backend.service).record() as count:
        fleet = backend.scheduler.machine_statuses()
    assert count.snapshot() == Counts(database=7)
    rentals = {row.machine.name: row.runs for row in fleet if row.cost}
    assert rentals == {machine: (first, second), idle: ()}
    source = DirectoryStorage(tmp_path / "runs")
    _start_run(source, first, 8, job="learn", seed=0)
    measured = MeasuredStorage(source)
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        reads = measured.listings, measured.requests, measured.bytes
        assert 'href="/machines"' in (server.page("/") or "")
        html = server.page("/machines") or ""
        statements: list[str] = []
        with server.index._connect() as db:
            db.set_trace_callback(statements.append)
            slots = _machines_table(db, fleet)
        assert len(statements) == 1
        assert all(f'href="/runs/{address}"' in slots for address in (first, second))
        row = next(r for r in html.split("</tr>") if f">{machine}</th>" in r)
        assert "rented until" in row and "~$" in row and ">l4</td>" in row
        assert all(f'href="/runs/{address}"' in row for address in (first, second))
        assert "ready," in row and machine not in row.rsplit("</th>", 1)[1]
        assert "running" in row and "steps 1 / 40" in row and queued not in html
        assert "Idle" in html and "never expires" in html
        assert backend.scheduler.read(first).clocks == {"steps": 2}
        header = _get(server, f"/runs/{first}?tab=console").split("</header>", 1)[1]
        line = header.split("<p>", 1)[1].split("</p>", 1)[0]
        assert line.count("running") == line.count("steps") == 1
        assert "steps 1 / 40" in line and machine in line
        card = server.page("/status") or ""
        assert f"{first} · running · steps 1 / 40 · {machine}" in card
        detail = server.page("/experiments/lab?run=learn@0&tab=console") or ""
        assert line in detail
        assert all("last loop" not in p and "launcher observed" not in p for p in (card, detail))
        assert "steps 2 / 40" in (server.page("/runs/" + second) or "")
        assert 'data-refresh="5000"' in html
        assert (measured.listings, measured.requests, measured.bytes) == reads

        _seal(source.root / first, 1, 4)
        _ingest(server)
        reads = measured.listings, measured.requests, measured.bytes
        assert "steps 2 / 40" in (server.page("/machines/status") or "")
        assert (measured.listings, measured.requests, measured.bytes) == reads
        backend.scheduler.cancel(queued)
        backend.run(600)
        _ingest(server)
        settled = server.page("/machines/status") or ""
        assert machine not in settled and first not in settled and "Idle" in settled
    backend.store.close()


def test_run_console_opens_on_the_last_500_lines_of_retained_output(tmp_path: Path) -> None:
    backend = bench(home())
    (run,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
    backend.scheduler.tick()
    backend.run(1)
    (attempt,) = backend.scheduler.attempts(run)
    output = "".join(f"line <{n}>\n" for n in range(1, 601)).encode()
    backend.store.retain_log(snapshot_stream(attempt.session, attempt.id, "job", output, False))
    # This is the payload a selected server sends in its batched response.
    wire = LauncherObservation.read((run,)).model_dump_json()
    assert wire.count("line <") == 499 and "line <600>" in wire
    source = DirectoryStorage(tmp_path / "runs")
    source.put(f"{run}/run.json", RunRecord(id=run, experiment="lab").model_dump_json().encode())
    with Server(source, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        detail = f"/experiments/lab/status?run={run}&tab=console"
        assert "Waiting for retained console" in _get(server, detail)
        _ingest(server)
        page = server.page("/runs/" + run) or ""
        pane = _get(server, detail)
        console = page.split('aria-label="Retained console">', 1)[1].split("</pre>", 1)[0]
        assert console in pane and f"etude logs --run {run}" in pane
        lines = unescape(console).splitlines()
        assert len(lines) == 500
        assert lines[0] == "line <102>" and lines[-2] == "line <600>"
        assert lines[-1].startswith("[snapshot incomplete")
        assert "Some console output is missing" in page and f"etude logs --run {run}" in page
        assert "line <1>\n" in backend.scheduler.logs(run=run).streams[-1].text
        for section in ("console", "config", "checkpoints"):
            assert f'href="?tab={section}"' in page
            assert f'id="{section}"' in _get(server, f"/runs/{run}?tab={section}")
    backend.store.close()


def test_incomplete_directory_listing_keeps_file_facts_until_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    address = "lab/base@0"
    _start_run(source, address, 7)
    source.put("readable/note", b"another subtree")
    measured = MeasuredStorage(source)
    backend = bench(home())
    scandir = os.scandir

    def deny_lab(path: str | Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if path in (source.root / "lab", str(source.root / "lab")):
            raise PermissionError("cannot traverse lab")
        return scandir(path)

    def check_outage(server: Server) -> None:
        reads = measured.listings, measured.requests, measured.bytes
        graph = _get(server, "/runs/" + address)
        assert ">running<" in graph and "steps 1" in graph and _loss(graph) == [7]
        status = _get(server, "/experiments/lab/status")
        assert address in status and ">running<" in status and "steps 1" in status
        overview = _get(server, "/status")
        opened, _ = overview.split("<h2>Completed</h2>")
        assert address in opened and queued in opened
        assert "files observed 11 min ago" in graph
        for page in (status, overview):
            assert "Run files listed 11 min ago" in page
        for page in (graph, status, overview):
            assert "Run files: PermissionError: cannot traverse lab" in page
        assert (measured.listings, measured.requests, measured.bytes) == reads

    with Server(measured, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        report_path = "/experiments/lab/report?embed=1"
        original = _get(server, report_path)
        monkeypatch.setattr(os, "scandir", deny_lab)
        _advance_view_clock(monkeypatch, minutes=11)
        server.refresh_files()
        (queued,) = backend.scheduler.queue(job(name="learn", experiment="other"), rent="l4")
        backend.store.close()
        server.refresh_launcher()
        check_outage(server)
        with pytest.raises(PermissionError, match="cannot traverse lab"):
            server.page(report_path, refresh=True)
        assert _get(server, report_path) == original

    with Server(measured, None, ("127.0.0.1", 0)) as reopened:
        check_outage(reopened)
        reopened.refresh_files()
        check_outage(reopened)
        monkeypatch.setattr(os, "scandir", scandir)
        reopened.refresh_files()
        graph = _get(reopened, "/runs/" + address)
        assert ">no end recorded<" in graph and "cannot traverse lab" not in graph
        assert "lab" in _get(reopened, "/status").split("<h2>Completed</h2>")[1]
        block = _seal(source.root / address, 1, 3)
        stamp = (datetime.now(UTC) + timedelta(minutes=11)).timestamp()
        os.utime(source.root / address / block, (stamp, stamp))
        reopened.refresh_files()
        graph = _get(reopened, "/runs/" + address)
        assert ">running<" in graph and "steps 2" in graph and _loss(graph) == [7, 3]
        for key in source.keys(""):
            source.delete(key)
        reopened.refresh_files()
        assert reopened.page("/runs/" + address) is None
        assert queued in _get(reopened, "/status")


def test_slow_listing_dates_silence_from_when_the_listing_began(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    _start_run(source, "lab/base@0", 7)
    versions = source.versions

    def delayed(prefix: str) -> dict[str, str]:
        listed = versions(prefix)
        _advance_view_clock(monkeypatch, minutes=11)
        return listed

    monkeypatch.setattr(source, "versions", delayed)
    with Server(source, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        graph = _get(server, "/runs/lab/base@0")
        assert "files observed 11 min ago" in graph and ">running<" in graph


@pytest.mark.parametrize("warm", [True, False], ids=["reopened", "first-start"])
def test_failed_listing_shows_fresh_queue_and_machines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm: bool
) -> None:
    backend = bench(home())
    source = DirectoryStorage(tmp_path / "runs")
    _start_run(source, "lab/compact", 7, job="base", seed=0)
    source.put(
        "lab/compact/outcome.json",
        RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode(),
    )
    measured = MeasuredStorage(source)
    declaration = experiment_file(tmp_path, "unstarted", COUNTED)
    if warm:
        with Server(measured, declaration, ("127.0.0.1", 0)) as first:
            _ingest(first)
            assert _loss(first.page("/runs/lab/compact") or "") == [7]

    def unavailable(prefix: str) -> dict[str, str]:
        raise OSError("fixture listing unavailable")

    _advance_view_clock(monkeypatch, minutes=2)
    (alias,) = backend.scheduler.queue(job(name="base", experiment="lab"), rent="l4")
    (queued,) = backend.scheduler.queue(
        job(name="learn", experiment="lab", config='{"rate":3}'), rent="l4"
    )
    backend.scheduler.add_machine("studio", "studio")
    backend.store.close()
    with monkeypatch.context() as patch:
        patch.setattr(source, "versions", unavailable)
        with Server(measured, declaration, ("127.0.0.1", 0)) as server:
            _ingest(server)
            reads = measured.listings, measured.requests
            supplied = unescape(_get(server, "/runs/" + queued + "?tab=config"))
            assert ">queued<" in supplied and '{"rate":3}' in supplied
            for path in ("/", "/experiments/lab/status"):
                page = _get(server, path)
                assert queued in page and "fixture listing unavailable" in page
                assert (
                    "Run files listed 2 min ago" if warm else "File observation unavailable"
                ) in page
            assert ">studio</th>" in (server.page("/machines") or "")
            graph = _get(server, "/runs/" + alias)
            if warm:
                assert ">finished<" in graph and "steps 1" in graph and _loss(graph) == [7]
                status = _get(server, f"/experiments/lab/status?run={alias}&tab=console")
                assert 'href="/runs/lab/compact"' in status
                assert (
                    'href="/experiments/lab?run=compact&amp;tab=console#run-detail"' in status
                    and f'title="{alias}"' not in status
                )
            else:
                assert ">queued<" in graph and "File observation unavailable" in graph
                assert "Config details appear once" in _get(
                    server, "/runs/" + alias + "?tab=config"
                )
                for detail in ("Curves", "Checkpoints"):
                    assert f"{detail} appear once the view has read the run files." in graph
                assert (
                    "No metric curves recorded" not in graph
                    and "No checkpoints recorded" not in graph
                )
                assert "Waiting for run files" in _get(server, "/experiments/lab/report")
                assert "Waiting for run files" in _get(server, "/experiments/unstarted/report")
                assert "No config recorded" not in graph
                assert "Waiting for the first file observation." in _get(
                    server, "/experiments/unstarted/status"
                )
            assert (measured.listings, measured.requests) == reads
    with Server(measured, declaration, ("127.0.0.1", 0)) as recovered:
        overview = recovered.page("/") or ""
        assert queued in overview and "fixture listing unavailable" in overview
        _seal(source.root / "lab/compact", 1, 3)
        _ingest(recovered)
        graph = recovered.page("/runs/" + alias) or ""
        assert ">finished<" in graph and _loss(graph) == [7, 3] and "steps 2" in graph
        assert queued in (recovered.page("/") or "") and "fixture listing unavailable" not in graph
        for key in source.keys(""):
            source.delete(key)
        _ingest(recovered)
        assert recovered.page("/runs/lab/compact") is None
        assert "No config recorded." in _get(recovered, "/runs/" + alias + "?tab=config")
        assert "No runs recorded." in _get(recovered, "/experiments/unstarted/status")
        assert ">queued<" in (recovered.page("/runs/" + alias) or "")
        assert ">studio</th>" in (recovered.page("/machines/status") or "")


@pytest.mark.parametrize("held_at", ["listing", "new block"])
def test_held_file_pass_publishes_launcher_and_joins_latest_placement_on_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_at: str
) -> None:
    backend = bench(home())
    source = DirectoryStorage(tmp_path / "runs")
    address = "lab/learn@0"
    _start_run(source, address, 7, job="learn", seed=0)
    measured = MeasuredStorage(source)
    entered, release = threading.Event(), threading.Event()

    read = LauncherObservation.read
    console_tails = 0

    def count_tails(consoles: tuple[str, ...], *, machines: bool) -> LauncherObservation:
        nonlocal console_tails
        observation = read(consoles, machines=machines)
        console_tails += len(observation.consoles)
        return observation

    monkeypatch.setattr(LauncherObservation, "read", staticmethod(count_tails))

    def hold() -> None:
        entered.set()
        if not release.wait(10):
            raise TimeoutError("fixture file pass was not released")

    with Server(measured, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        tail = address + "/" + _seal(source.root / address, 1, 3)
        before = measured.listings
        with Server(measured, None, ("127.0.0.1", 0)) as other, ThreadPoolExecutor(1) as pool:
            if held_at == "listing":
                versions = source.versions

                def listing(prefix: str) -> dict[str, str]:
                    hold()
                    return versions(prefix)

                monkeypatch.setattr(source, "versions", listing)
            else:
                get = source.get

                def block(key: str) -> bytes:
                    if key == tail:
                        hold()
                    return get(key)

                monkeypatch.setattr(source, "get", block)
            held = pool.submit(server.refresh_files)
            try:
                assert entered.wait(5)
                (queued,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
                assert queued == address
                backend.scheduler.tick()
                _reporting(backend, log_bytes=100)
                backend.run(1)
                machine = backend.scheduler.read(address).machine
                assert machine
                reads = measured.requests
                server.refresh_launcher()
                graph = _get(server, "/runs/" + address)
                assert machine in graph and "steps 1" in graph and _loss(graph) == [7]
                _ingest(other)
                assert address in _get(other, "/machines/status")
                server.refresh_launcher()
                assert "log of " in _get(server, "/runs/" + address)
                backend.run(700)
                server.refresh_launcher()
                _get(server, "/runs/" + address + "?observe=1")
                server.refresh_launcher()
                settled = _get(server, "/runs/" + address)
                assert (
                    ">finished<" in settled
                    and "snapshot complete" in settled
                    and _loss(settled) == [7]
                )
                status = _get(server, "/experiments/lab/status")
                overview = _get(server, "/status")
                assert "steps 1" in status and "1 finished" in overview
                assert all('data-refresh="5000"' in page for page in (settled, status, overview))
                tails_at_finish = console_tails
                assert tails_at_finish > 0
                _advance_view_clock(monkeypatch, minutes=2)
                for _ in range(3):
                    check = _get(server, "/runs/" + address + "?observe=1")
                    assert _version(check) == _version(settled) and len(check.encode()) < 1024
                    assert "files observed 2 min ago" in check and "Last collected" in check
                    server.refresh_launcher()
                assert console_tails == tails_at_finish
                (next_run,) = backend.scheduler.queue(job(name="next", experiment="lab"), rent="l4")
                server.refresh_launcher()
                assert next_run in _get(server, "/status")
                assert measured.requests == reads
                assert measured.listings == before + 1
            finally:
                release.set()
            held.result(5)
        graph = server.page("/runs/" + address) or ""
        assert machine in graph and "steps 2" in graph and _loss(graph) == [7, 3]
        check = _get(server, "/runs/" + address + "?observe=1")
        assert _version(settled) != _version(check) == _version(graph)
        assert "steps 2" in _get(server, "/experiments/lab/status")
    backend.store.close()


def test_failed_launcher_read_after_restart_preserves_queue_while_files_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = bench(home())
    backend.scheduler.add_machine("studio", "studio")
    (queued,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
    backend.store.close()
    source = DirectoryStorage(tmp_path / "runs")
    _start_run(source, "local", 7)
    with Server(source, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        assert ">queued<" in (server.page("/runs/" + queued) or "")
        assert _loss(server.page("/runs/local") or "") == [7]
        assert ">studio</th>" in (server.page("/machines") or "")

    def incompatible(path: Path) -> Database:
        raise LauncherError("fixture needs etude database upgrade")

    _advance_view_clock(monkeypatch, minutes=2)
    monkeypatch.setattr("etude.launch.server.open_home", incompatible)
    _seal(source.root / "local", 1, 3)
    with Server(source, None, ("127.0.0.1", 0)) as restarted:
        _ingest(restarted)
        listing = restarted.page("/machines/status") or ""
        assert ">studio</th>" in listing and "launcher observed 2 min ago" in listing
        assert "etude database upgrade" in listing
        queued_page = restarted.page("/runs/" + queued) or ""
        assert ">queued<" in queued_page and "launcher observed 2 min ago" in queued_page
        assert _loss(restarted.page("/runs/local") or "") == [7, 3]
        assert "steps 2" in (restarted.page("/experiments/local/status") or "")
        # An endpoint with no launcher database returns a successful, empty observation.
        monkeypatch.setattr(
            "etude.launch.server.LauncherObservation.read",
            lambda consoles, *, machines: LauncherObservation(),
        )
        _ingest(restarted)
        assert restarted.page("/runs/" + queued) is None
        assert ">studio</th>" not in (restarted.page("/machines/status") or "")
        assert "etude database upgrade" not in (restarted.page("/status") or "")


def test_outstanding_ssh_keeps_files_moving_and_never_crosses_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = bench(home())
    (queued,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
    held = LauncherObservation.read((queued,))
    backend.scheduler.tick()
    _reporting(backend, log_bytes=100)
    backend.run(1)
    second = LauncherObservation.read((queued,))
    backend.store.close()
    source = DirectoryStorage(tmp_path / "runs")
    _start_run(source, "local", 7)
    measured = MeasuredStorage(source)
    endpoint = home() / "server"
    endpoint.write_text("first")
    monkeypatch.delenv("ETUDE_SERVER_LOCAL", raising=False)
    started, release, ended = threading.Event(), threading.Event(), threading.Event()
    exchanges = 0
    elapsed = 0
    monkeypatch.setattr("etude.view.time", SimpleNamespace(monotonic=lambda: elapsed))

    def exchange(client: PythonClient, consoles: tuple[str, ...]) -> LauncherObservation:
        nonlocal exchanges
        exchanges += 1
        if client.address == "second":
            if not elapsed:
                raise LauncherError("second is offline")
            return second if queued in consoles else second.model_copy(update={"consoles": {}})
        if exchanges == 1:
            return held
        started.set()
        try:
            if not release.wait(5):
                raise TimeoutError("SSH was not released")
            return held
        finally:
            ended.set()

    monkeypatch.setattr(PythonClient, "observation", exchange)
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        _publish_ssh(server)
        assert ">queued<" in (server.page("/runs/" + queued) or "")
        assert 'id="retained-output"' in (server.page("/runs/" + queued) or "")
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        try:
            assert started.wait(5)
            _seal(source.root / "local", 1, 3)
            _ingest(server)
            reads = measured.listings, measured.requests, measured.bytes
            assert _loss(server.page("/runs/local") or "") == [7, 3]
            assert "steps 2" in (server.page("/experiments/local/status") or "")
            assert ">queued<" in (server.page("/runs/" + queued) or "")
            assert (measured.listings, measured.requests, measured.bytes, exchanges) == (*reads, 2)
            assert not ended.is_set()
            report_route = "/experiments/local/report?embed=1"
            cached_report = server.page(report_route)
            assert cached_report
            endpoint.write_text("second")
            _seal(source.root / "local", 2, 1)

            def file_pages(view: Server) -> None:
                reads = measured.listings, measured.requests, measured.bytes
                graph = view.page("/runs/local") or ""
                assert _loss(graph) == [7, 3] and "steps 2" in graph
                assert "Launcher observation unavailable" in graph and "files observed " in graph
                assert 'data-src="/experiments/local/report?embed=1"' in (
                    view.page("/experiments/local") or ""
                )
                status = view.page("/experiments/local/status") or ""
                assert "steps 2" in status and "Launcher observation unavailable" in status
                overview = view.page("/") or ""
                assert 'href="/runs/local"' in overview and queued not in overview
                assert view.page("/experiments/lab/report") is None
                assert view.page("/runs/" + queued) is None
                assert (measured.listings, measured.requests, measured.bytes) == reads

            file_pages(server)
            with Server(measured, None, ("127.0.0.1", 0)) as restarted:
                file_pages(restarted)

            def unavailable(prefix: str) -> dict[str, str]:
                raise OSError("fixture listing unavailable")

            with monkeypatch.context() as patch:
                patch.setattr(source, "versions", unavailable)
                _ingest(server)
                file_pages(server)
                assert server.page(report_route) == cached_report
                assert "fixture listing unavailable" in (server.page("/runs/local") or "")
        finally:
            release.set()
        _publish_ssh(server)
        assert "second is offline" in (server.page("/status") or "")
        assert server.page("/runs/" + queued) is None
        assert _loss(server.page("/runs/local") or "") == [7, 3, 1]
        assert "steps 3" in (server.page("/experiments/local/status") or "")
        assert exchanges == 3
        # A file pass during the outage did not discard the first launcher's dated answer.
        endpoint.write_text("first")
        prior = server.page("/runs/" + queued) or ""
        assert ">queued<" in prior and 'id="retained-output"' in prior
        endpoint.write_text("third")
        assert "second is offline" not in (server.page("/runs/local") or "")
        endpoint.write_text("second")
        elapsed += 30
        _ingest(server)
        _publish_ssh(server)
        page = server.page("/runs/" + queued) or ""
        assert ">running<" in page and "Waiting for retained console" in page
        assert 'id="retained-output"' not in page and "second is offline" not in page
        elapsed += 30
        _ingest(server)
        _publish_ssh(server)
        assert "log of " in (server.page("/runs/" + queued) or "")


def test_machines_without_a_launcher_or_with_a_selected_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "file-only"))
    source = MeasuredStorage(DirectoryStorage(tmp_path / "runs"))
    with Server(source, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        reads = source.listings, source.requests, source.bytes
        absent = server.page("/machines/status") or ""
        assert "No launcher is used here." in absent and 'data-refresh="5000"' in absent
        (home() / "server").write_text("team@fixture")
        monkeypatch.delenv("ETUDE_SERVER_LOCAL", raising=False)
        html = server.page("/machines") or ""
        assert "team@fixture" in html and "<code>etude machines</code>" in html
        assert "Inventory unavailable" in html and "an hour</span>" not in html
        assert "No launcher" not in html and 'data-refresh="5000"' in html
        assert (source.listings, source.requests, source.bytes) == reads


def test_experiments_group_by_activity_and_date_the_latest_write(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    unused = experiment_file(tmp_path, "unstarted", COUNTED)
    measured = MeasuredStorage(source)
    with Server(measured, unused, ("127.0.0.1", 0)) as server:
        assert "Waiting for the first observation." in (server.page("/") or "")
        _ingest(server)
        empty = server.page("/status") or ""
        assert "Waiting for runs." in empty and 'data-refresh="5000"' in empty
        now = datetime.now(UTC).timestamp()
        for name, experiment, age in (
            ("archived/base", "archived", 3 * 86400 + 10),
            ("lab/base", "lab", 130),
            ("lab/earlier", "lab", 7200),
        ):
            key = name + "/run.json"
            source.put(key, RunRecord(id=name, experiment=experiment).model_dump_json().encode())
            os.utime(source.root / key, (now - age, now - age))
        _ingest(server)
        reads = measured.listings, measured.requests, measured.bytes
        page = server.page("/") or ""
        opened, completed = page.split("<h2>Completed</h2>")
        assert 'href="/experiments/lab"' in opened and ">2 min ago<" in opened
        assert 'href="/experiments/archived"' in completed and ">3 d ago<" in completed
        assert "1 running" in opened and all(
            "1 no end recorded" in part for part in (opened, completed)
        )
        assert "unstarted" not in page and not unused.with_suffix(".built").exists()
        assert (measured.listings, measured.requests, measured.bytes) == reads
        source.put(
            "lab/base/outcome.json",
            RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode(),
        )
        _ingest(server)
        reads = measured.listings, measured.requests, measured.bytes
        updated = server.page("/status") or ""
        opened, completed = updated.split("<h2>Completed</h2>")
        assert "No open experiments." in opened and 'href="/experiments/lab"' in completed
        assert "1 finished" in completed and 'data-refresh="5000"' in updated
        assert (measured.listings, measured.requests, measured.bytes) == reads


def test_silent_file_run_reopens_on_the_next_observation(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    key = "lab/base@0/run.json"
    source.put(key, RunRecord(id="lab/base@0", experiment="lab").model_dump_json().encode())
    os.utime(source.root / key, (100, 100))
    with Server(source, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        html = server.page("/runs/lab/base@0") or ""
        assert ">no end recorded<" in html
        assert "Last file write" in html and "at least 10 minutes" in html
        assert _version(_get(server, "/runs/lab/base@0?observe=1")) == _version(html)
        status = server.page("/experiments/lab/status") or ""
        assert (
            ">no end recorded<" in status
            and "10 minutes" in status
            and 'data-refresh="5000"' in status
        )
        _seal(source.root / "lab/base@0", 0, 7)
        server.refresh_files()
        observed = _get(server, "/runs/lab/base@0?observe=1")
        assert _version(observed) != _version(html)
        current = _get(server, "/runs/lab/base@0")
        assert _version(current) == _version(observed) and ">running<" in current


def test_selected_server_status_prints_the_view_running_here(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (home() / "server").write_text("fixture-host")
    monkeypatch.delenv("ETUDE_SERVER_LOCAL", raising=False)

    def forward(address: str, argv: list[str]) -> int:
        print("lab/base@0  base  running")
        return 0

    monkeypatch.setattr("etude.launch.server.forward", forward)
    with advertise("http://localhost:1234"):
        assert main(["status", "base@0", "--experiment", "lab"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "lab/base@0  base  running",
        "view: http://localhost:1234/runs/lab/base@0",
    ]


def test_run_page_serves_the_selected_launchers_retained_console_and_stored_details(
    tmp_path: Path, repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = repository
    remote = tmp_path / "launcher"
    backend = bench(remote)
    spec = job(
        name="base",
        experiment="lab",
        repo=str(repo),
        until="steps=40",
        config='{"queued":"config"}',
        max_attempts=2,
    )
    (identity,) = backend.scheduler.queue(RunRequest(id="base@0", seed=0, job=spec), rent="l4")
    source = MeasuredStorage(DirectoryStorage(tmp_path / "runs"))
    (remote / "server-heartbeat").write_text((datetime.now(UTC) - timedelta(hours=2)).isoformat())
    endpoint = home() / "server"
    endpoint.write_text("fixture")
    monkeypatch.delenv("ETUDE_SERVER_LOCAL", raising=False)
    exchanges = 0
    elapsed = 0
    monkeypatch.setattr("etude.view.time", SimpleNamespace(monotonic=lambda: elapsed))

    def exchange(
        address: str, argv: Sequence[str], *, input: bytes, capture_output: bool
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal exchanges
        exchanges += 1
        return subprocess.run(
            [sys.executable, "-m", "etude", *argv],
            input=input,
            capture_output=capture_output,
            env={**os.environ, "ETUDE_HOME": str(remote), "ETUDE_SERVER_LOCAL": "1"},
            check=True,
        )

    monkeypatch.setattr("etude.launch.server._ssh", exchange)
    with Server(source, None, ("127.0.0.1", 0)) as server:
        _ingest(server)
        _publish_ssh(server)
        route = "/runs/" + identity
        queued = unescape(server.page(route) or "")
        assert ">queued<" in queued and '{"queued":"config"}' in unescape(
            _get(server, route + "?tab=config")
        )
        assert "Target steps 40" in queued and "Waiting for retained console" in queued
        backend.scheduler.tick()
        _reporting(backend, log_bytes=100)
        backend.run(1)
        (attempt,) = backend.scheduler.attempts(identity)
        backend.store.record_hardware(
            backend.store.session(attempt.session),
            GPU(id="NVIDIA L4", min_vcpus=6, min_memory_gb=48),
        )
        weights = tmp_path / "weights"
        weights.write_bytes(b"saved")
        with open_run(
            experiment="lab",
            run="compact",
            job="base",
            seed=0,
            clocks=["steps"],
            config={"label": "<writer>"},
            directory=tmp_path / "runs",
        ) as writer:
            writer.log({"loss": 7}, steps=12)
            writer.checkpoint(weights, {"steps": 12})
        # A courier can deliver sealed metrics and checkpoints ahead of the outcome.
        outcome = tmp_path / "runs/lab/compact/outcome.json"
        pending = outcome.rename(tmp_path / "pending-outcome.json")
        _ingest(server)
        assert exchanges == 1
        assert ">queued<" in (server.page("/runs/lab/compact") or "")
        endpoint.write_text("other")
        assert ">running<" in (server.page("/runs/lab/compact") or "")
        endpoint.write_text("fixture")
        elapsed += 30
        _ingest(server)
        _publish_ssh(server)
        reads = source.requests, source.listings
        page = server.page(route) or ""
        assert f"log of {attempt.id}" in page and "output not received" in page
        assert (
            "Last collected" in page
            and "last loop 2 h ago" in page
            and "launcher observed" not in page
        )
        config = _get(server, route + "?tab=config")
        assert "&lt;writer&gt;" in config and "<writer>" not in config
        assert "steps 12" in page and "<td>1</td>" in page
        assert "steps-12" in page and "steps 12 / 40" in page
        revision = git(repo, "rev-parse", "HEAD")
        assert revision in config and "on main" in config
        assert (
            attempt.machine in page
            and "L4" in page
            and "1 of 2 attempts used" in page
            and "~$0." in page
        )
        assert (source.requests, source.listings, exchanges) == (*reads, 2)
        # The old launcher's spelling is an alias for this compact stored run.
        endpoint.write_text("other")
        assert server.page(route) is None
        assert _version(_get(server, route + "?observe=1")) == "waiting"
        check = _get(server, "/runs/lab/compact?observe=1")
        stored = server.page("/runs/lab/compact") or ""
        assert _version(page) != _version(check) == _version(stored)
        stored_config = _get(server, "/runs/lab/compact?tab=config")
        assert (
            "&lt;writer&gt;" in stored_config
            and revision not in stored_config
            and "steps-12" in stored
        )
        assert "steps 12" in stored and _loss(stored) == [7]
        assert "Launcher observation unavailable" in stored
        assert all(
            old not in stored
            for old in (
                "steps 12 / 40",
                attempt.machine,
                "last loop",
                "of 2 attempts",
                "on main",
                'id="retained-output"',
                revision,
            )
        )
        assert (source.requests, source.listings, exchanges) == (*reads, 2)
        endpoint.write_text("fixture")
        pending.rename(outcome)
        backend.run(700)
        elapsed += 30
        _ingest(server)
        _publish_ssh(server)
        finished = server.page(route) or ""
        assert ">finished<" in finished and "snapshot complete" in finished
        assert _version(_get(server, route + "?observe=1")) == _version(finished) and exchanges == 3
    assert "local" in {pool.name for pool in backend.scheduler.pools()}
    backend.store.close()


def test_evaluation_link_survives_partial_delivery_failed_read_and_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [eval_job(name, None) for name in ("ladder", "gauntlet")]
    group = eval_group(*jobs)
    request = group_evaluations(
        [job.lower(ExperimentState(experiment="grouped")) for job in jobs], group.evaluations
    ).model_copy(update={"max_attempts": 2})
    backend = bench(home())
    (address,) = backend.scheduler.queue(
        RunRequest(id="grouped/evals-test", job=request), rent="l4"
    )
    source = DirectoryStorage(tmp_path / "archive")
    storage = MeasuredStorage(source)
    root = "copy/" + address + "/"
    members = tuple(spec.address for spec in group.evaluations)
    end = RunOutcome(state="finished", counters=RunCounters())
    page = "/runs/" + address

    def check(view: Server, phase: str) -> str:
        before = storage.listings, storage.requests
        html = _get(view, page)
        status = _get(view, "/experiments/grouped/status")
        assert f">{phase}<" in html and f">{phase}<" in status
        assert status.count('href="/runs/') == 1 and f'href="{page}"' in status
        assert all(member not in status for member in members)
        assert (storage.listings, storage.requests) == before
        return html

    def publish_writer(name: str, attempt: str, start: int) -> None:
        writer = RunWriter(
            writer=name, run=address, attempt=attempt, host="fixture", started_ms=start
        )
        source.put(root + "metrics/" + name + ".json", writer.model_dump_json().encode())

    with Server(storage, None, ("127.0.0.1", 0)) as view:
        _ingest(view)
        check(view, "queued")
        backend.scheduler.tick()
        (first,) = backend.scheduler.attempts(address)
        view.refresh_launcher()
        assert _get(view, "/machines/status").count(f'href="{page}"') == 1
        source.put(
            root + "run.json",
            RunRecord(id=address, experiment="grouped", kind="evaluation", members=members)
            .model_dump_json()
            .encode(),
        )
        publish_writer("111111111111", first.id, 100)
        source.put(
            root + members[0] + "/run.json",
            RunRecord(id=members[0], experiment="grouped", kind="evaluation")
            .model_dump_json()
            .encode(),
        )
        source.put(root + members[0] + "/outcome.json", end.model_dump_json().encode())
        source.put(root + members[0] + "/blocks/example/scores.parquet", b"unread")
        listings = storage.listings
        view.refresh_files()
        assert storage.listings == listings + 1 and not any("/blocks/" in k for k in storage.got)
        assert "last file write unknown" not in check(view, "running")
        assert "1 running" in _get(view, "/status")
        source.put(
            root + "outcome.json",
            end.model_copy(update={"writer": "111111111111", "state": "failed"})
            .model_dump_json()
            .encode(),
        )
        view.refresh_files()
        check(view, "failed")
        backend.run(1)
        backend.service.kill(first.session)
        backend.run(1)
        second = backend.scheduler.attempts(address)[1]
        view.refresh_launcher()
        check(view, "setting up")

        # The new allocation and a later member arrive together. A listed member
        # outcome vanishes, so none of this generation can advance the file facts.
        _advance_view_clock(monkeypatch, minutes=2)
        publish_writer("222222222222", second.id, 200)
        source.put(
            root + members[1] + "/run.json",
            RunRecord(id=members[1], experiment="grouped", kind="evaluation")
            .model_dump_json()
            .encode(),
        )
        missing = root + members[1] + "/outcome.json"
        source.put(missing, end.model_dump_json().encode())
        get = source.get

        def interrupted(key: str) -> bytes:
            if key == missing:
                raise FileNotFoundError(key)
            return get(key)

        with monkeypatch.context() as patch:
            patch.setattr(source, "get", interrupted)
            view.refresh_files()
        assert "files observed 2 min ago" in check(view, "setting up")

    with Server(storage, None, ("127.0.0.1", 0)) as restarted:
        assert "files observed 2 min ago" in check(restarted, "setting up")
        _reporting(backend, tally=Tally(unit="matches", done=2, total=2))
        backend.run(1)
        backend.store.retain_log(
            snapshot_stream(second.session, second.id, "job", b"both scores delivered\n", True)
        )
        restarted.refresh_launcher()
        assert "2/2 matches" in check(restarted, "running")
        source.put(
            root + "outcome.json",
            end.model_copy(update={"writer": "222222222222"}).model_dump_json().encode(),
        )
        restarted.refresh_files()
        assert "both scores delivered" in check(restarted, "finished")
        assert "1 finished" in _get(restarted, "/status")
        assert _version(_get(restarted, "/experiments/grouped/report?observe=1")) == "ready"
    backend.store.close()
