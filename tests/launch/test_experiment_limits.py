"""Stored experiment spending limits on separate submissions and launcher connections."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch.client import limit, limits, queue
from etude.launch.database import launcher_file
from etude.launch.provisioner.machines import check_creation
from etude.launch.records import Launch, Offer, OnDemand, Pool
from etude.launch.scheduler.loop import Scheduler
from etude.launch.schemas import Packet
from etude.launch.server import LaunchInput, SubmissionRequest, _answer
from etude.records import RunRecord
from etude.spec import Job, RunRequest
from tests.launch.counting import Counting, Counts, query_plan
from tests.launch.fake import L4, bench, open_database
from tests.launch.test_source import git
from tests.launch.test_source import repository as repository


def _request(
    name: str,
    *,
    usd: str = "1",
    experiment: str = "ppo",
    repo: Path | None = None,
    revision: str | None = None,
) -> RunRequest:
    return RunRequest(
        id=name,
        job=Job(
            command="train",
            experiment=experiment,
            max_hours=1,
            max_usd=Decimal(usd),
            repo=str(repo) if repo is not None else None,
            revision=revision,
        ),
    )


def test_queue_counts_prior_runs_and_owned_work_can_pass_a_full_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr("etude.launch.client._client", lambda: b.scheduler)
    (first,) = queue(_request("first"), rent="l4")
    assert b.scheduler.set_limit("ppo", Decimal(1)).committed_usd == 1
    b.scheduler.add_pool(Pool(name="lab"))
    b.scheduler.set_default("lab")
    assert queue(_request("owned")) == ("ppo/owned",)
    b.scheduler.set_default(rent="l4")
    with pytest.raises(LauncherError, match=r"\$1.00 committed.*asks \$1.00.*limit set ppo"):
        queue(_request("second"))
    assert {r.id for r in b.store.runs()} == {first, "ppo/owned"}
    assert b.store.launches("ppo/second")[-1].state == "failed"
    reopened = Scheduler(open_database(launcher_file(tmp_path)), {}, clock=b.clock)
    assert reopened.limits()[0].committed_usd == 1
    reopened.set_limit("ppo", Decimal(2))
    assert queue(_request("second")) == ("ppo/second",)
    assert reopened.limits()[0].committed_usd == 2
    assert b.service.creates == 0
    reopened.unset_limit("ppo")
    assert reopened.limit("ppo") is None
    with pytest.raises(LauncherError, match="has no spending limit"):
        reopened.unset_limit("ppo")


@pytest.mark.parametrize(
    ("experiment", "usd", "message"),
    [
        ("ppo.py", "0", "label"),
        ("../ppo", "0", "label"),
        ("ppo", "-1", "nonnegative"),
        ("ppo", "NaN", "finite"),
        ("ppo", "Infinity", "finite"),
    ],
)
def test_limit_validation(tmp_path: Path, experiment: str, usd: str, message: str) -> None:
    b = bench(tmp_path)
    with pytest.raises(LauncherError, match=message):
        b.scheduler.set_limit(experiment, Decimal(usd))
    assert b.scheduler.limits() == ()


@pytest.mark.parametrize("command", ["launch", "queue"])
@pytest.mark.parametrize("switch", [("--max-usd", "1"), ("--max-usd=1",)])
@pytest.mark.parametrize("before_file", [False, True])
def test_removed_limit_flag_is_refused_on_an_eval_only_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    switch: tuple[str, ...],
    before_file: bool,
) -> None:
    file = tmp_path / "ppo.py"
    file.write_text("def eval_jobs(state):\n    return []\n")
    args = [*switch, str(file)] if before_file else [str(file), *switch]
    assert main([command, *args]) == 1
    assert "etude limit set EXPERIMENT --max-usd D" in capsys.readouterr().err


@pytest.mark.parametrize(("rate", "displayed"), [("0.59", "~$0.10"), ("0.0024", "~$0.0004")])
def test_cli_lists_retained_estimated_commitments_without_provider_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rate: str,
    displayed: str,
) -> None:
    b = bench(tmp_path, usd_per_hour=rate)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    (run,) = b.scheduler.launch(_request("finished"), rent="l4")
    b.run(601)
    assert b.scheduler.read(run).done
    assert main(["limit", "set", "ppo", "--max-usd", "0.30"]) == 0
    assert capsys.readouterr().out == f"ppo  limited to $0.30; {displayed} committed\n"
    assert main(["limits"]) == 0
    assert capsys.readouterr().out == f"ppo  {displayed} of $0.30 committed\nlast loop: never\n"
    (configured,) = limits()
    assert limit("ppo") == configured
    assert configured.estimated
    with pytest.raises(LauncherError) as refused:
        b.scheduler.launch(_request("second", usd="0.30"), rent="l4")
    assert f"{displayed} committed and this launch asks $0.30" in str(refused.value)
    assert b.service.creates == 1
    assert main(["limit", "unset", "ppo"]) == 0
    assert limits() == ()
    assert main(["limit", "unset", "ppo"]) == 1
    assert "has no spending limit" in capsys.readouterr().err
    assert main(["limit", "set", "ppo", "--max-usd", "two"]) == 1
    assert "--max-usd must be a dollar amount" in capsys.readouterr().err


def test_limits_requires_an_initialized_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    assert main(["limits"]) == 1
    assert "is not initialized; run etude database upgrade" in capsys.readouterr().err
    assert not launcher_file(tmp_path).exists()


def test_limits_shows_in_flight_dollars_and_warns_when_the_loop_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path, usd_per_hour="0.000001")
    b.clock.now = datetime.now(UTC)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    assert main(["limit", "set", "ppo", "--max-usd", "0.005"]) == 0
    assert "limited to $0.005" in capsys.readouterr().out
    b.scheduler.queue(_request("queued", usd="0.001"), rent="l4")
    b.scheduler.prepare_launch((_request("preparing", usd="0.0000001"),), rent="l4")
    assert main(["limit", "set", "ppo", "--max-usd", "0.005"]) == 0
    assert capsys.readouterr().out == (
        "ppo  limited to $0.005; $0.001 committed ($0.0000001 in flight)\n"
    )
    assert main(["limits"]) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "ppo  $0.001 of $0.005 committed ($0.0000001 in flight)\nlast loop: never\n"
    )
    assert "work is waiting; no server loop has run recently" in captured.err
    with pytest.raises(LauncherError, match=r"limited to \$0.005: .* asks \$0.0041"):
        b.scheduler.queue(_request("above", usd="0.0041"), rent="l4")
    with pytest.raises(LauncherError) as refused:
        b.scheduler.queue(_request("held", usd="0.004"), rent="l4")
    assert "$0.001 committed ($0.0000001 in flight)" in str(refused.value)
    assert "Retry after in-flight launches settle" in str(refused.value)


def test_quote_and_launch_refuse_before_source_or_provider_calls(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.set_limit("ppo", Decimal(0))
    # The nonexistent checkout would fail source preparation if the limit did not refuse first.
    run = _request("new")
    broken = run.model_copy(
        update={"job": run.job.model_copy(update={"repo": str(tmp_path / "missing")})}
    )
    count = Counting(b.store, b.service)
    with count.record():
        with pytest.raises(LauncherError, match="limited to"):
            b.scheduler.quote([broken], rent="l4")
        assert b.store.launches("ppo/new") == ()
        with pytest.raises(LauncherError, match="limited to"):
            b.scheduler.launch(broken, rent="l4")
        assert count.snapshot().provider == count.snapshot().git == count.snapshot().bucket == 0
    assert not b.store.sessions() and not b.store.runs()
    b.scheduler.set_limit("ppo", Decimal(1))
    assert b.scheduler.quote([run], rent="l4").limit_usd == 1
    assert b.scheduler.limits()[0].committed_usd == 0
    b.service.refuse = 1
    with pytest.raises(LauncherError, match="provider refused"):
        b.scheduler.launch(run, rent="l4")
    assert b.scheduler.limits()[0].committed_usd == 0
    assert not b.store.unsettled_sessions()
    assert b.scheduler.launch(run, rent="l4") == ("ppo/new",)
    assert b.service.creates == 2


def test_cli_resume_replaces_its_prior_commitment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.setattr("etude.launch.client._client", lambda: b.scheduler)
    file = tmp_path / "ppo.py"
    recipe = """from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command

