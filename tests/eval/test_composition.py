"""Player trees share checkpoint loading while retaining independent results."""

import json
import socket
from multiprocessing.reduction import ForkingPickler
from pathlib import Path

import pytest

from etude.checkpoints import CheckpointRef, PolicyKey
from etude.errors import EtudeError, LauncherError, RetryRequested
from etude.eval.agents import Answer, CheckpointRequest, recv_frame
from etude.eval.batching import _Batches
from etude.eval.composition import _restore
from etude.eval.query import statsdb
from etude.eval.records import (
    Aligned,
    Benchmark,
    CheckpointSlot,
    Code,
    Composition,
    Final,
    Line,
    Match,
    Seated,
    Seating,
    TournamentRun,
    checkpoint_player,
    compose,
    player_checkpoints,
    relocate_player,
)
from etude.eval.tournament import evaluating, played, scores
from etude.jobs import Agents, EvalJob, ExperimentState, checkpoint_inputs, hold_group
from etude.page import build as build_page
from etude.records import ArtifactRef
from tests.eval.arithmetic import Shift, build, checkpoint, load
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
from tests.test_eval_groups import (
    shared_agents as shared_agents,
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


@pytest.mark.parametrize("wrapped", [False, True])
def test_relocated_entrants_resume_without_replaying(tmp_path: Path, wrapped: bool) -> None:
    small = checkpoint("small")
    player = compose(Ensemble(members=(small, compose(Shift(over=small))))) if wrapped else small
    tournament = job("relocated", None).tournament
    with evaluating(
        tournament,
        experiment="trees",
        enter=(player, Code(name="heuristic")),
        directory=tmp_path,
        run="relocated",
    ) as active:
        assert active.play(plain).played == 1
    moved = relocate_player(player, {"small": "file:///another/machine/weights"})
    with evaluating(
        tournament,
        experiment="trees",
        enter=(moved, Code(name="heuristic")),
        directory=tmp_path,
        run="relocated",
        resume=True,
    ) as active:
        assert active.play(plain).played == 0


def test_relocated_benchmark_leaves_and_pairings_resume_without_replaying(tmp_path: Path) -> None:
    small = checkpoint("small").checkpoint
    other = checkpoint("other")

    def benchmark(opponent: Composition) -> Benchmark:
        return Benchmark(
            name="relocated",
            environment=job("unused", None).tournament.environment,
            seatings=(
                Seating(
                    name="relative",
                    players=(CheckpointSlot(), opponent),
                    configs=({},),
                    games=1,
                ),
            ),
        )

    def lines(player: Composition) -> tuple[Line, ...]:
        return (Line(name="other", job="other", checkpoints=player_checkpoints(player)),)

    small = small.model_copy(update={"at": {"updates": 1}})
    other_ref = other.checkpoint.model_copy(update={"at": {"updates": 1}})
    fixed = compose(Shift(over=checkpoint_player(other_ref)))
    for uri, expected in ((small.artifact.uri, 1), ("file:///moved/weights", 0)):
        moved = relocate_player(fixed, {"other": uri})
        assert isinstance(moved, Composition)
        tree = compose(Ensemble(members=(moved, Aligned(clock="updates", job="other"))))
        ref = small.model_copy(update={"artifact": small.artifact.model_copy(update={"uri": uri})})
        with evaluating(
            benchmark(tree),
            experiment="trees",
            checkpoint=ref,
            lines=lines(moved),
            directory=tmp_path,
            run="relocated",
            resume=expected == 0,
        ) as active:
            assert active.play(plain).played == expected


@pytest.mark.parametrize("changed", ["bytes", "author_uri"])
def test_resume_rejects_changed_checkpoint_bytes_and_author_locations(
    tmp_path: Path, changed: str
) -> None:
    small = checkpoint("small")
    tree = compose(Ensemble(members=(small,), options={"uri": "file:///authored"}))
    tournament = job("same", None).tournament
    with evaluating(
        tournament,
        experiment="trees",
        enter=(tree, Code(name="heuristic")),
        directory=tmp_path,
        run="unchanged",
    ) as active:
        assert active.play(plain).played == 1
    changed_data = compose(Ensemble(members=(small,), options={"uri": "file:///different"}))
    changed_bytes = small.model_copy(
        update={
            "checkpoint": small.checkpoint.model_copy(
                update={
                    "artifact": small.checkpoint.artifact.model_copy(update={"sha256": "0" * 64})
                }
            )
        }
    )
    changed_tree = compose(Ensemble(members=(changed_bytes,), options={"uri": "file:///authored"}))
    with pytest.raises(EtudeError) as refused:
        with evaluating(
            tournament,
            experiment="trees",
            enter=(changed_tree if changed == "bytes" else changed_data, Code(name="heuristic")),
            directory=tmp_path,
            run="unchanged",
            resume=True,
        ):
            pytest.fail("a changed hold must be refused")
    assert refused.value.code == "eval_run_tournament_conflict"


def test_same_named_author_types_have_distinct_labels_and_keys() -> None:
    first = Composition(type="one:Search", fields={"simulations": 64})
    second = Composition(type="two:Search", fields=first.fields)
    assert first.name == "one:Search(simulations=64)"
    assert second.name == "two:Search(simulations=64)"
    assert first.key != second.key


def test_distinct_players_share_a_checkpoint_batch() -> None:
    small = checkpoint("small")
    parent, child = socket.socketpair()
    batches = _Batches(echo, 2, 1024)
    try:
        for req_id, name in ((1, "bare"), (2, "composed")):
            batches.add(child, CheckpointRequest(req_id, small.checkpoint, [name], 1))
        observations = batches.flush()
        assert ForkingPickler.loads(recv_frame(parent)) == Answer(1, "small", ["bare"])
        assert ForkingPickler.loads(recv_frame(parent)) == Answer(2, "small", ["composed"])
        assert batches.loads == batches.batches == 1
        assert observations[0].checkpoint == "small"
        assert observations[0].rows == 2
        assert observations[0].requests == 2
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
    assert sum(b["rows"] for b in batches) == 12
    db = statsdb("grouped", tmp_path)
    try:
        assert {row[0] for row in db.sql("select name from players").fetchall()} == {
            "tests.eval.arithmetic:Shift(over=tests.eval.arithmetic:Shift(over=small, amount=1), amount=2)",
            "tests.eval.arithmetic:Shift(over=small, amount=1)",
            "small",
        }
        assert db.sql(
            "select score, count(*) from scores group by score order by score"
        ).fetchall() == [(2, 4), (3, 4), (5, 4)]
    finally:
        db.close()
    page = build_page(tmp_path, None, experiment="grouped")
    assert {p.key for p in page.players} == {small.key, shifted.key, deeper.key}
    note = next(n for n in page.notes if n.startswith("Drawn as anchors"))
    assert all(p.name in note for p in page.players)
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0, 0]
    assert "agents: shared inference" not in capfd.readouterr().err


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
        # A game that returned before the first load failed remains delivered.
        # Every other match containing the failed leaf must retain that failure.
        delivered_broken = sum(
            any(c.key == "broken" for p in result.match.players for c in player_checkpoints(p))
            for result in found.delivered
        )
        assert len(found.failed) + delivered_broken == 5
        assert len(found.failed) + len(found.delivered) == 6
        assert len(found.new) == len(found.retry) == (len(found.failed) if retry else 0)
        assert all(
            any(c.key == "broken" for p in m.players for c in player_checkpoints(p))
            for m in found.failed
        )


