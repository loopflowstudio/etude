"""Players keep their identities across the rename from agents, and old records still load."""

from __future__ import annotations

from hashlib import sha256

import pytest
from pydantic import ValidationError

from etude.eval.records import (
    Aligned,
    At,
    Benchmark,
    CheckpointKey,
    CheckpointRef,
    Code,
    Earlier,
    Environment,
    Final,
    Fraction,
    Gap,
    Inference,
    Line,
    Match,
    Model,
    RoundRobin,
    Seated,
    Seating,
    TournamentRun,
)
from etude.run.records import ArtifactRef

DIGEST = "0" * 64
WORLD = Environment(name="world", files="1" * 64)
U500 = CheckpointRef(
    key=CheckpointKey(run="ppo-1", name="updates-500.pt"),
    artifact=ArtifactRef(uri="s3://b/updates-500.pt", sha256=DIGEST),
)
SEARCH = Inference(config={"sims": 64})
HEURISTIC = Code(name="heuristic")

# Written by etude-sdk at 2eb32d5, before players were named.
OLD_MODEL = (
    '{"key":{"checkpoint":{"run":"ppo-1","name":"updates-500.pt","address":null},"code":null,'
    '"recorded":null,"config":{"sims":64}},"name":"updates-500.pt","artifact":{"uri":'
    '"s3://b/updates-500.pt","sha256":"' + DIGEST + '","size_bytes":null,"version":null,'
    '"state":"unverified","media_type":"application/octet-stream"},"version":{"commit":null,'
    '"source":null}}'
)
OLD_CODE = (
    '{"key":{"checkpoint":null,"code":"heuristic","recorded":null,"config":{}},'
    '"name":"heuristic","artifact":null,"version":{"commit":null,"source":null}}'
)
OLD_MATCH = (
    '{"agents":[' + OLD_MODEL + "," + OLD_CODE + '],"groups":{"all":[0,1]},'
    '"conditions":{"deal":1},"games":8}'
)
OLD_BENCHMARK = (
    '{"tournament":{"name":"ladder","environment":{"name":"world","files":"' + "1" * 64 + '",'
    '"config":{}},"matchups":[{"name":"direct vs heuristic","players":[{"config":{"sims":64},'
    '"version":{"commit":null,"source":null}},' + OLD_CODE + '],"conditions":[{"deal":1}],'
    '"games":8,"tags":[]}]},"checkpoints":[],"entrants":[],"share":[0,1]}'
)
OLD_ROUND_ROBIN = (
    '{"tournament":{"name":"rr","environment":{"name":"world","files":"' + "1" * 64 + '",'
    '"config":{}},"conditions":[{"deal":1}],"games":2},"checkpoints":[],"entrants":['
    + OLD_MODEL
    + ","
    + OLD_CODE
    + '],"share":[0,1]}'
)


def test_renaming_changes_no_match_key_or_matchup_definition() -> None:
    match = Match(players=(SEARCH.model(U500), HEURISTIC), conditions={"deal": 1}, games=8)
    # Pinned at 2eb32d5: a changed key would replay every stored match.
    assert match.key == "a64354de99bed9ed3bfc72755e7af6774877e28a383306be8e512561e6c793c8"
    matchup = Seating(
        name="direct vs heuristic", players=(SEARCH, HEURISTIC), conditions=({"deal": 1},), games=8
    )
    assert matchup.definition == "9185c1177e55b1aa269d7d099aaa4a02d2a013dc780343081b531dba66c1ea91"


def test_a_match_stored_with_agents_loads_as_the_same_players() -> None:
    old = Match.model_validate_json(OLD_MATCH)
    now = Match(players=(SEARCH.model(U500), HEURISTIC), conditions={"deal": 1}, games=8)
    assert old == now
    assert Match.model_validate_json(now.model_dump_json()) == now
    assert "agents" not in now.model_dump_json()


def test_tournament_files_stored_with_agents_load_as_players() -> None:
    benchmark = TournamentRun.model_validate_json(OLD_BENCHMARK)
    matchup = Seating(
        name="direct vs heuristic", players=(SEARCH, HEURISTIC), conditions=({"deal": 1},), games=8
    )
    assert benchmark.tournament == Benchmark(name="ladder", environment=WORLD, seatings=(matchup,))
    held = TournamentRun.model_validate_json(OLD_ROUND_ROBIN)
    assert isinstance(held.tournament, RoundRobin)
    assert held.entrants == (SEARCH.model(U500), HEURISTIC)


