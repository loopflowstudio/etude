"""Callable forwards preserve search behavior across shared checkpoint batches."""

import asyncio
import json
import socket
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from random import Random

import pytest

from etude.eval.agents import Answer, CheckpointRequest, recv_frame
from etude.eval.batching import _Batches
from etude.eval.records import Code, compose
from etude.jobs import Agents, hold_group
from tests.eval.arithmetic import checkpoint
from tests.eval.mirror import Mirror, Search, async_forward, build, load, play
from tests.test_eval_groups import group, job


@pytest.mark.parametrize("shared", [False, True])
def test_mirror_sends_all_branches_once_per_ply_and_builds_author_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    monkeypatch.setattr(
        "etude.eval.resources._detect_accelerator",
        lambda: "cuda" if shared else "no accelerator detected",
    )
    small = checkpoint("small")
    mirror = Mirror(over=small)
    greedy = mirror.model_copy(update={"choice": "greedy"})
    assert compose(mirror).key != compose(greedy).key
    assert compose(mirror).key != compose(mirror.model_copy(update={"temperature": 2.0})).key
    selected = job("mirror", Agents(load=load, build=build)).model_copy(
        update={"play": play, "enter": (compose(mirror), compose(greedy))}
    )
    assert hold_group(group(selected), directory=tmp_path)[0].played == 1
    artifacts = tmp_path / "grouped/artifacts/agents"
    if shared:
        batches = json.loads((artifacts / "batches.json").read_text())
        assert sum(batch["forward_calls"] for batch in batches) == 2 * 2 * mirror.depth
        assert {batch["rows"] for batch in batches} == {mirror.worlds * mirror.actions}
        assert sum(batch["loads"] for batch in batches) == 1
    else:
        [report] = [json.loads(path.read_text()) for path in artifacts.glob("game-*.json")]
        assert report["local_forward_calls"] == 2 * 2 * mirror.depth
        assert report["local_rows"] == 2 * 2 * mirror.depth * mirror.worlds * mirror.actions
        assert report["local_loads"] == 1
        assert not (artifacts / "batches.json").exists()


def test_mirror_trajectories_do_not_depend_on_other_inputs_in_the_batch() -> None:
    small = checkpoint("small")
    parent, child = socket.socketpair()
    batches = _Batches(load, 32, 1024)

    async def shared(inputs: list[object], *, rows: int | None = None) -> list[object]:
        batches.add(
            child,
            [
                CheckpointRequest(1, small.checkpoint, [100], 1),
                CheckpointRequest(
                    2, small.checkpoint, inputs, len(inputs) if rows is None else rows
                ),
            ],
        )
        batches.flush()
        first = ForkingPickler.loads(recv_frame(parent))
        assert first == Answer(1, [101])
        answer = ForkingPickler.loads(recv_frame(parent))
        assert isinstance(answer, Answer)
        assert isinstance(answer.result, list)
        return answer.result

    async def compare() -> None:
        for choice in ("greedy", "sample"):
            player = Mirror(over=small, choice=choice)
            local, remote = Search(player, async_forward), Search(player, shared)
            expected_random, actual_random = Random(73117), Random(73117)
            assert [await local.choose(expected_random) for _ in range(4)] == [
                await remote.choose(actual_random) for _ in range(4)
            ]

    try:
        asyncio.run(compare())
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


def test_cpu_games_in_a_group_load_checkpoints_in_each_game_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "etude.eval.resources._detect_accelerator", lambda: "no accelerator detected"
    )
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
        for path in (tmp_path / "grouped/artifacts/agents").glob("game-*.json")
    ]
    assert len(reports) == 2
    assert sum(report["matches"] for report in reports) == 2
    assert sum(report["local_forward_calls"] for report in reports) == 16
    assert all(report["local_loads"] == 1 for report in reports)
    assert all(report["requests"] == report["answers"] == 0 for report in reports)
