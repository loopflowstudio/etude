"""Folder checkpoints connect registered models, carriage, evaluation and job inputs."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from etude.carriage import RecordMismatch, put_back, recorded_file, recorded_files, restore_set
from etude.checkpoints import CheckpointRecord, PolicyKey
from etude.errors import EtudeError
from etude.eval.match import environment, evaluate
from etude.eval.query import statsdb
from etude.eval.records import Code, Match, checkpoint_player
from etude.experiment import observe
from etude.jobs import Checkpoints, TrainingJob, checkpoint_inputs_for
from etude.records import Continuation, SourceKey
from etude.run import RunFiles
from etude.storage import DirectoryStorage
from etude.train.records import Step
from etude.train.tracking import track
from tests.eval.games import play_checkpoint_folder
from tests.run.storage import MeasuredStorage


class Config(BaseModel):
    updates: int = 2


def train(config: Config) -> None:
    pass


def test_two_models_publish_one_checkpoint_and_read_the_same_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        for _ in run.steps("updates", to=2, save_every=1):
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
    assert observed.path == tmp_path / "bucket/tiny/seed-0/out/checkpoints/updates-00000002"
    assert checkpoint_player(observed.ref).checkpoint == observed.ref
    assert {given.name for given in checkpoint_inputs_for(observed, "start")} == {
        "start/checkpoint.json",
        "start/policy.pt",
        "start/value.pt",
    }
    seated = Match(players=(checkpoint_player(observed.ref), Code(name="opponent")), games=1)
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
    job = TrainingJob(
        name="base", train=train, config=Config(), start=observed, max_hours=1
    ).plain()
    assert len(job.inputs) == 3
    for variable in job.environment:
        monkeypatch.setenv(variable.name, variable.value)
    with track(
        experiment="tiny",
        run="child",
        directory=tmp_path / "child-runs",
        clocks=[Step("updates", "updates")],
    ) as child:
        recorded = RunFiles(child.files.root).run_file()
        assert recorded is not None and recorded.start == observed.key
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


def test_policy_key_defaults_and_roles_keep_separate_checkpoint_lines(tmp_path: Path) -> None:
    def export(folder: Path) -> None:
        (folder / "weights.bin").write_bytes(b"same weights")

    for seed in (2, 0):
        with track(
            experiment="league",
            run=f"small@{seed}",
            job="small",
            seed=seed,
            directory=tmp_path,
            clocks=["updates"],
            strict=True,
        ) as run:
            run.checkpoint(export, updates=100)
            run.checkpoint(export, updates=100, role="exploiter")
            assert len(run.checkpoints()) == 2
    state = observe(tmp_path, "league")
    with pytest.raises(EtudeError, match="ambiguous_policy.*"):
        state.policy(PolicyKey())
    policy = state.policy(PolicyKey(role="exploiter"))
    assert policy.key == PolicyKey(experiment="league", job="small", seed=0, role="exploiter")
    assert isinstance(policy.checkpoints, Checkpoints)
    assert isinstance(policy.checkpoints.last, CheckpointRecord)
    assert policy.checkpoints.at("updates", 100).every(1) == policy.checkpoints
    assert policy.checkpoints.tagged("missing") == ()
    assert len(policy.checkpoints) == 1
    assert "exploiter.updates-00000100" in policy.checkpoints[0].key
    assert policy.checkpoints[0].policy == policy.key
    with pytest.raises(EtudeError, match="policy_not_found"):
        state.policy(PolicyKey(job="missing"))
    with pytest.raises(EtudeError, match="observe"):
        state.policy(PolicyKey(experiment="other"))


def test_policy_key_defaults_choose_lowest_seed_and_list_ambiguous_jobs(tmp_path: Path) -> None:
    for job, seed in (("small", 2), ("small", 0)):
        with track(
            experiment="defaults",
            run=f"{job}@{seed}",
            job=job,
            seed=seed,
            directory=tmp_path,
            clocks=["updates"],
            strict=True,
        ) as run:
            path = run.out / "weights.pt"
            path.write_bytes(b"weights")
            run.checkpoint(path, updates=1)
    assert observe(tmp_path, "defaults").policy(PolicyKey()).key == PolicyKey(
        experiment="defaults", job="small", seed=0, role="policy"
    )
    with track(
        experiment="defaults",
        run="large@0",
        job="large",
        seed=0,
        directory=tmp_path,
        clocks=["updates"],
        strict=True,
    ):
        pass
    with pytest.raises(EtudeError, match="choose job from.*large.*small"):
        observe(tmp_path, "defaults").policy(PolicyKey())


def test_policy_roles_reference_one_out_file_without_sharing_identity(tmp_path: Path) -> None:
    with track(
        experiment="roles",
        run="small@0",
        job="small",
        directory=tmp_path,
        clocks=["updates"],
        strict=True,
    ) as run:
        path = run.out / "weights.pt"
        path.write_bytes(b"shared weights")
        run.checkpoint(path, updates=1)
        run.checkpoint(path, updates=1, role="exploiter")
    state = observe(tmp_path, "roles")
    main = state.policy(PolicyKey(role="policy"))
    exploiter = state.policy(PolicyKey(role="exploiter"))
    assert main.checkpoints[0].artifact == exploiter.checkpoints[0].artifact
    assert main.checkpoints[0].key == "roles/small@0/out/weights.pt"
    assert exploiter.checkpoints[0].key == "roles/small@0/roles/exploiter/out/weights.pt"
    assert main.checkpoints[0].ref.line != exploiter.checkpoints[0].ref.line


def test_policy_key_checkpoint_choosers_follow_continuation_save_order(tmp_path: Path) -> None:
    for name, position in (("z-parent", 1), ("a-child", 2)):
        with track(
            experiment="chain",
            run=name,
            job="small",
            directory=tmp_path,
            clocks=["updates"],
            continues=Continuation(
                parent=SourceKey(namespace="local", kind="run", source_id="z-parent"),
                clock="updates",
                value=1,
            )
            if position == 2
            else None,
        ) as run:
            weights = run.out / "weights.pt"
            weights.write_bytes(name.encode())
            run.checkpoint(weights, updates=position, tags=("chosen",))
    policy = observe(tmp_path, "chain").policy(PolicyKey())
    assert [c.run for c in policy.checkpoints] == ["z-parent", "a-child"]
    assert policy.checkpoints.last is not None and policy.checkpoints.last.run == "a-child"
    assert policy.checkpoints.tagged("chosen").at("updates", 2) == policy.checkpoints[1:]
    assert all(c.policy == policy.key for c in policy.checkpoints)