def test_etude_sdk_0_2_players_load_and_equal_no_player_since() -> None:
    sha = sha256(b"weights").hexdigest()
    old = Match.model_validate(
        {
            "agents": [
                {"name": "updates-100.pt", "kind": "checkpoint", "artifact": sha,
                 "uri": "s3://models/updates-100.pt", "config": {"search": 0}},
                {"name": "heuristic", "kind": "code", "artifact": None, "uri": None,
                 "code": DIGEST, "config": {}},
            ]
        }
    )  # fmt: skip
    model, code = old.players
    placeholder = Seating.model_validate(
        {"name": "vs heuristic", "players": [{"config": {"search": 0}, "code": DIGEST},
                                             {"name": "heuristic", "kind": "code", "code": DIGEST}],
         "conditions": [{"deal": 1}], "games": 2}
    )  # fmt: skip
    assert placeholder.players[0] == Inference.model_validate(
        {"config": {"search": 0}, "version": {"source": DIGEST}}
    )
    assert isinstance(placeholder.players[1], Code)
    assert isinstance(model, Model) and isinstance(code, Code)
    assert model.inference.config == {"search": 0}
    assert model.checkpoint.artifact.uri == "s3://models/updates-100.pt"
    now = Inference(config={"search": 0}).model(
        CheckpointRef(
            key=CheckpointKey(run="ppo-1", name="updates-100.pt"),
            artifact=ArtifactRef(uri="s3://models/updates-100.pt", sha256=sha),
        )
    )
    assert model.key != now.key and code.key != HEURISTIC.key


def test_a_model_needs_its_checkpoint_digest() -> None:
    with pytest.raises(ValidationError, match="model_checkpoint_requires_digest"):
        CheckpointRef(key=U500.key, artifact=ArtifactRef(uri="s3://b/updates-500.pt"))


def _ref(run: str, updates: float) -> CheckpointRef:
    return CheckpointRef(
        key=CheckpointKey(run=run, name=f"updates-{updates:g}.pt"),
        artifact=ArtifactRef(uri=f"file:///{run}/{updates:g}.pt", sha256=DIGEST),
        at={"updates": updates},
        line=run,
    )


def _line(
    run: str, job: str, *positions: float, ended: bool = False, seed: int | None = None
) -> Line:
    return Line(
        name=run,
        seed=seed,
        job=job,
        launch=job,
        ended=ended,
        checkpoints=tuple(_ref(run, at) for at in positions),
    )


def _found(seated: Seated) -> list[tuple[CheckpointRef | None, int]]:
    """Each pairing's second seat and games."""
    return [(each.seats[1], each.games) for each in seated.pairings]


def test_a_point_splits_its_games_over_every_pair_of_seeds() -> None:
    direct = Inference(config={"search": 0})
    large = [_line(f"l{seed}", "large", 100, seed=seed) for seed in (1, 2)]
    small = [_line(f"s{seed}", "small", 100, seed=seed) for seed in (3, 1, 2)]
    lines = [*large, *small]
    aligned = Seating(
        name="s",
        players=(direct, Aligned(clock="updates", job="small", inference=direct)),
        conditions=({"deal": 0},),
        games=10,
    )
    # 2 × 3 pairs share 10 games: the four lowest by seed get two, the rest one.
    by_seed = [s.checkpoints[0] for s in sorted(small, key=lambda line: line.order)]
    first, second = (aligned.resolve(each.checkpoints[0], lines) for each in large)
    assert isinstance(first, Seated) and isinstance(second, Seated)
    assert _found(first) == [(by_seed[0], 2), (by_seed[1], 2), (by_seed[2], 2)]
    assert _found(second) == [(by_seed[0], 2), (by_seed[1], 1), (by_seed[2], 1)]
    # The point holds every game; its own line and a fixed player count as one seed.
    own = Seating(
        name="o",
        players=(direct, At(clock="updates", value=100, inference=direct)),
        conditions=({"deal": 0},),
        games=3,
    )
    fixed = Seating(name="f", players=(direct, HEURISTIC), conditions=({"deal": 0},), games=3)
    for seating in (own, fixed):
        held = [seating.resolve(each.checkpoints[0], lines) for each in large]
        assert [
            [games for _, games in _found(each)] for each in held if isinstance(each, Seated)
        ] == [
            [2],
            [1],
        ]
    # A seed of the other job that may still save there holds the point back.
    waiting = [*large, *small, _line("s4", "small", seed=4)]
    assert isinstance(aligned.resolve(large[0].checkpoints[0], waiting), Gap)


def test_one_seed_plays_the_same_pair_at_every_point_and_hold() -> None:
    direct = Inference(config={"search": 0})
    large = [_line(f"l{seed}", "large", 100, 200, seed=seed) for seed in (1, 2, 3)]
    small = [_line(f"s{seed}", "small", 100, 200, seed=seed) for seed in (1, 2, 3)]
    lines = [*large, *small]
    one = Seating(
        name="s",
        players=(direct, Aligned(clock="updates", job="small", inference=direct)),
        conditions=({"deal": 0},),
        games=10,
        seeds="one",
    )

    def played(given: list[Line]) -> list[tuple[CheckpointRef, CheckpointRef | None, int]]:
        found = [(held, one.resolve(held, given)) for line in large for held in line.checkpoints]
        return [
            (held, other, games)
            for held, seated in found
            if isinstance(seated, Seated)
            for other, games in _found(seated)
        ]

    chosen = played(lines)
    # One seed per side at each point, all its games, and the same pair at both points.
    assert [games for *_, games in chosen] == [10, 10]
    assert len({(held.line, other.line if other else None) for held, other, _ in chosen}) == 1
    assert played(list(reversed(lines))) == chosen
    assert one.definition != one.model_copy(update={"seeds": "every"}).definition


