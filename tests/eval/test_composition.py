"""Player trees share checkpoint loading while retaining independent results."""

import json
from multiprocessing import get_context
from pathlib import Path

import pytest

from etude.errors import EtudeError, LauncherError, RetryRequested
from etude.eval.agents import Answer, Request
from etude.eval.batching import _Batches
from etude.eval.composition import _restore
from etude.eval.query import statsdb
from etude.eval.records import (
    Aligned,
    Benchmark,
    Code,
    Held,
    Line,
    Match,
    Seated,
    Seating,
    compose,
    player_checkpoints,
    relocate_player,
)
from etude.eval.tournament import evaluating, played, scores
from etude.jobs import Agents, ExperimentState, _relocated, checkpoint_inputs, hold_group
from etude.page import _players
from examples.agent_game import Shift, build, checkpoint
from examples.agent_models import load
from tests.eval.trees import Ensemble, Tagged
from tests.test_eval_groups import (
    asks_or_returns,
    echo,
    group,
    job,
    plain,
    selective_load,
    selective_retry_load,
)


def test_nested_players_keep_identity_while_sharing_checkpoint_bytes() -> None:
    small = checkpoint("small")
    shifted = compose(Shift(over=small))
    deeper = compose(Shift(over=shifted, amount=2))
    match = Match(players=(small, shifted, deeper))
    assert len({p.key for p in match.players}) == 3
    assert [c.key for p in match.players for c in player_checkpoints(p)] == ["small"] * 3
    moved = relocate_player(deeper, {"small": "file:///another/machine/weights"})
    assert moved.key == deeper.key
    assert player_checkpoints(moved)[0].artifact.uri == "file:///another/machine/weights"
    assert Match.model_validate_json(match.model_dump_json()) == match
    assert compose(Shift(over=small, amount=2)).key != deeper.key


def test_distinct_players_share_a_checkpoint_batch() -> None:
    small = checkpoint("small")
    shifted = compose(Shift(over=small))
    parent, child = get_context("spawn").Pipe()
    batches = _Batches(echo, 2, 1, 1024)
    try:
        for player, name in ((small, "bare"), (shifted, "composed")):
            batches.add(child, Request(small.checkpoint, [name], "ladder", name, player.key), 0)
        observations = batches.flush(0)
        assert parent.recv() == Answer(["bare"])
        assert parent.recv() == Answer(["composed"])
        assert batches.loads == batches.batches == 1
        assert observations[0].checkpoint == "small"
        assert {row.player for row in observations[0].rows} == {small.key, shifted.key}
    finally:
        parent.close()
        child.close()


def test_group_builds_nested_players_and_loads_the_shared_leaf_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    small = checkpoint("small")
    shifted = compose(Shift(over=small))
    deeper = compose(Shift(over=shifted, amount=2))
    jobs = [
        job(name, Agents(load=load, build=build)).model_copy(
            update={"enter": (small, shifted, deeper)}
        )
        for name in ("ladder", "gauntlet")
    ]
    selected = group(*jobs)
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [3, 3]
    assert capfd.readouterr().err.count("agents: loaded") == 1
    batches = json.loads((tmp_path / "grouped/artifacts/agents/batches.json").read_text())
    assert {b["checkpoint"] for b in batches} == {"small"}
    assert {r["player"] for b in batches for r in b["rows"]} == {small.key, shifted.key, deeper.key}
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from players").fetchone() == (3,)
        placements, notes = _players(db, {}, {})
        assert {p.key for p in placements} == {small.key, shifted.key, deeper.key}
        assert all(any(p.name in note for note in notes) for p in placements)
        assert db.sql(
            "select score, count(*) from scores group by score order by score"
        ).fetchall() == [(2, 4), (3, 4), (5, 4)]
    finally:
        db.close()
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0, 0]
    assert "agents: started" not in capfd.readouterr().err


@pytest.mark.parametrize("retry", [False, True])
def test_loader_failure_follows_a_checkpoint_inside_every_player_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 2)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    broken = checkpoint("broken")
    nested = compose(Shift(over=compose(Shift(over=broken))))
    jobs = [
        job(
            name, Agents(load=selective_retry_load if retry else selective_load, build=build)
        ).model_copy(
            update={
                "enter": (broken, nested, checkpoint("left"), checkpoint("right")),
                "play": asks_or_returns,
            }
        )
        for name in ("one", "two")
    ]
    with pytest.raises(RetryRequested if retry else EtudeError):
        hold_group(group(*jobs), directory=tmp_path)
    for active in jobs:
        found = played(
            active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path
        )
        assert len(found.failed) == 5
        assert len(found.delivered) == 1
        assert len(found.new) == len(found.retry) == (5 if retry else 0)
        assert all(
            any(c.key == "broken" for p in m.players for c in player_checkpoints(p))
            for m in found.failed
        )