def training_jobs() -> list[TrainingJob]:
    return [TrainingJob(name='base', train=train_command, runtime=RUNTIME,
                        config=Command(command='true', text='{}'),
                        repo=None, max_hours=1, max_usd=1)]
"""
    file.write_text(recipe)
    argv = ["queue", str(file), "--rent", "l4", "--source", str(tmp_path / "tracking")]
    assert main(argv) == 0
    (prior,) = b.store.runs()
    file.write_text(recipe.replace("max_usd=1", "max_usd=2"))
    b.scheduler.set_limit("ppo", Decimal(1))
    assert main([*argv, "--resume"]) == 1
    assert "committed and this launch asks $1.00" in capsys.readouterr().err
    assert b.store.runs() == (prior,)
    b.scheduler.set_limit("ppo", Decimal(2))
    assert main([*argv, "--resume"]) == 0
    (resumed,) = b.store.runs()
    assert resumed.id == prior.id and resumed.launch.spec.max_usd == 2
    assert b.scheduler.limits()[0].committed_usd == 2
    # Resuming at the same cap asks nothing more, so a lowered limit does not refuse it.
    b.scheduler.set_limit("ppo", Decimal(1))
    assert main([*argv, "--resume"]) == 0


def test_server_reserves_before_client_publication_and_releases_a_failed_intent(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    backend = b.scheduler
    submission = LaunchInput(runs=(_request("remote"),), pool=None, rent="l4", queued=True)
    prepare = SubmissionRequest(operation="prepare_launch", submission=submission)
    backend.set_limit("ppo", Decimal(0))
    with pytest.raises(LauncherError, match="limited to"):
        _answer(backend, prepare)
    backend.set_limit("ppo", Decimal(1))
    launch = _answer(backend, prepare)
    assert isinstance(launch, Launch)
    assert backend.limits()[0].committed_usd == 1
    backend.fail_launch(launch.id, "client could not publish source")
    assert backend.limits()[0].committed_usd == 0
    launch = _answer(backend, prepare)
    assert isinstance(launch, Launch)
    runs = launch.addressed(submission.runs)
    run = runs[0]
    changed = run.model_copy(update={"job": run.job.model_copy(update={"max_usd": Decimal(2)})})
    with pytest.raises(LauncherError, match="spending differs"):
        _answer(
            backend,
            SubmissionRequest(
                operation="launch",
                submission=submission.model_copy(update={"launch": launch.id, "runs": (changed,)}),
            ),
        )
    assert not b.store.runs()
    assert backend.limits()[0].committed_usd == 0
    launch = _answer(backend, prepare)
    assert isinstance(launch, Launch)
    b.clock.advance(299)
    assert (
        _answer(
            backend,
            SubmissionRequest(
                operation="missing_sources",
                submission=submission.model_copy(update={"launch": launch.id}),
            ),
        )
        == ()
    )
    b.clock.advance(2)
    assert _answer(
        backend,
        SubmissionRequest(
            operation="launch",
            submission=submission.model_copy(
                update={"launch": launch.id, "runs": launch.addressed(submission.runs)}
            ),
        ),
    ) == ("ppo/remote",)
    assert backend.limits()[0].committed_usd == 1


def test_abandoned_preparation_expires_and_fences_the_late_ssh_client(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.set_limit("ppo", Decimal(1))
    other = Scheduler(open_database(launcher_file(tmp_path)), {}, clock=b.clock)
    run = _request("late")
    launch = b.scheduler.prepare_launch((run,), rent="l4")
    assert launch.expires_at is not None
    b.clock.advance(299.999999)
    assert other.limits()[0].reserved_usd == 1
    with pytest.raises(LauncherError, match="reconciling"):
        other.queue(run, rent="l4")
    with pytest.raises(LauncherError, match="limited to"):
        other.queue(_request("different"), rent="l4")

    b.clock.now = launch.expires_at
    assert other.limits()[0].committed_usd == 0
    assert other.queue(run, rent="l4") == ("ppo/late",)
    count = Counting(b.store, b.service)
    with count.record(), pytest.raises(LauncherError, match="reservation expired"):
        _answer(
            b.scheduler,
            SubmissionRequest(
                operation="launch",
                submission=LaunchInput(
                    runs=launch.addressed((run,)), pool=None, rent="l4", launch=launch.id
                ),
            ),
        )
    assert count.snapshot().provider == count.snapshot().bucket == count.snapshot().git == 0
    assert b.store.launch_evidence(launch.id).state == "failed"
    assert other.limits()[0].committed_usd == 1
    assert other.limits()[0].reserved_usd == 0


def test_abandoned_launch_logs_finish_after_retry_before_the_scheduler_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr("etude.launch.client._client", lambda: b.scheduler)
    b.scheduler.set_limit("ppo", Decimal(1))
    request = _request("retry")
    abandoned = b.scheduler.prepare_launch((request,), rent="l4")
    assert not b.scheduler.logs(run="ppo/retry").complete

    b.clock.advance(300)
    (run,) = b.scheduler.queue(request, rent="l4")
    b.store.cancel(b.store.run(run), b.clock())
    assert b.scheduler.read(run).done
    count = Counting(b.store, b.service)
    with count.record():
        assert main(["logs", "--run", run, "--follow", "--timeout", "1"]) == 0
    output = capsys.readouterr().out
    assert f"launch {abandoned.id}: expired" in output
    assert "admitted" in output and "incomplete" not in output
    assert count.snapshot().provider == count.snapshot().bucket == 0
    assert b.store.launch_evidence(abandoned.id).state == "preparing"

    b.scheduler.tick()
    b.scheduler.tick()
    assert b.store.launch_evidence(abandoned.id).state == "failed"
    snapshot = b.scheduler.logs(run=run)
    assert snapshot.complete
    assert [d.message for d in snapshot.diagnostics if d.launch == abandoned.id].count(
        "reservation expired"
    ) == 1


@pytest.mark.parametrize(("upload_seconds", "limited"), [(151, False), (151, True), (301, True)])
def test_staging_renews_admission_between_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: tuple[Path, Path],  # noqa: F811
    upload_seconds: int,
    limited: bool,
) -> None:
    b = bench(tmp_path)
    repo, _ = repository
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-m", "Second source archive")
    second = git(repo, "rev-parse", "HEAD")
    runs = (
        _request("first", repo=repo, revision=first),
        _request("second", repo=repo, revision=second),
    )
    if limited:
        b.scheduler.set_limit("ppo", Decimal(2))
    other = Scheduler(open_database(launcher_file(tmp_path)), {}, clock=b.clock)
    stage = b.service.stage
    expires = upload_seconds > 300

    def slow_upload(repo: str, commit: str) -> None:
        stage(repo, commit)
        b.clock.advance(upload_seconds)
        if expires:
            assert other.queue(_request("replacement"), rent="l4") == ("ppo/replacement",)
        elif limited and len(b.service.staged) == 2:
            with pytest.raises(LauncherError, match=r"\$2.00 in flight"):
                other.queue(_request("replacement"), rent="l4")

    monkeypatch.setattr(b.service, "stage", slow_upload)
    if expires:
        with pytest.raises(LauncherError, match="reservation expired"):
            b.scheduler.launch(runs, rent="l4")
        assert b.service.staged == [(min(first, second), 0)]
        assert b.service.creates == 0
        assert not b.store.sessions()
        assert other.limits()[0].committed_usd == 1
    else:
        assert b.scheduler.launch(runs, rent="l4") == ("ppo/first", "ppo/second")
        assert b.service.staged == [(commit, 0) for commit in sorted((first, second))]
        assert b.service.creates == 2
    if limited:
        assert other.limits()[0].reserved_usd == 0


def test_slow_staging_renews_the_reservation_before_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: tuple[Path, Path],  # noqa: F811
) -> None:
    b = bench(tmp_path)
    repo, _ = repository
    b.scheduler.set_limit("ppo", Decimal(1))
    other = Scheduler(open_database(launcher_file(tmp_path)), {}, clock=b.clock)
    offer = b.service.offer

    def slow_offer() -> Offer:
        b.clock.advance(2)
        with pytest.raises(LauncherError, match=r"\$1.00 in flight"):
            other.queue(_request("replacement"), rent="l4")
        return offer()

    monkeypatch.setattr(b.service, "stage", lambda *_: b.clock.advance(299))
    monkeypatch.setattr(b.service, "offer", slow_offer)
    assert b.scheduler.launch(_request("slow", repo=repo, revision="HEAD"), rent="l4") == (
        "ppo/slow",
    )
    assert other.limits()[0].reserved_usd == 0
    assert b.service.creates == 1


def test_reusing_idle_machines_renews_admission_between_placements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path, max_machines=2)
    machines = {b.scheduler.rent("l4", 3) for _ in range(2)}
    b.scheduler.set_limit("ppo", Decimal(2))
    other = Scheduler(open_database(launcher_file(tmp_path)), {}, clock=b.clock)
    link = b.store.link_launch

    def link_after_delay(launch: str, session: str, *, acquired: bool = False) -> None:
        b.clock.advance(160)
        assert other.limits()[0].reserved_usd == 2
        link(launch, session, acquired=acquired)

    monkeypatch.setattr(b.store, "link_launch", link_after_delay)
    runs = b.scheduler.launch((_request("first"), _request("second")), rent="l4")
    assert {b.scheduler.attempts(run)[0].machine for run in runs} == machines
    assert b.service.creates == 2
    assert other.limits()[0].committed_usd == 2
    assert other.limits()[0].reserved_usd == 0


def test_unsettled_acquisition_keeps_admission_live_after_lease_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.set_limit("ppo", Decimal(1))
    other = Scheduler(open_database(launcher_file(tmp_path)), {}, clock=b.clock)
    create = b.service.create

    def slow_create(packet: Packet) -> str:
        b.clock.advance(299)
        check_creation()  # the supplying lock is renewed immediately before the provider call
        provider_id = create(packet)
        b.clock.advance(2)
        other.tick()
        assert other.limits()[0].reserved_usd == 1
        snapshot = other.logs(run="ppo/slow")
        (launch,) = snapshot.launches
        assert snapshot.launch_state(launch) == "admitting"
        assert not snapshot.complete
        return provider_id

    monkeypatch.setattr(b.service, "create", slow_create)
    (run,) = b.scheduler.launch(_request("slow"), rent="l4")
    assert b.scheduler.attempts(run)[0].added
    assert other.limits()[0].committed_usd == 1
    assert other.limits()[0].reserved_usd == 0
    (session,) = b.service.sessions.values()
    assert session.killed_at is None


def test_expired_admission_cannot_record_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.set_limit("ppo", Decimal(1))
    other = Scheduler(open_database(launcher_file(tmp_path)), {}, clock=b.clock)
    commit = b.store.launch

    def late_commit(
        records: Sequence[RunRecord], *, launch: str, clock: Callable[[], datetime]
    ) -> None:
        b.clock.advance(301)
        assert other.queue(_request("replacement"), rent="l4") == ("ppo/replacement",)
        commit(records, launch=launch, clock=clock)

    monkeypatch.setattr(b.store, "launch", late_commit)
    with pytest.raises(LauncherError, match="reservation expired"):
        b.scheduler.queue(_request("late"), rent="l4")
    assert [run.id for run in b.store.runs()] == ["ppo/replacement"]
    assert other.limits()[0].committed_usd == 1
    assert other.limits()[0].reserved_usd == 0


@pytest.mark.parametrize("first_wave", [0, 1])
def test_unknown_create_holds_reservation_until_the_rental_is_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_wave: int
) -> None:
    b = bench(tmp_path)
    ceiling = Decimal(first_wave + 1)
    b.scheduler.set_limit("ppo", ceiling)
    other = Scheduler(open_database(launcher_file(tmp_path)), {}, clock=b.clock)
    create = b.service.create

    def lose_last_reply(packet: Packet) -> str:
        if b.service.creates == first_wave:
            b.service.lose_reply = 1
        return create(packet)

    monkeypatch.setattr(b.service, "create", lose_last_reply)
    runs = tuple(_request(f"run{i}") for i in range(first_wave + 1))
    with pytest.raises(LauncherError, match="create reply unknown"):
        b.scheduler.launch(runs, rent="l4")
    assert not b.store.runs()
    assert b.store.launches("ppo/run0")[-1].state == "failed"
    b.clock.advance(301)
    with pytest.raises(LauncherError, match="reconciling"):
        other.queue(runs[-1], rent="l4")
    with pytest.raises(LauncherError, match="limited to"):
        other.queue(_request("would_overspend"), rent="l4")
    assert other.limits()[0].reserved_usd == ceiling
    b.scheduler.tick()
    b.scheduler.tick()
    assert not b.store.unsettled_sessions()
    assert other.limits()[0].reserved_usd == 0
    assert other.queue(runs[-1], rent="l4") == (f"ppo/run{first_wave}",)
    assert b.service.creates == first_wave + 1


def test_prepared_launch_uses_the_target_it_reserved(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.set_limit("ppo", Decimal(0))
    b.scheduler.add_pool(Pool(name="lab"))
    b.scheduler.set_default("lab")
    run = _request("owned")
    launch = b.scheduler.prepare_launch((run,))
    b.scheduler.set_default(rent="l4")
    (identity,) = b.scheduler.submit_launch(launch.id, launch.addressed((run,)), queued=True)
    assert b.scheduler.record(identity).launch.pool == "lab"
    assert b.scheduler.record(identity).launch.rent is None
    assert b.scheduler.limits()[0].committed_usd == 0


def test_two_launches_racing_for_the_last_dollars_create_one_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_ondemand(OnDemand(name="other", template=L4, max_machines=1))
    other = Scheduler(
        open_database(launcher_file(tmp_path)), {"runpod": lambda held: b.service}, b.clock
    )
    b.scheduler.set_limit("ppo", Decimal(1))
    create = b.service.create

    def interleave(packet: Packet) -> str:
        provider_id = create(packet)
        assert other.limits()[0].committed_usd == 1
        with pytest.raises(LauncherError, match="limited to"):
            other.launch(_request("second"), rent="other")
        return provider_id

    monkeypatch.setattr(b.service, "create", interleave)
    assert b.scheduler.launch(_request("first"), rent="l4") == ("ppo/first",)
    assert b.service.creates == 1
    assert other.limits()[0].committed_usd == 1


@pytest.mark.parametrize("change", ["set", "bill"])
def test_a_limit_set_or_bill_revised_during_create_holds_for_the_next_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    b = bench(tmp_path)
    other_store = open_database(launcher_file(tmp_path))
    other = Scheduler(other_store, {}, clock=b.clock)
    (finished,) = b.scheduler.launch(_request("finished"), rent="l4")
    b.run(601)
    (attempt,) = b.scheduler.attempts(finished)
    assert b.scheduler.read(finished).done
    if change == "bill":
        assert other.set_limit("ppo", Decimal("1.15")).estimated
    create = b.service.create

    def interleave(packet: Packet) -> str:
        provider_id = create(packet)
        if change == "set":
            limit = other.set_limit("ppo", Decimal(0))
            assert limit.committed_usd > 1
        else:
            session = other_store.session(attempt.session)
            other_store.record_bill(session, Decimal("0.40"), b.clock())
            (limit,) = other.limits()
            assert limit.committed_usd.quantize(Decimal("0.01")) == Decimal("1.40")
            assert not limit.estimated
        return provider_id

    monkeypatch.setattr(b.service, "create", interleave)
    assert b.scheduler.launch(_request("admitted"), rent="l4") == ("ppo/admitted",)
    with pytest.raises(LauncherError, match="limited to"):
        b.scheduler.launch(_request("refused"), rent="l4")
    assert b.service.creates == 2


def test_unlimited_admission_and_limited_queries_use_bounded_reads(tmp_path: Path) -> None:
    b = bench(tmp_path)
    count = Counting(b.store, b.service)
    with count.record():
        b.scheduler.queue(_request("queued"), rent="l4")
    assert count.snapshot() == Counts(database=39)
    lookup = query_plan(b.store, lambda: b.store.limit("ppo"))
    runs = query_plan(b.store, lambda: b.store.runs(experiment="ppo"))
    reservations = query_plan(b.store, lambda: b.store.reservations("ppo", b.clock()))
    sweep = query_plan(b.store, lambda: b.store.expire_launches(b.clock))
    assert any("SEARCH experiment_limits USING INDEX" in step for step in lookup), lookup
    assert any("SEARCH r USING INDEX runs_experiment" in step for step in runs), runs
    for index in ("launches_reserving", "sessions_open_machines", "launch_acquisitions"):
        assert any(index in step for step in reservations), reservations
    assert sum("SEARCH l USING INDEX launches_reserving" in step for step in sweep) == 2, sweep


def test_unfinished_run_commits_its_spend_when_the_bill_exceeds_its_cap(tmp_path: Path) -> None:
    b = bench(tmp_path)
    (run,) = b.scheduler.launch(_request("running"), rent="l4")
    b.run(1)
    b.clock.advance(30)
    (attempt,) = b.scheduler.attempts(run)
    session = b.store.session(attempt.session)
    b.store.record_bill(session, Decimal(2), b.clock())
    assert b.scheduler.read(run).phase == "running"
    assert b.scheduler.set_limit("ppo", Decimal(1)).committed_usd == 2
