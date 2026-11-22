"""What the local view serves while the writer and report keep moving."""

from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws

from etude.block_file import tail_key
from etude.cli import main
from etude.launch.source import home
from etude.page import Page, Pages, build
from etude.records import Clock, RunRecord
from etude.storage import DirectoryStorage, S3Storage, Storage
from etude.train.block import Builder
from etude.train.tracking import write_block
from etude.view import Server
from etude.view_link import advertise, print_view
from tests.experiments import experiment_file
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
    server = Server(measured, file, ("127.0.0.1", 0))
    try:
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
    finally:
        server.server_close()


def test_refresh_failure_preserves_the_last_page_and_recovers(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    file = experiment_file(tmp_path, "lab", REPORTED)
    with Server(runs, file, ("127.0.0.1", 0)) as server:
        original = server.page("/experiments/lab/report")
        assert original is not None
        assert '"refresh": null' in original and 'id="refresh-report"' in original
        file.write_text("raise RuntimeError('broken report')\n")
        with pytest.raises(RuntimeError, match="broken report"):
            server.page("/experiments/lab/report", refresh=True)
        assert (
            server.page("/experiments/lab/report") == original
        )  # includes its original generation timestamp
        file.write_text(REPORTED.replace("Report()", 'Report(labels={"a": "Readable"})'))
        refreshed = server.page("/experiments/lab/report", refresh=True)
        assert refreshed != original and '"Readable"' in (refreshed or "")
        assert server.page("/experiments/lab/report") == refreshed


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
    file = experiment_file(tmp_path, "lab", REPORTED)
    started, release, joined = threading.Event(), threading.Event(), threading.Event()
    build_page = Pages.build
    generations = 0

    def blocked_build(self: Pages, path: Path | None) -> Page:
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
    monkeypatch.setattr("etude.view.Future", Joined)
    with Server(runs, file, ("127.0.0.1", 0)) as server, ThreadPoolExecutor(2) as pool:
        server.ingest()
        first = pool.submit(server.page, "/experiments/lab/report", refresh=True)
        try:
            assert started.wait(5)
            second = pool.submit(server.page, "/experiments/lab/report", refresh=True)
            assert joined.wait(5)
            assert ">finished<" in (server.page("/runs/lab/a") or "")
        finally:
            release.set()
        assert first.result(5) == second.result(5) == server.page("/experiments/lab/report")
        assert generations == 1


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
            file.write_text("raise RuntimeError('cannot generate token=fixture-credential')\n")
            client.request("POST", "/experiments/lab/report")
            reply = client.getresponse()
            error = reply.read()
            assert reply.status == 500 and b"cannot generate token=[redacted]" in error
            assert b"fixture-credential" not in error
            client.request("GET", "/experiments/lab/report")
            reply = client.getresponse()
            assert reply.status == 200 and reply.read() == original
            file.write_text(REPORTED)
            client.request("POST", "/experiments/lab/report")
            reply = client.getresponse()
            assert reply.status == 200 and b'id="refresh-report"' in reply.read()
        finally:
            client.close()
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def test_a_shared_report_module_of_the_repository_is_reread_on_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations: list[Path | None] = []

    def counted(
        storage: Storage | Path | str, file: Path | None, *, experiment: str | None = None
    ) -> Page:
        generations.append(file)
        return build(storage, file, experiment=experiment)

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
        first = server.page("/experiments/frontier/report") or ""
        assert all(text in first for text in ('"Width 64"', '"Standard"', '"Frontier only"'))
        server.page("/experiments/frontier/report", refresh=True)
        assert len(generations) == 1  # nothing changed, so nothing is rebuilt
        shared.write_text(shared.read_text().replace("Standard", "Edited standard"))
        assert '"Edited standard"' in (
            server.page("/experiments/frontier/report", refresh=True) or ""
        )
        assert len(generations) == 2


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
        assert (measured.listings, measured.requests, measured.bytes) == reads
        outcome.write_bytes(saved)
        restarted.ingest()
        assert restarted.index.error is None


def test_view_links_follow_only_the_live_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    address = home() / "view"
    address.write_text("http://stale.invalid")
    print_view("lab/base@0")
    assert capsys.readouterr().out == ""
    with advertise("http://localhost:1234"):
        print_view("lab/base@0")
        assert capsys.readouterr().out.strip() == "view: http://localhost:1234/runs/lab/base@0"
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
