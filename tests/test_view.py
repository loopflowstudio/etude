"""What the local view serves while the writer and report keep moving."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta, tzinfo
from html import unescape
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws

from etude.block_file import tail_key
from etude.cli import main
from etude.errors import LauncherError
from etude.home import home
from etude.launch.database import Database
from etude.launch.logs import snapshot_stream
from etude.launch.records import GPU
from etude.launch.server import LauncherObservation, PythonClient
from etude.page import Page, Pages, _run_file
from etude.records import Clock, RunCounters, RunOutcome, RunRecord
from etude.spec import RunRequest
from etude.storage import DirectoryStorage, S3Storage
from etude.train.block import Builder
from etude.train.tracking import write_block
from etude.view import Server
from etude.view_link import advertise, print_view
from tests.experiments import experiment_file
from tests.launch.counting import Counting, Counts
from tests.launch.fake import bench
from tests.launch.test_scheduler import _reporting, job
from tests.launch.test_source import git
from tests.launch.test_source import repository as repository
from tests.run.storage import CountedClient, MeasuredStorage
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
    with Server(measured, file, ("127.0.0.1", 0)) as server:
        assert '"name": "a"' in (server.page("/experiments/lab/report") or "")
        reads = measured.requests, measured.listings
        server.page("/experiments/lab/report")
        assert (measured.requests, measured.listings) == reads
        assert builds.read_text().count("built") == 1
        # Another experiment's run is not this page's input; a run of its own is.
        trained(runs, "other", "d")
        server.page("/experiments/lab/report", refresh=True)
        assert builds.read_text().count("built") == 1
        trained(runs, "lab", "b")
        assert '"name": "b"' not in (server.page("/experiments/lab/report") or "")
        assert '"name": "b"' in (server.page("/experiments/lab/report", refresh=True) or "")
        assert builds.read_text().count("built") == 2
        file.write_text(COUNTED + "# edited\n")
        server.page("/experiments/lab/report", refresh=True)
        assert builds.read_text().count("built") == 3


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


def test_concurrent_refreshes_join_one_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    file = experiment_file(tmp_path, "lab", COUNTED)
    started, release, joined = threading.Event(), threading.Event(), threading.Event()
    build_page = Pages.build

    def blocked_build(self: Pages, path: Path | None, *, experiment: str | None = None) -> Page:
        started.set()
        if not release.wait(5):
            raise TimeoutError("test generation was not released")
        return build_page(self, path, experiment=experiment)

    class Joined(Future[str]):
        def result(self, timeout: float | None = None) -> str:
            # Only the waiter reaches result() while the owner is still building.
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr(Pages, "build", blocked_build)
    monkeypatch.setattr("etude.view.Future", Joined)
    with Server(runs, file, ("127.0.0.1", 0)) as server, ThreadPoolExecutor(2) as pool:
        server.ingest()
        builds = file.with_suffix(".built")
        _publish_declarations(server)
        before = builds.read_text()
        first = pool.submit(server.page, "/experiments/lab/report", refresh=True)
        try:
            assert started.wait(5)
            second = pool.submit(server.page, "/experiments/lab/report", refresh=True)
            assert joined.wait(5)
            assert ">finished<" in (server.page("/experiments/lab") or "")
            assert ">finished<" in (server.page("/runs/lab/a") or "")
        finally:
            release.set()
        assert first.result(5) == second.result(5) == server.page("/experiments/lab/report")
        assert builds.read_text() == before + "built\n"


def test_http_refresh_reports_failures_without_replacing_the_cached_page(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    file = experiment_file(tmp_path, "lab", REPORTED)
    with Server(runs, file, ("127.0.0.1", 0)) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            client.request("GET", "/experiments/lab/report")
            reply = client.getresponse()
            assert reply.status == 200 and reply.getheader("Cache-Control") == "no-store"
            original = reply.read()
            assert b'"refresh": null' in original and b'id="refresh-report"' in original
            client.request("GET", "/runs/absent")
            waiting = client.getresponse()
            assert waiting.status == 404 and runs.as_uri() in waiting.read().decode()
            file.write_text("raise RuntimeError('cannot generate token=fixture-credential')\n")
            client.request("POST", "/experiments/lab/report")
            reply = client.getresponse()
            error = reply.read()
            assert reply.status == 500 and b"cannot generate token=[redacted]" in error
            assert b"fixture-credential" not in error
            client.request("GET", "/experiments/lab/report")
            reply = client.getresponse()
            assert reply.status == 200 and reply.read() == original
            file.write_text(REPORTED.replace("Report()", 'Report(labels={"a": "Readable"})'))
            client.request("POST", "/experiments/lab/report")
            reply = client.getresponse()
            refreshed = reply.read()
            assert reply.status == 200 and b'"Readable"' in refreshed
            client.request("GET", "/experiments/lab/report")
            assert client.getresponse().read() == refreshed
        finally:
            client.close()
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
        first = server.page("/experiments/frontier/report") or ""
        assert all(text in first for text in ('"Width 64"', '"Standard"', '"Frontier only"'))
        reads = source.requests
        server.page("/experiments/frontier/report", refresh=True)
        assert source.requests == reads
        shared.write_text(shared.read_text().replace("Standard", "Edited standard"))
        assert '"Edited standard"' in (
            server.page("/experiments/frontier/report", refresh=True) or ""
        )


def _seal(root: Path, sequence: int, loss: float) -> str:
    writer = "123456abcdef"
    builder = Builder(["steps"])
    builder.add(1000 + sequence, {"steps": float(sequence + 1)}, {"loss": loss})
    name = tail_key(sequence, writer)
    write_block(root, name, builder.columns(writer, sequence))
    return name


def _loss(html: str) -> list[float | None]:
    payload = html.split('<script type="application/json" id="data">')[1].split("</script>")[0]
    page = Page.model_validate_json(payload, extra="ignore")
    return page.training.series["steps"]["loss"][0].mean


def _publish_declarations(server: Server) -> None:
    # Tests drive the real ingestion loop one generation at a time.
    _, pending = wait([reading.future for reading in server.index._reports.values()], timeout=5)
    assert not pending
    server.ingest()


def _publish_ssh(server: Server) -> None:
    if future := server.index._remote_read:
        assert wait((future,), timeout=10).done
        server.ingest()


def test_report_edits_and_a_restart_leave_both_experiments_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    for name in ("lab", "other"):
        address = name + "/base@0"
        record = RunRecord(
            id=address,
            experiment=name,
            clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
        )
        source.put(address + "/run.json", record.model_dump_json().encode())
        _seal(source.root / address, 0, 7)
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
        server.ingest()
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
        server.ingest()
        try:
            assert started.wait(5)
            for name in ("lab", "other"):
                _seal(source.root / name / "base@0", 1, 3)
            server.ingest()
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
        server.ingest()
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
        server.ingest()
        _publish_declarations(server)
        page = server.page("/runs/lab/base@0") or ""
        assert _loss(page) == [7] and "token=[redacted]" in page and "fixture-value" not in page


def test_ingestion_reads_only_the_new_block_and_publishes_its_last_point(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    root = source.root / "lab/base@0"
    record = RunRecord(
        id="lab/base@0",
        experiment="lab",
        clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
    )
    source.put("lab/base@0/run.json", record.model_dump_json().encode())
    _seal(root, 0, 7.0)
    measured = MeasuredStorage(source)
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        server.ingest()
        first = server.page("/runs/lab/base@0") or ""
        assert ">running<" in first and "last write" in first and _loss(first) == [7.0]
        assert "Console was not recorded." in first
        before = measured.requests, measured.listings
        measured.got.clear()
        added = _seal(root, 1, 3.0)
        server.ingest()
        assert measured.got == ["lab/base@0/" + added]
        assert (measured.requests, measured.listings) == (before[0] + 1, before[1] + 1)
        assert _loss(server.page("/runs/lab/base@0") or "") == [7.0, 3.0]


def test_graph_requests_reuse_the_published_generation_after_interruption(tmp_path: Path) -> None:
    storage = DirectoryStorage(tmp_path / "runs")
    root = storage.root / "lab/base@0"
    with open_run(experiment="lab", run="base@0", directory=storage.root, clocks=["steps"]) as run:
        run.log({"loss": 1}, steps=1)
    measured = MeasuredStorage(storage)
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        server.ingest()
        assert _loss(server.page("/runs/lab/base@0") or "") == [1]
    # An unparseable replacement interrupts a cold writer before publication.
    outcome = root / "outcome.json"
    saved = outcome.read_bytes()
    outcome.write_text("{")
    with Server(measured, None, ("127.0.0.1", 0)) as restarted:
        restarted.ingest()
        assert restarted.index.error
        reads = measured.listings, measured.requests, measured.bytes
        page = restarted.page("/runs/lab/base@0") or ""
        assert _loss(page) == [1] and ">finished<" in page and 'role="status"' in page
        status = restarted.page("/experiments/lab/status") or ""
        assert (
            ">finished<" in status and 'role="status"' in status and 'data-refresh="5000"' in status
        )
        overview = restarted.page("/") or ""
        assert "1 finished" in overview and 'data-refresh="5000"' in overview
        assert restarted.index.error in unescape(overview)
        assert (measured.listings, measured.requests, measured.bytes) == reads
        outcome.write_bytes(saved)
        restarted.ingest()
        assert restarted.index.error is None


def test_an_interrupted_writer_never_exposes_half_a_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    for name in ("a", "b"):
        address = "lab/" + name
        source.put(
            address + "/run.json",
            RunRecord(
                id=address,
                experiment="lab",
                clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
            )
            .model_dump_json()
            .encode(),
        )
        _seal(source.root / address, 0, 5)

    measured = MeasuredStorage(source)
    with Server(measured, None, ("127.0.0.1", 0)) as writer:
        writer.ingest()
        _seal(source.root / "lab/a", 1, 4)
        tail = "lab/b/" + _seal(source.root / "lab/b", 1, 3)
        get = source.get
        reader_points: list[float | None] = []
        with monkeypatch.context() as patch:

            def interrupted(key: str) -> bytes:
                if key != tail:
                    return get(key)
                # Chart A has been updated inside the writer's transaction.
                with Server(measured, None, ("127.0.0.1", 0)) as reader:
                    reader.ingest()
                    reader_points.extend(_loss(reader.page("/runs/lab/a") or ""))
                raise OSError("interrupted reading the second chart")

            patch.setattr(source, "get", interrupted)
            before = measured.listings
            writer.ingest()
        assert measured.listings == before + 1
        assert reader_points == [5]
        assert writer.index.error and "interrupted reading the second chart" in writer.index.error
        assert _loss(writer.page("/runs/lab/a") or "") == [5]
        writer.ingest()
        assert _loss(writer.page("/runs/lab/a") or "") == [5, 4]
        assert _loss(writer.page("/runs/lab/b") or "") == [5, 3]


def test_view_links_follow_only_the_live_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    address = home() / "view"
    address.write_text("http://stale.invalid")
    print_view("lab/base@0")
    assert capsys.readouterr().out == ""
    with Server(tmp_path, None, ("127.0.0.1", 0)) as first:
        with Server(tmp_path, None, ("127.0.0.1", 0)) as second:
            first.ingest()
            print_view("lab/base@0")
            assert capsys.readouterr().out.strip() == f"view: {second.origin}/runs/lab/base@0"
        first.ingest()
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
            server.ingest()
            assert ">running<" in (server.page("/runs/lab/base@0") or "")
            reads = client.requests
            bucket.put("lab/base@0/outcome.json", local.get("lab/base@0/outcome.json"))
            server.ingest()
            assert client.requests == reads + 1
            html = server.page("/runs/lab/base@0") or ""
            assert ">finished<" in html and _loss(html) == [2.0]
            server.ingest()
            assert client.requests == reads + 1


def test_two_files_with_one_name_leave_runs_refreshing(tmp_path: Path) -> None:
    folder = tmp_path / "experiments"
    experiment_file(folder, "lab", REPORTED)
    experiment_file(folder / "copy", "lab", REPORTED)
    source = DirectoryStorage(tmp_path / "runs")
    root = source.root / "lab/base@0"
    record = RunRecord(
        id="lab/base@0",
        experiment="lab",
        clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
    )
    source.put("lab/base@0/run.json", record.model_dump_json().encode())
    _seal(root, 0, 7.0)
    with Server(source, folder, ("127.0.0.1", 0)) as server:
        server.ingest()
        _seal(root, 1, 3.0)
        server.ingest()
        assert _loss(server.page("/runs/lab/base@0") or "") == [7.0, 3.0]
        assert "experiment_named_twice" in (server.page("/status") or "")


def test_indexed_experiment_opens_its_default_report_separately(tmp_path: Path) -> None:
    with open_run(experiment="lab", run="base@0", clocks=["steps"], directory=tmp_path) as run:
        run.log({"loss": 8}, steps=1)
    source = MeasuredStorage(DirectoryStorage(tmp_path))
    with Server(source, None, ("127.0.0.1", 0)) as server:
        server.ingest()
        before = (source.requests, source.listings)
        html = server.page("/experiments/lab") or ""
        assert 'src="/experiments/lab/report"' in html and 'href="/runs/lab/base@0"' in html
        assert (source.requests, source.listings) == before
        assert _loss(server.page("/experiments/lab/report") or "") == [8.0]


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
    source.put(
        local + "/run.json",
        RunRecord(
            id=local,
            experiment="lab",
            job_name="baseline",
            clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
        )
        .model_dump_json()
        .encode(),
    )
    _seal(source.root / local, 0, 7)
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
            return next(r for r in html.split("</tr>") if f'href="/runs/{address}"' in r)

        server.ingest()
        html = status("/experiments/lab")
        assert ">running<" in row(html, running) and machine in row(html, running)
        assert ">learn<" in row(html, queued) and ">queued<" in row(html, queued)
        reason = backend.scheduler.read(queued).reason
        assert reason and reason in row(html, queued) and "Target steps 40" in row(html, queued)
        assert ">baseline<" in row(html, local) and ">steps 1<" in row(html, local)
        assert ">score<" in row(html, evaluation) and ">finished<" in row(html, evaluation)
        assert "last loop 2 h ago" in html and 'src="/experiments/lab/report"' in html
        eval_page = status("/runs/" + evaluation)
        assert f"Evaluation · {evaluation}" in eval_page and ">finished<" in eval_page
        assert 'href="/experiments/lab#report">lab report</a>' in eval_page
        assert "last write" in eval_page
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
        server.ingest()
        updated = status("/experiments/lab/status")
        assert ">steps 2<" in row(updated, local) and ">running<" in row(updated, local)
        assert 'data-refresh="5000"' in updated and "<iframe" not in updated
        assert "steps 2" in status("/status")
        source.put(local + "/outcome.json", outcome)
        server.ingest()
        assert ">finished<" in row(status("/experiments/lab/status"), local)
        # A saved terminal outcome can lead the launcher while it releases the attempt.
        source.put(
            running + "/run.json",
            RunRecord(id=running, job_name="learn", seed=0, experiment="lab")
            .model_dump_json()
            .encode(),
        )
        source.put(running + "/outcome.json", outcome)
        server.ingest()
        ending = row(status("/experiments/lab/status"), running)
        assert ">finished<" in ending and ">running<" not in ending and machine in ending
        # The local outcome alone cannot close an experiment with outstanding launcher work.
        assert "3 finished" in status("/status").split("<h2>Completed</h2>")[0]
        backend.scheduler.cancel(queued)
        backend.run(600)
        server.ingest()
        settled = status("/status")
        opened, completed = settled.split("<h2>Completed</h2>")
        assert "No open experiments." in opened and 'href="/experiments/lab"' in completed
        assert "3 finished" in completed and "1 failed" in completed
        assert "data-refresh" not in settled
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
    source.put(
        first + "/run.json",
        RunRecord(
            id=first,
            experiment="lab",
            job_name="learn",
            seed=0,
            clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
        )
        .model_dump_json()
        .encode(),
    )
    _seal(source.root / first, 0, 8)
    measured = MeasuredStorage(source)
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        server.ingest()
        reads = measured.listings, measured.requests, measured.bytes
        assert 'href="/machines"' in (server.page("/") or "")
        html = server.page("/machines") or ""
        row = next(r for r in html.split("</tr>") if f">{machine}</th>" in r)
        assert "rented until" in row and "~$" in row and ">l4</td>" in row
        assert all(f'href="/runs/{address}"' in row for address in (first, second))
        assert "running" in row and "steps 1 / 40" in row and queued not in html
        assert "Idle" in html and "never expires" in html
        assert backend.scheduler.read(first).clocks == {"steps": 2}
        header = (server.page("/runs/" + first) or "").split("</header>", 1)[1]
        line = header.split("<p>", 1)[1].split("</p>", 1)[0]
        assert line.count("running") == line.count("steps") == 1
        assert "steps 1 / 40" in line and machine in line
        for path in ("/", "/experiments/lab", "/machines"):
            page = server.page(path) or ""
            assert line in page and "last loop" not in page and "launcher observed" not in page
        assert "steps 2 / 40" in (server.page("/runs/" + second) or "")
        assert 'data-refresh="5000"' in html
        assert (measured.listings, measured.requests, measured.bytes) == reads

        _seal(source.root / first, 1, 4)
        server.ingest()
        reads = measured.listings, measured.requests, measured.bytes
        assert "steps 2 / 40" in (server.page("/machines/status") or "")
        assert (measured.listings, measured.requests, measured.bytes) == reads
        backend.scheduler.cancel(queued)
        backend.run(600)
        server.ingest()
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
        server.ingest()
        assert "Waiting for retained console" in (server.page("/runs/" + run) or "")
        server.ingest()
        page = server.page("/runs/" + run) or ""
        console = page.split('aria-label="Retained console">', 1)[1].split("</pre>", 1)[0]
        lines = unescape(console).splitlines()
        assert len(lines) == 500
        assert lines[0] == "line <102>" and lines[-2] == "line <600>"
        assert lines[-1].startswith("[snapshot incomplete")
        assert "Some console output is missing" in page and f"etude logs --run {run}" in page
        assert "line <1>\n" in backend.scheduler.logs(run=run).streams[-1].text
        for section in ("console", "config", "checkpoints"):
            assert f'href="#{section}"' in page and f'id="{section}"' in page
    backend.store.close()


def test_failed_launcher_read_after_restart_preserves_queue_while_files_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = bench(home())
    backend.scheduler.add_machine("studio", "studio")
    (queued,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
    backend.store.close()
    source = DirectoryStorage(tmp_path / "runs")
    record = RunRecord(
        id="local",
        experiment="local",
        clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
    )
    source.put("local/run.json", record.model_dump_json().encode())
    _seal(source.root / "local", 0, 7)
    with Server(source, None, ("127.0.0.1", 0)) as server:
        server.ingest()
        assert ">queued<" in (server.page("/runs/" + queued) or "")
        assert _loss(server.page("/runs/local") or "") == [7]
        assert ">studio</th>" in (server.page("/machines") or "")

    def incompatible(path: Path) -> Database:
        raise LauncherError("fixture needs etude database upgrade")

    class Later(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return super().now(tz) + timedelta(minutes=2)

    monkeypatch.setattr("etude.view.datetime", Later)
    monkeypatch.setattr("etude.launch.server.open_home", incompatible)
    _seal(source.root / "local", 1, 3)
    with Server(source, None, ("127.0.0.1", 0)) as restarted:
        restarted.ingest()
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
        restarted.ingest()
        assert restarted.page("/runs/" + queued) is None
        assert ">studio</th>" not in (restarted.page("/machines/status") or "")
        assert "etude database upgrade" not in (restarted.page("/status") or "")


def test_outstanding_ssh_keeps_files_moving_and_never_crosses_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = bench(home())
    (queued,) = backend.scheduler.queue(job(name="learn", experiment="lab"), rent="l4")
    held = LauncherObservation.read((queued,))
    backend.store.close()
    source = DirectoryStorage(tmp_path / "runs")
    record = RunRecord(
        id="local",
        experiment="local",
        clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
    )
    source.put("local/run.json", record.model_dump_json().encode())
    _seal(source.root / "local", 0, 7)
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
            return held.model_copy(update={"consoles": {}})
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
        server.ingest()
        _publish_ssh(server)
        assert ">queued<" in (server.page("/runs/" + queued) or "")
        assert 'id="retained-output"' in (server.page("/runs/" + queued) or "")
    with Server(measured, None, ("127.0.0.1", 0)) as server:
        server.ingest()
        try:
            assert started.wait(5)
            _seal(source.root / "local", 1, 3)
            server.ingest()
            reads = measured.listings, measured.requests, measured.bytes
            assert _loss(server.page("/runs/local") or "") == [7, 3]
            assert "steps 2" in (server.page("/experiments/local/status") or "")
            assert ">queued<" in (server.page("/runs/" + queued) or "")
            assert (measured.listings, measured.requests, measured.bytes, exchanges) == (*reads, 2)
            assert not ended.is_set()
            endpoint.write_text("second")
            # Even before its first file pass the new endpoint cannot serve the old console.
            assert server.page("/runs/" + queued) is None
            server.ingest()
            assert queued not in (server.page("/status") or "")
        finally:
            release.set()
        _publish_ssh(server)
        assert "second is offline" in (server.page("/status") or "")
        assert server.page("/runs/" + queued) is None
        assert _loss(server.page("/runs/local") or "") == [7, 3]
        assert exchanges == 3
        elapsed += 30
        server.ingest()
        _publish_ssh(server)
        page = server.page("/runs/" + queued) or ""
        assert ">queued<" in page and "Waiting for retained console" in page
        assert 'id="retained-output"' not in page and "second is offline" not in page


def test_machines_without_a_launcher_or_with_a_selected_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "file-only"))
    source = MeasuredStorage(DirectoryStorage(tmp_path / "runs"))
    with Server(source, None, ("127.0.0.1", 0)) as server:
        server.ingest()
        reads = source.listings, source.requests, source.bytes
        absent = server.page("/machines/status") or ""
        assert "No launcher is used here." in absent and "data-refresh" not in absent
        (home() / "server").write_text("team@fixture")
        monkeypatch.delenv("ETUDE_SERVER_LOCAL", raising=False)
        html = server.page("/machines") or ""
        assert "team@fixture" in html and "<code>etude machines</code>" in html
        assert "No launcher" not in html and 'data-refresh="5000"' not in html
        assert (source.listings, source.requests, source.bytes) == reads


def test_experiments_group_by_activity_and_date_the_latest_write(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    unused = experiment_file(tmp_path, "unstarted", COUNTED)
    measured = MeasuredStorage(source)
    with Server(measured, unused, ("127.0.0.1", 0)) as server:
        assert "Waiting for the first observation." in (server.page("/") or "")
        server.ingest()
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
        server.ingest()
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
        server.ingest()
        reads = measured.listings, measured.requests, measured.bytes
        updated = server.page("/status") or ""
        opened, completed = updated.split("<h2>Completed</h2>")
        assert "No open experiments." in opened and 'href="/experiments/lab"' in completed
        assert "1 finished" in completed and "data-refresh" not in updated
        assert (measured.listings, measured.requests, measured.bytes) == reads


def test_silent_file_run_stops_refreshing_without_a_recorded_end(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    key = "lab/base@0/run.json"
    source.put(key, RunRecord(id="lab/base@0", experiment="lab").model_dump_json().encode())
    os.utime(source.root / key, (100, 100))
    with Server(source, None, ("127.0.0.1", 0)) as server:
        server.ingest()
        html = server.page("/runs/lab/base@0") or ""
        assert ">no end recorded<" in html
        assert "last write" in html and "at least 10 minutes" in html
        assert "}, 5000);" not in html
        status = server.page("/experiments/lab/status") or ""
        assert (
            ">no end recorded<" in status
            and "10 minutes" in status
            and "data-refresh" not in status
        )


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
    (home() / "server").write_text("fixture")
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
        server.ingest()
        _publish_ssh(server)
        route = "/runs/" + identity
        queued = unescape(server.page(route) or "")
        assert ">queued<" in queued and '{"queued":"config"}' in queued
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
            run="base@0",
            job="base",
            seed=0,
            clocks=["steps"],
            config={"label": "<writer>"},
            directory=tmp_path / "runs",
        ) as writer:
            writer.log({"loss": 7}, steps=12)
            writer.checkpoint(weights, {"steps": 12})
            server.ingest()
            assert exchanges == 1
            elapsed += 30
            server.ingest()
            _publish_ssh(server)
            reads = source.requests, source.listings
            page = server.page(route) or ""
            assert f"log of {attempt.id}" in page and "output not received" in page
            assert (
                "Last collected" in page
                and "last loop 2 h ago" in page
                and "launcher observed" not in page
            )
            assert "&lt;writer&gt;" in page and "<writer>" not in page
            assert "steps 12" in page and "<td>1</td>" in page
            assert "steps-12" in page and "steps 12 / 40" in page
            assert git(repo, "rev-parse", "HEAD") in page and "on main" in page
            assert (
                attempt.machine in page
                and "L4" in page
                and "1 of 2 attempts used" in page
                and "~$0." in page
            )
            assert (source.requests, source.listings, exchanges) == (*reads, 2)
        backend.run(700)
        elapsed += 30
        server.ingest()
        _publish_ssh(server)
        finished = server.page(route) or ""
        assert ">finished<" in finished and "snapshot complete" in finished
        assert "}, 5000);" not in finished and exchanges == 3
    assert "local" in {pool.name for pool in backend.scheduler.pools()}
    backend.store.close()
