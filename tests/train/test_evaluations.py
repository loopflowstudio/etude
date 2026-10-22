"""Training reports join explicit eval requests at exact checkpoint coordinates."""

import json
from pathlib import Path
from typing import cast

import pytest

from etude.eval.match import agent, environment
from etude.eval.records import Model, RoundRobin, digest
from etude.eval.tournament import hold
from etude.reports import Evaluation, record
from etude.run.records import Clock, RunCounters, RunState
from etude.run.storage import DirectoryStorage
from etude.train.read import read
from etude.train.report import pages, render
from etude.train.run import track
from etude.train.serve import Server
from tests.eval.games import play


def test_linked_curves_keep_coordinates_identity_gaps_and_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "runs"
    training = track(
        experiment="learning",
        run="train-a",
        clocks=[
            Clock(name="updates", kind="step", meaning="absolute optimizer updates"),
            Clock(name="decisions", kind="step", meaning="learner decisions"),
        ],
        directory=source,
        strict=True,
    )
    for update in (101, 507, 911, 1203):
        path = tmp_path / "weights"
        path.write_text(str(update))
        training.log({"loss": 1 / update}, updates=update, decisions=update * 256)
        training.checkpoint(path, updates=update, decisions=update * 256)
    training.finish(timeout=5)
    trained = read(source, "learning", "train-a")
    assert trained.file is not None
    script = tmp_path / "anchor.py"
    script.write_text("anchor")
    anchor = agent("same-name", code=[script], config={"strength": 0})
    protocol = RoundRobin(
        name="benchmark",
        environment=environment("duel"),
        conditions=({"deal": 1}, {"deal": 2}),
        games=4,
    )
    links = []
    for index, checkpoint in enumerate(trained.checkpoints):
        subject = Model(config={"strength": index}).agent(checkpoint.artifact)
        link = Evaluation(
            training=trained.file.declaration.source_key,
            checkpoint=checkpoint,
            run=f"eval-{index}",
            benchmark=digest(protocol.model_dump(mode="json")),
            label="same-name",
            subject=subject.identity,
        )
        record(source, trained.name, link)
        links.append(link)

    # A later checkpoint finishes first. Evaluation wall time has no role on x.
    def finish(index: int) -> None:
        link = links[index]
        monkeypatch.setenv("ETUDE_RUN", link.run)
        hold(
            protocol,
            enter=(Model(config={"strength": index}).agent(link.checkpoint.artifact), anchor),
            play=play,
            directory=source,
        )

    finish(1)
    before = pages(source)["learning"]
    assert before == pages(source)["learning"]
    evaluations = cast(list[dict[str, object]], before["evaluations"])
    assert [e["state"] for e in evaluations] == ["pending", "finished", "pending", "pending"]
    assert evaluations[1]["done"] == evaluations[1]["scheduled"] == 2
    finish(0)
    # Explicit failure, plus unfinished final request: neither becomes zero.
    storage = DirectoryStorage(source)
    storage.put(
        "eval-2/state.json",
        RunState(state="failed", counters=RunCounters()).model_dump_json().encode(),
    )
    page = pages(source)["learning"]
    evaluations = cast(list[dict[str, object]], page["evaluations"])
    assert [e["state"] for e in evaluations] == ["finished", "finished", "failed", "pending"]
    assert [e["checkpoint"]["at"]["updates"] for e in evaluations] == [101, 507, 911, 1203]  # type: ignore[index]
    assert [e["checkpoint"]["at"]["decisions"] for e in evaluations] == [  # type: ignore[index]
        25856,
        129792,
        233216,
        307968,
    ]
    results = cast(list[dict[str, object]], evaluations[1]["results"])
    assert {r["metric"] for r in results} >= {"won", "hits"}
    assert all(r["rows"] == 8 and r["matches"] == 2 for r in results)
    assert evaluations[2]["results"] == evaluations[3]["results"] == []
    assert page["runs"][0]["name"] == "train-a"  # type: ignore[index]
    html = render(page, refresh=2)
    assert 'id="evaluation-charts"' in html and "checkpoint.at[clock]" in html
    assert '"updates": 507.0' in html and "training seeds" in html
    server = Server(source, ("127.0.0.1", 0))
    try:
        assert '"run": "eval-1"' in (server.report("learning") or "")
    finally:
        server.server_close()
    # Same weights recorded by another run do not steal the explicit association.
    other = track(experiment="learning", run="aaa", clocks=trained.clocks, directory=source)
    other.checkpoint(trained.checkpoints[0].artifact, updates=9999, decisions=1)
    other.finish(timeout=5)
    assert (
        cast(list[dict[str, object]], pages(source)["learning"]["evaluations"])[0]["training_run"]
        == "train-a"
    )
    record(source, "train-a", links[0])
    with pytest.raises(ValueError, match="binding_changed"):
        record(source, "train-a", links[0].model_copy(update={"benchmark": "a" * 64}))
    # Retain delivered partial counts even if a producer wrongly says finished.
    (source / "eval-1" / "matches").joinpath(
        next((source / "eval-1" / "matches").iterdir()).name
    ).unlink()
    incomplete = cast(list[dict[str, object]], pages(source)["learning"]["evaluations"])[1]
    assert (
        incomplete["state"] == "incomplete"
        and incomplete["done"] == 1
        and incomplete["pending"] == 1
    )
    assert json.loads(json.dumps(page))["experiment"] == "learning"
