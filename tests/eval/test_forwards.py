"""Callable forwards preserve search behavior across shared checkpoint batches."""

import json
from multiprocessing import get_context
from pathlib import Path
from random import Random

import pytest

from etude.eval.agents import Answer, Request
from etude.eval.batching import _Batches
from etude.eval.records import Code, compose
from etude.jobs import Agents, hold_group
from tests.eval.arithmetic import checkpoint
from tests.eval.mirror import Mirror, Search, build, forward, load, play
from tests.test_eval_groups import group, job


@pytest.mark.parametrize("shared", [False, True])
def test_mirror_sends_all_branches_once_per_ply_and_builds_author_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    monkeypatch.setattr(
        "etude.eval.resources._detect_accelerator", lambda: "cuda" if shared else "no accelerator detected"
    )
    small = checkpoint("small")
    mirror = Mirror(over=small)
    greedy = mirror.model_copy(update={"choice": "greedy"})
    assert compose(mirror).key != compose(greedy).key
    assert compose(mirror).key != compose(mirror.model_copy(update={"temperature": 2.0})).key
    selected = job("mirror", Agents(load=load, build=build, max_delay_seconds=0)).model_copy(
        update={"play": play, "enter": (compose(mirror), compose(greedy))}
    )
    assert hold_group(group(selected), directory=tmp_path)[0].played == 1
    artifacts = tmp_path / "grouped/artifacts/agents"
    if shared:
        batches = json.loads((artifacts / "batches.json").read_text())
        assert sum(batch["forward_calls"] for batch in batches) == 2 * 2 * mirror.depth
        assert {len(batch["inputs"]) for batch in batches} == {mirror.worlds * mirror.actions}
        assert sum(batch["loads"] for batch in batches) == 1
    else:
        [report] = [json.loads(path.read_text()) for path in artifacts.glob("protocol-*.json")]
        assert report["local_forward_calls"] == 2 * 2 * mirror.depth
        assert report["local_inputs"] == 2 * 2 * mirror.depth * mirror.worlds * mirror.actions
        assert report["local_loads"] == 1
        assert not (artifacts / "batches.json").exists()


def test_mirror_trajectories_do_not_depend_on_other_inputs_in_the_batch() -> None:
    small = checkpoint("small")
    parent, child = get_context("spawn").Pipe()
    batches = _Batches(load, 32, 0, 1024)

    def shared(inputs: list[object]) -> list[object]:
        batches.add(child, Request(small.checkpoint, [100], "other", "other", small.key), 0)
        batches.add(child, Request(small.checkpoint, inputs, "mirror", "mirror", small.key), 0)
        batches.flush(0)
        assert parent.recv() == Answer([101])
        answer = parent.recv()
        assert isinstance(answer, Answer)
        assert answer.error is None
        return answer.outputs

    try:
        for choice in ("greedy", "sample"):
            player = Mirror(over=small, choice=choice)
            local, remote = Search(player, forward), Search(player, shared)
            expected_random, actual_random = Random(73117), Random(73117)
            assert [local.choose(expected_random) for _ in range(4)] == [
                remote.choose(actual_random) for _ in range(4)
            ]
    finally:
        parent.close()
        child.close()


def test_code_players_build_author_objects_without_an_agent_process(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    selected = job("code", None).model_copy(
        update={
            "play": play,
            "agents": Agents(build=build),
            "enter": (Code(name="one"), Code(name="two")),
        }
    )
    assert hold_group(group(selected), directory=tmp_path)[0].played == 1
    assert "agents: shared inference" not in capfd.readouterr().err
    assert not (tmp_path / "grouped/artifacts/agents/batches.json").exists()


def test_cpu_games_reuse_the_checkpoint_across_tournaments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources._detect_accelerator", lambda: "no accelerator detected")
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    small = checkpoint("small")
    players = (compose(Mirror(over=small)), compose(Mirror(over=small, choice="greedy")))
    declarations = [
        job(name, Agents(load=load, build=build)).model_copy(
            update={"play": play, "enter": players}
        )
        for name in ("first", "second")
    ]
    assert [result.played for result in hold_group(group(*declarations), directory=tmp_path)] == [
        1,
        1,
    ]
    reports = [
        json.loads(path.read_text())
        for path in (tmp_path / "grouped/artifacts/agents").glob("protocol-*.json")
    ]
    assert len(reports) == 2
    assert sum(report["local_loads"] for report in reports) == 1
    assert sum(report["local_forward_calls"] for report in reports) == 16
    assert all(report["requests"] == report["answers"] == 0 for report in reports)
