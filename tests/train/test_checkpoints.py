"""Folder checkpoints connect registered models, carriage, evaluation and job inputs."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from etude.carriage import RecordMismatch, put_back, recorded_file, recorded_files, restore_set
from etude.eval.match import environment, evaluate
from etude.eval.query import statsdb
from etude.eval.records import Code, Inference, Match
from etude.experiment import observe
from etude.jobs import checkpoint_inputs_for, training
from etude.tracking.storage import DirectoryStorage
from etude.train.records import Step
from etude.train.run import track
from tests.eval.games import play_checkpoint_folder
from tests.run.storage import MeasuredStorage


class Config(BaseModel):
    updates: int = 2


def train(config: Config) -> None:
    pass


def test_two_models_publish_one_checkpoint_and_read_the_same_record(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")  # optional training dependency
    policy, value = torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)
    bucket = DirectoryStorage(tmp_path / "bucket")
    with track(
        experiment="tiny",
        run="seed-0",
        directory=tmp_path / "runs",
        clocks=[Step("updates", "updates")],
        storages=[bucket],
        strict=True,
    ) as run:
        run.resume(policy=policy, value=value)
        for _ in run.steps("updates", to=2, every=1):
            run.save()
        first = run.checkpoint(policy=policy, value=value)
        assert run.checkpoint(policy=policy, value=value) == first
        (checkpoint,) = run.checkpoints()
        assert checkpoint.path.is_dir()
        manifest = json.loads((checkpoint.path / "checkpoint.json").read_text())
        assert manifest == {
            "at": {"updates": 2.0},
            "files": {"policy": "policy.pt", "value": "value.pt"},
        }
        assert torch.equal(
            torch.load(checkpoint.path / "policy.pt", weights_only=True)["weight"], policy.weight
        )
    counted = MeasuredStorage(bucket)
    observed = observe(counted, "tiny").run("seed-0").final
    assert (counted.listings, counted.requests) == (1, 4)
    assert observed is not None
    assert observed.artifact.sha256 == first.sha256
    assert observed.path == tmp_path / "bucket/seed-0/out/checkpoints/updates-00000002"
    assert observed.player(Inference()).checkpoint == observed.ref
    assert {given.name for given in checkpoint_inputs_for(observed, "start")} == {
        "start/checkpoint.json",
        "start/policy.pt",
        "start/value.pt",
    }
    seated = Match(players=(observed.player(Inference()), Code(name="opponent")), games=1)
    result = evaluate(
        experiment="tiny",
        environment=environment("tiny-game"),
        matches=[seated],
        play=play_checkpoint_folder,
        workers=1,
        run="eval",
        directory=tmp_path / "evaluations",
        storages=[bucket],
    )
    assert (result.played, result.skipped) == (1, 0)
    with statsdb("tiny", bucket) as db:
        assert db.sql(
            "select checkpoint from training_checkpoints join players using(checkpoint)"
        ).fetchall() == [(observed.key,)]
    job = training(train, config=Config(), start=observed, max_hours=1)
    assert len(job.inputs) == 3
    # A relay keeps all folder members and checks each against its recorded digest.
    files = restore_set(run.files.root)
    copied = tmp_path / "relay"
    put_back(copied, files)
    for key, member in recorded_files(copied).items():
        assert key.startswith("out/checkpoints/")
        single = recorded_file(copied, key)
        assert len(single[key]) == member.size_bytes
    damaged = {**files, key: b"damaged weights"}
    with pytest.raises(RecordMismatch, match="does not match"):
        put_back(tmp_path / "damaged-relay", damaged)


def test_exported_folder_is_immutable_and_a_failed_export_publishes_nothing(tmp_path: Path) -> None:
    with track(
        experiment="tiny",
        run="r",
        directory=tmp_path,
        clocks=[Step("updates", "updates")],
        strict=True,
    ) as run:

        def export(folder: Path) -> None:
            (folder / "policy").write_bytes(b"weights")

        first = run.checkpoint(export, updates=1)
        assert run.checkpoint(export, updates=1) == first

        def changed(folder: Path) -> None:
            (folder / "policy").write_bytes(b"other weights")

        second = run.checkpoint(changed, updates=1)
        assert second.uri != first.uri
        assert len(run.checkpoints()) == 2

        def failed(folder: Path) -> None:
            (folder / "partial").write_bytes(b"partial")
            raise ValueError("export interrupted")

        with pytest.raises(ValueError, match="export interrupted"):
            run.checkpoint(failed, updates=2)
        assert len(run.checkpoints()) == 2
        assert not list((run.out / "checkpoints").glob(".*"))