def test_a_relative_seat_finds_an_exact_position_or_says_whether_one_may_still_come() -> None:
    direct = Inference(config={"search": 0})

    def seating(seat: Earlier | Fraction | Final | Aligned) -> Seating:
        return Seating(name="s", players=(direct, seat), conditions=({"deal": 0},), games=1)

    small = _line("s1", "small", 63, 90)
    large = _line("l1", "large", 90, 180)
    lines = [small, large]
    # 90 × 0.7 is 62.99999999999999 in binary, and still the checkpoint at 63.
    found = seating(Fraction(clock="updates", value=0.7, inference=direct)).resolve(
        small.checkpoints[1], lines
    )
    assert isinstance(found, Seated) and _found(found) == [(small.checkpoints[0], 1)]
    # Passed without saving there: never. Ahead of the line: not yet, until it ends.
    earlier = seating(Earlier(clock="updates", by=10, inference=direct))
    assert isinstance(gap := earlier.resolve(small.checkpoints[1], lines), Gap)
    assert (gap.missing, gap.position) == ("never", 80)
    aligned = seating(Aligned(clock="updates", job="small", inference=direct))
    assert isinstance(gap := aligned.resolve(large.checkpoints[1], lines), Gap)
    assert (gap.missing, gap.job) == ("not_yet", "small")
    ended = [small.model_copy(update={"ended": True}), large]
    assert isinstance(gap := aligned.resolve(large.checkpoints[1], ended), Gap)
    assert gap.missing == "never"
    # Held for the job it names, it plays that job: here the held checkpoint against itself.
    selfplay = aligned.resolve(small.checkpoints[0], lines)
    assert isinstance(selfplay, Seated) and _found(selfplay) == [(small.checkpoints[0], 1)]
    final = seating(Final(job="small", inference=direct))
    assert isinstance(final.resolve(large.checkpoints[0], lines), Gap)
    done = final.resolve(large.checkpoints[0], ended)
    assert isinstance(done, Seated) and _found(done) == [(small.checkpoints[1], 1)]
    (match,) = final.matches(large.checkpoints[0], done)
    assert match.players[1] == direct.model(small.checkpoints[1])
    with pytest.raises(ValidationError, match="relative_seat_needs_an_inference_seat"):
        Seating(
            name="s",
            players=(Earlier(clock="updates", by=1, inference=direct), HEURISTIC),
            conditions=({"deal": 0},),
            games=1,
        )


def test_an_at_or_before_seat_takes_the_latest_checkpoint_once_none_nearer_can_come() -> None:
    direct = Inference(config={"search": 0})

    def seating(seat: Fraction) -> Seating:
        return Seating(name="s", players=(direct, seat), conditions=({"deal": 0},), games=1)

    half = seating(Fraction(clock="updates", value=0.5, at_or_before=True, inference=direct))
    exact = seating(Fraction(clock="updates", value=0.5, inference=direct))
    line = _line("l1", "large", 100, 300, 400, 700)
    # Half of 700 is 350: none sits there, and the latest at or before it is 300.
    found = half.resolve(line.checkpoints[3], [line])
    assert isinstance(found, Seated) and _found(found) == [(line.checkpoints[1], 1)]
    assert isinstance(gap := exact.resolve(line.checkpoints[3], [line]), Gap)
    assert gap.missing == "never"
    # In another job's line that has not saved past 350, a save at 340 could still come.
    other = seating(
        Fraction(clock="updates", value=0.5, at_or_before=True, job="small", inference=direct)
    )
    small = _line("s1", "small", 100, 300)
    assert isinstance(gap := other.resolve(line.checkpoints[3], [line, small]), Gap)
    assert (gap.missing, gap.position, gap.at_or_before) == ("not_yet", 350, True)
    # Once it saves past the target or ends, nothing nearer can come.
    for done in (_line("s1", "small", 100, 300, 500), small.model_copy(update={"ended": True})):
        found = other.resolve(line.checkpoints[3], [line, done])
        assert isinstance(found, Seated) and _found(found) == [(done.checkpoints[1], 1)]
    # Nothing at or before the target, once past it: never.
    late = _line("s1", "small", 400)
    assert isinstance(gap := other.resolve(line.checkpoints[3], [line, late]), Gap)
    assert gap.missing == "never"