def test_composition_resolves_two_relative_leaves() -> None:
    held = checkpoint("held").checkpoint.model_copy(
        update={"outside_line": "held", "at": {"updates": 10}}
    )
    left = checkpoint("left").checkpoint.model_copy(
        update={"outside_line": "left", "at": {"updates": 10}}
    )
    right = checkpoint("right").checkpoint.model_copy(
        update={"outside_line": "right", "at": {"updates": 10}}
    )
    # A record may carry any number of checkpoint-bearing fields, including containers.
    tree = compose(
        Ensemble(
            members=(Aligned(clock="updates", job="left"), Aligned(clock="updates", job="right"))
        )
    )
    seating: Seating = Seating(
        name="both", players=(CheckpointSlot(), tree), configs=({},), games=3
    )
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
    wrapped = compose(Shift(over=CheckpointSlot()))
    nested = compose(Shift(over=wrapped, amount=2))
    ensemble = compose(Ensemble(members=(CheckpointSlot(), checkpoint("belief"))))
    seatings: tuple[Seating, ...] = tuple(
        Seating(name=name, players=(player, Code(name="heuristic")), configs=({},), games=1)
        for name, player in (
            ("bare", CheckpointSlot()),
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
    spec = group(job("relocate", None)).evaluations[0].model_copy(update={"matches": (match,)})
    relocated = spec.relocate({key: f"file:///inputs/{key}" for key in inputs})
    assert relocated.matches is not None
    [moved] = relocated.matches
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
    active = job("wrapped", Agents(load=load))
    with pytest.raises(LauncherError, match="build"):
        EvalJob(
            tournament=active.tournament,
            play=active.play,
            agents=active.agents,
            enter=(checkpoint("small"), compose(Shift(over=checkpoint("small")))),
            max_hours=0.1,
        )


def test_one_seed_selects_one_held_line_for_fixed_and_composed_opponents() -> None:
    def line(name: str, job_name: str, seed: int) -> Line:
        ref = checkpoint(name).checkpoint.model_copy(
            update={"outside_line": name, "at": {"updates": 10}}
        )
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
            name="one", players=(CheckpointSlot(), opponent), configs=({},), games=7, seeds="one"
        )
        scheduled = [seating.resolve(each.checkpoints[0], (*mine, *others)) for each in mine]
        assert all(isinstance(result, Seated) for result in scheduled)
        nonempty = [
            result for result in scheduled if isinstance(result, Seated) and result.pairings
        ]
        assert len(nonempty) == 1
        assert sum(pair.games for pair in nonempty[0].pairings) == 7


def test_composed_policy_seats_resolve_each_experiment_and_role() -> None:
    artifact = ArtifactRef(uri="file:///weights", sha256="a" * 64, size_bytes=1)
    held = CheckpointRef(
        experiment="league", job="base", seed=0, role="policy", name="final", artifact=artifact
    )
    opponent = CheckpointRef(
        experiment="rival", job="base", seed=2, role="policy", name="final", artifact=artifact
    )
    belief = CheckpointRef(
        experiment="belief", job="base", seed=1, role="sampler", name="final", artifact=artifact
    )
    seating: Seating = Seating(
        name="composed",
        players=(
            CheckpointSlot(),
            compose(
                Ensemble(
                    members=(
                        Final(experiment="rival", job="base", seed=2),
                        Final(
                            policy=PolicyKey(
                                experiment="belief", job="base", seed=1, role="sampler"
                            )
                        ),
                    )
                )
            ),
        ),
        configs=({},),
        games=3,
    )
    benchmark = Benchmark(
        name="policies", environment=job("unused", None).tournament.environment, seatings=(seating,)
    )
    assert benchmark.experiments == frozenset({"rival", "belief"})
    lines = (
        Line(
            name="base@0", experiment="league", job="base", seed=0, checkpoints=(held,), ended=True
        ),
        Line(
            name="base@2",
            experiment="rival",
            job="base",
            seed=2,
            checkpoints=(opponent,),
            ended=True,
        ),
        Line(
            name="base@1/roles/sampler",
            experiment="belief",
            job="base",
            seed=1,
            role="sampler",
            checkpoints=(belief,),
            ended=True,
        ),
    )
    seated = seating.resolve(held, lines, experiment="league")
    assert isinstance(seated, Seated)
    (match,) = seating.matches(held, seated)
    assert [ref.key for ref in player_checkpoints(match.players[1])] == [opponent.key, belief.key]
    assert match.games == 3
    # Defaults inside fixed wrappers bind using the same policy lines.
    unresolved = CheckpointRef(experiment="rival", job="base", name="final", artifact=artifact)
    fixed = compose(Ensemble(members=(checkpoint_player(unresolved),)))
    bound = TournamentRun(tournament=benchmark, entrants=(fixed,)).bind(lines, experiment="league")
    (ref,) = player_checkpoints(bound.entrants[0])
    assert ref.key == opponent.key
    assert ref.line == "rival/base@2"
