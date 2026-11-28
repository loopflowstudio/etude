"""Stored experiment spending limits on separate submissions and launcher connections."""

from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch.client import limit, limits, queue
from etude.launch.database import launcher_file
from etude.launch.records import Launch, OnDemand, Pool
from etude.launch.scheduler.loop import Scheduler
from etude.launch.schemas import Packet
from etude.launch.server import LaunchInput, SubmissionRequest, _answer
from etude.spec import Job, RunRequest
from tests.launch.counting import Counting, Counts, query_plan
from tests.launch.fake import L4, bench, open_database


def _request(name: str, *, usd: str = "1", experiment: str = "ppo") -> RunRequest:
    return RunRequest(
        id=name,
        job=Job(command="train", experiment=experiment, max_hours=1, max_usd=Decimal(usd)),
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


def test_cli_lists_retained_estimated_commitments_without_provider_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    (run,) = b.scheduler.launch(_request("finished"), rent="l4")
    b.run(601)
    assert b.scheduler.read(run).done
    assert main(["limit", "set", "ppo", "--max-usd", "0.30"]) == 0
    assert capsys.readouterr().out == "ppo  limited to $0.30; ~$0.10 committed\n"
    assert main(["limits"]) == 0
    assert capsys.readouterr().out == "ppo  ~$0.10 of $0.30 committed\nlast loop: never\n"
    (configured,) = limits()
    assert limit("ppo") == configured
    assert configured.estimated
    assert b.service.creates == 1
    assert main(["limit", "unset", "ppo"]) == 0
    assert limits() == ()
    assert main(["limit", "unset", "ppo"]) == 1
    assert "has no spending limit" in capsys.readouterr().err
    assert main(["limit", "set", "ppo", "--max-usd", "two"]) == 1
    assert "--max-usd must be a dollar amount" in capsys.readouterr().err


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
    assert count.snapshot() == Counts(database=38)
    lookup = query_plan(b.store, lambda: b.store.limit("ppo"))
    runs = query_plan(b.store, lambda: b.store.runs(experiment="ppo"))
    reservations = query_plan(b.store, lambda: b.store.reservations("ppo"))
    assert any("SEARCH experiment_limits USING INDEX" in step for step in lookup), lookup
    assert any("SEARCH r USING INDEX runs_experiment" in step for step in runs), runs
    assert any("USING INDEX launches_reserving" in step for step in reservations), reservations


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
