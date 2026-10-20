"""Tournaments end to end: benchmarks held on the go and in batch, round robins split across runs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from etude.eval.match import agent, environment
from etude.eval.query import connect
from etude.eval.records import Agent
from etude.eval.tournament import benchmark, hold, round_robin, standings
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Clock
from etude.run.storage import DirectoryStorage
from etude.train.run import track
from tests.eval.games import play
from tests.run.storage import MeasuredStorage

WORLD = environment("duel", config={"world": "w6"})
DEALS = [{"deal": deal} for deal in range(3)]


def scripted(tmp_path: Path, name: str, strength: int) -> Agent:
    script = tmp_path / "players" / f"{name}.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"STRENGTH = {strength}\n")
    return agent(name, code=[script], config={"strength": strength})


def trained(tmp_path: Path, bucket: DirectoryStorage) -> list[ArtifactRef]:
    """Three checkpoints of one training run, delivered to the bucket."""
    run = track(
        experiment="ppo",
        run="ppo-1",
        clocks=[Clock(name="updates", kind="step", meaning="optimizer updates")],
        directory=tmp_path / "trainer",
        storages=[bucket],
    )
    saved = []
    for ordinal in (1, 2, 3):
        model = tmp_path / "latest.pt"
        model.write_bytes(f"weights {ordinal}".encode())
        saved.append(run.checkpoint(model, updates=100 * ordinal))
    run.finish(timeout=30)
    return saved


def test_checkpoints_held_on_the_go_then_a_benchmark_added_in_batch(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    checkpoints = [
        agent(f"ppo@{ordinal}", checkpoint=ref, config={"strength": 2 * ordinal - 1})
        for ordinal, ref in enumerate(trained(tmp_path, bucket), start=1)
    ]
    heuristic, greedy = scripted(tmp_path, "heuristic", 0), scripted(tmp_path, "greedy", 2)
    progress = benchmark(
        "progress", environment=WORLD, conditions=DEALS, games=4, benchmarks=[heuristic, greedy]
    )

    def on_the_go(entrant: Agent, measured: MeasuredStorage) -> int:
        # Each checkpoint's eval run starts on its own machine, with an empty disk.
        held = hold(
            progress, enter=[entrant], play=play, storages=[measured],
            directory=tmp_path / "machines" / entrant.name,
        )  # fmt: skip
        return held.played

    assert on_the_go(checkpoints[0], MeasuredStorage(bucket)) == 2 * len(DEALS)
    later = [MeasuredStorage(bucket), MeasuredStorage(bucket)]
    with ThreadPoolExecutor(2) as pool:
        played = list(pool.map(on_the_go, checkpoints[1:], later))
    assert played == [2 * len(DEALS)] * 2
    # A fixed cost whatever the tournament's size: one checkpoint more in it changes nothing.
    second, third = ((m.listings, m.requests, m.uploads) for m in later)
    assert second == third
    # tournament.json; listings of entrants, benchmarks and the run's own matches.
    assert (second[0], second[1]) == (3, 1)

    puct = scripted(tmp_path, "puct", 4)
    batch = MeasuredStorage(bucket)
    added = hold(progress, add_benchmarks=[puct], play=play, storages=[batch],
                 directory=tmp_path / "machines" / "batch")  # fmt: skip
    # Only the new benchmark's matches, against every checkpoint entered so far.
    assert (added.played, added.skipped) == (3 * len(DEALS), 0)

    found = standings(connect(bucket), "progress", table="plays", value="won")
    assert found.complete and found.scheduled == 3 * 3 * len(DEALS)
    field = [entry for entry in found.entries if entry.opponent is None]
    assert [entry.name for entry in field] == ["ppo@1", "ppo@2", "ppo@3"]
    assert [
        entry.checkpoint and (entry.checkpoint.run, entry.checkpoint.at) for entry in field
    ] == [
        ("ppo-1", {"updates": 100.0}),
        ("ppo-1", {"updates": 200.0}),
        ("ppo-1", {"updates": 300.0}),
    ]
    # Stronger checkpoints win more against the same benchmarks.
    assert field[0].value < field[1].value < field[2].value
    against = {
        (entry.name, entry.opponent): entry.matches
        for entry in found.entries
        if entry.opponent is not None
    }
    assert len(against) == 9 and set(against.values()) == {len(DEALS)}


def test_a_batch_hold_skips_what_any_eval_run_delivered_by_listing_them(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    entrants = [scripted(tmp_path, f"s{strength}", strength) for strength in range(3)]
    progress = benchmark("progress", environment=WORLD, conditions=DEALS, games=2,
                         benchmarks=[scripted(tmp_path, "anchor", 1)])  # fmt: skip
    for each in entrants:
        hold(progress, enter=[each], play=play, storages=[bucket], directory=tmp_path / each.name)
    measured = MeasuredStorage(bucket)
    again = hold(progress, play=play, storages=[measured], directory=tmp_path / "batch")
    assert again.played == 0
    # Entrants, benchmarks, the tournament's runs, each of its 3 eval runs, and its own.
    assert measured.listings == 3 + 3 + 1


def test_a_round_robin_held_as_two_shares_stands_as_one(tmp_path: Path) -> None:
    finals = [scripted(tmp_path, f"final{strength}", strength) for strength in range(4)]
    league = round_robin("finals", environment=WORLD, conditions=DEALS, games=4)
    split, whole = DirectoryStorage(tmp_path / "split"), DirectoryStorage(tmp_path / "whole")
    halves = [
        hold(
            league,
            enter=finals,
            play=play,
            share=(k, 2),
            storages=[split],
            directory=tmp_path / f"machine-{k}",
        )  # fmt: skip
        for k in (0, 1)
    ]
    one = hold(league, enter=finals, play=play, workers=2, storages=[whole],
               directory=tmp_path / "machine")  # fmt: skip
    assert halves[0].name != halves[1].name
    # Every unordered pair once per deal.
    assert sum(half.played for half in halves) == one.played == 6 * len(DEALS)

    def table(storage: DirectoryStorage) -> list[tuple[str, str | None, float, int]]:
        found = standings(connect(storage), "finals", table="plays", value="won")
        assert found.complete
        return [(e.name, e.opponent, e.value, e.matches) for e in found.entries]

    assert table(split) == table(whole)
    # Ordered by value against the field, best first.
    assert [name for name, opponent, *_ in table(whole) if opponent is None] == [
        "final3",
        "final2",
        "final1",
        "final0",
    ]


def test_a_tournament_of_the_same_name_with_other_rules_is_refused(tmp_path: Path) -> None:
    entrant = scripted(tmp_path, "s", 1)
    first = round_robin("finals", environment=WORLD, conditions=DEALS, games=4)
    hold(first, enter=[entrant], play=play, directory=tmp_path)
    other = round_robin("finals", environment=WORLD, conditions=DEALS, games=8)
    with pytest.raises(EtudeError) as refused:
        hold(other, enter=[entrant], play=play, directory=tmp_path)
    assert refused.value.code == "tournament_conflict"