def test_composition_resolves_two_relative_leaves() -> None:
    held = checkpoint("held").checkpoint.model_copy(update={"line": "held", "at": {"updates": 10}})
    left = checkpoint("left").checkpoint.model_copy(update={"line": "left", "at": {"updates": 10}})
    right = checkpoint("right").checkpoint.model_copy(
        update={"line": "right", "at": {"updates": 10}}
    )
    # A record may carry any number of checkpoint-bearing fields, including containers.
    tree = compose(
        Ensemble(
            members=(Aligned(clock="updates", job="left"), Aligned(clock="updates", job="right"))
        )
    )
    seating: Seating = Seating(name="both", players=(Held(), tree), configs=({},), games=3)
    seated = seating.resolve(
        held,
        (
            Line(name="held", checkpoints=(held,)),
            Line(name="left", job="left", checkpoints=(left,)),
            Line(name="right", job="right", checkpoints=(right,)),
        ),
    )
    assert isinstance(seated, Seated)
    (match,) = seating.matches(held, seated)
    assert [c.key for c in player_checkpoints(match.players[1])] == ["left", "right"]
    assert match.games == 3


def test_benchmark_scores_keep_players_with_one_or_several_checkpoint_leaves(
    tmp_path: Path,
) -> None:
    small = checkpoint("small").checkpoint
    wrapped = compose(Shift(over=Held()))
    nested = compose(Shift(over=wrapped, amount=2))
    ensemble = compose(Ensemble(members=(Held(), checkpoint("belief"))))
    seatings: tuple[Seating, ...] = tuple(
        Seating(name=name, players=(player, Code(name="heuristic")), configs=({},), games=1)
        for name, player in (
            ("bare", Held()),
            ("shift", wrapped),
            ("nested", nested),
            ("ensemble", ensemble),
        )
    )
    tournament = Benchmark(
        name="all", environment=job("unused", None).tournament.environment, seatings=seatings
    )
    with evaluating(tournament, experiment="trees", checkpoint=small, directory=tmp_path) as active:
        assert active.play(plain).played == 4
    db = statsdb("trees", tmp_path)
    try:
        lines = scores(db, "all", table="scores", value="score")
        assert {line.seating for line in lines} == {"bare", "shift", "nested", "ensemble"}
        assert all(len(line.points) == 1 and line.points[0].checkpoint == "small" for line in lines)
        # Multiple leaves have no invented primary checkpoint; their scheduled held point still scores.
        assert db.execute(
            "select checkpoint from players where player = ?", [ensemble.player(small).key]
        ).fetchone() == (None,)
    finally:
        db.close()


def test_mapping_fields_are_data_and_keep_their_types_in_the_build_hook() -> None:
    small = checkpoint("small")
    record = Ensemble(
        members=(compose(Shift(over=small)),),
        options={"kind": "model", "checkpoint": "ordinary data"},
    )
    tree = compose(record)
    assert [c.key for c in player_checkpoints(tree)] == ["small"]
    restored = _restore(tree)
    assert isinstance(restored, Ensemble)
    assert isinstance(restored.members, tuple)
    assert isinstance(restored.members[0], Shift)
    assert restored.options == record.options


def test_checkpoint_inputs_deduplicate_and_relocate_every_tree_leaf() -> None:
    small, belief = checkpoint("small"), checkpoint("belief")
    tree = compose(Ensemble(members=(small, compose(Shift(over=small)), belief)))
    match = Match(players=(small, tree))
    inputs = checkpoint_inputs((match,), ExperimentState(experiment="trees"))
    assert set(inputs) == {"small", "belief"}
    moved = _relocated(match, {key: f"file:///inputs/{key}" for key in inputs})
    assert moved.key == match.key
    assert [ref.artifact.uri for p in moved.players for ref in player_checkpoints(p)] == [
        "file:///inputs/small",
        "file:///inputs/small",
        "file:///inputs/small",
        "file:///inputs/belief",
    ]


def test_author_kind_fields_do_not_impersonate_checkpoint_records() -> None:
    small = checkpoint("small")
    tree = compose(Tagged(over=small))
    assert tree.key != small.key
    assert [c.key for c in player_checkpoints(tree)] == ["small"]
    restored = _restore(tree)
    assert isinstance(restored, Tagged) and restored.over == small


def test_a_composed_player_needs_its_build_hook_at_declaration() -> None:
    active = job("wrapped", Agents(load=load)).model_copy(
        update={"enter": (checkpoint("small"), compose(Shift(over=checkpoint("small"))))}
    )
    with pytest.raises(LauncherError, match="build"):
        active.plain()


def test_one_seed_selects_one_held_line_for_fixed_and_composed_opponents() -> None:
    def line(name: str, job_name: str, seed: int) -> Line:
        ref = checkpoint(name).checkpoint.model_copy(update={"line": name, "at": {"updates": 10}})
        return Line(name=name, job=job_name, seed=seed, checkpoints=(ref,))

    mine = [line(f"mine-{seed}", "mine", seed) for seed in range(3)]
    others = [line(f"{name}-{seed}", name, seed) for name in ("left", "right") for seed in range(3)]
    tree = compose(
        Ensemble(
            members=(Aligned(clock="updates", job="left"), Aligned(clock="updates", job="right"))
        )
    )
    for opponent in (checkpoint("fixed"), tree):
        seating: Seating = Seating(
            name="one", players=(Held(), opponent), configs=({},), games=7, seeds="one"
        )
        scheduled = [seating.resolve(each.checkpoints[0], (*mine, *others)) for each in mine]
        assert all(isinstance(result, Seated) for result in scheduled)
        nonempty = [
            result for result in scheduled if isinstance(result, Seated) and result.pairings
        ]
        assert len(nonempty) == 1
        assert sum(pair.games for pair in nonempty[0].pairings) == 7
