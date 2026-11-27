"""Callable forwards preserve search behavior across shared checkpoint batches."""

import asyncio
import json
from pathlib import Path
from random import Random

import pytest

from etude.eval.batching import _Batches
from etude.eval.bus import ForwardOutcome
from etude.eval.forwards import Forwards
from etude.eval.records import Code, compose
from etude.jobs import Agents, hold_group
from tests.eval.arithmetic import checkpoint
from tests.eval.arithmetic import load as load_adder
from tests.eval.mirror import Mirror, Search, async_forward, build, load, play
from tests.eval.playing import identify_outcome
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
    [report] = [json.loads(path.read_text()) for path in artifacts.glob("game-*.json")]
    if shared:
        batches = json.loads((artifacts / "batches.json").read_text())
        assert report["bus"]["wire_bytes"] > 0
    else:
        batches = json.loads((artifacts / f"batches-{report['pid']}.json").read_text())
        assert report["bus"]["wire_bytes"] == 0
        assert not (artifacts / "batches.json").exists()
    assert sum(batch["forward_calls"] for batch in batches) == 2 * 2 * mirror.depth
    assert {batch["rows"] for batch in batches} == {mirror.worlds * mirror.actions}
    assert sum(batch["loads"] for batch in batches) == 1


def test_mirror_trajectories_do_not_depend_on_other_inputs_in_the_batch() -> None:
    small = checkpoint("small")
    batches: _Batches[tuple[int, ForwardOutcome]] = _Batches(
        load, 32, 1024, encode=identify_outcome
    )
    answers: list[tuple[int, ForwardOutcome]] = []
    next_id = 0

    async def shared(inputs: list[object], *, rows: int | None = None) -> list[object]:
        nonlocal next_id
        next_id += 1
        batches.add(answers.append, next_id, small.checkpoint, [100], 1)
        next_id += 1
        batches.add(
            answers.append,
            next_id,
            small.checkpoint,
            inputs,
            len(inputs) if rows is None else rows,
        )
        batches.flush()
        first = answers.pop(0)
        assert first == (next_id - 1, ForwardOutcome(small.checkpoint.key, [101]))
        _, answer = answers.pop(0)
        assert answer.error is None
        return answer.outputs

    async def compare() -> None:
        for choice in ("greedy", "sample"):
            player = Mirror(over=small, choice=choice)
            local, remote = Search(player, async_forward), Search(player, shared)
            expected_random, actual_random = Random(73117), Random(73117)
            assert [await local.choose(expected_random) for _ in range(4)] == [
                await remote.choose(actual_random) for _ in range(4)
            ]

    asyncio.run(compare())


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
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
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
    artifacts = tmp_path / "grouped/artifacts/agents"
    [report] = [json.loads(path.read_text()) for path in artifacts.glob("game-*.json")]
    [batches] = artifacts.glob("batches-*.json")
    batch_rows = json.loads(batches.read_text())
    assert report["tournaments"] == ["first", "second"]
    assert report["matches"] == 2
    assert sum(batch["loads"] for batch in batch_rows) == 1
    assert sum(batch["forward_calls"] for batch in batch_rows) == 16
    assert report["bus"]["calls"] == 16
    assert report["bus"]["wire_bytes"] == 0


def test_a_checkpoint_larger_than_the_budget_loads_once_per_run() -> None:
    ref = checkpoint("large").checkpoint
    forwards = Forwards(load_adder, memory=1)
    outcomes = [forwards.answer(ref, [index]) for index in range(3)]
    assert [outcome.error for outcome in outcomes] == [None, None, None]
    assert forwards.loads == 1
