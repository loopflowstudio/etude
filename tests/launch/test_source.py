"""Transferred sources keep their original bytes across competing submissions."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from etude.cli import main
from etude.launch.client import _open, default
from etude.launch.records import Job, LauncherError, Pool, SourcePosition
from etude.launch.source import checkout, contains, line, retain, source_archive, stored


def test_concurrent_archives_cannot_replace_frozen_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    bodies = (b"first archive", b"other archive")
    with ThreadPoolExecutor(max_workers=2) as clients:
        submissions = [clients.submit(retain, "/absent/repo", "commit", body) for body in bodies]
        errors = [submission.exception() for submission in submissions]
    assert sum(error is None for error in errors) == 1
    assert sum(isinstance(error, LauncherError) for error in errors) == 1
    winner = bodies[errors.index(None)]
    retain("/absent/repo", "commit", winner)  # identical submissions are idempotent
    assert source_archive("/absent/repo", "commit") == winner
    assert list((tmp_path / "sources").iterdir()) == [stored("/absent/repo", "commit")]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    origin.mkdir()
    repo.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.test")
    (repo / "train").write_text("base\n")
    (repo / ".gitignore").write_text("ignored\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Base training")
    git(repo, "tag", "v1")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "origin", "main", "--tags")
    return repo, origin


def test_position_snapshot_publication_and_checkout(repository: tuple[Path, Path]) -> None:
    repo, origin = repository
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "feature")
    (repo / "train").write_text("feature\n")
    git(repo, "commit", "-am", "Feature")
    parent = git(repo, "rev-parse", "HEAD")
    (repo / "train").write_text("staged\n")
    git(repo, "add", "train")
    (repo / "train").write_text("working\n")
    (repo / "new").write_text("new\n")
    (repo / "ignored").write_text("secret\n")
    index = (repo / ".git/index").read_bytes()
    scheduler = _open(repo.parent / "launcher")
    scheduler.add_pool(Pool(name="waiting"))
    first = scheduler.queue(Job(command="true", max_hours=1, repo=str(repo)), "waiting")
    second = scheduler.queue(Job(command="true", max_hours=1, repo=str(repo)), "waiting")
    run = scheduler.record(first)
    position = run.source
    assert position is not None
    assert position.commit == scheduler.record(second).spec.revision
    assert position.base_commit == head and position.base_subject == "Base training"
    assert position.base_date and position.ahead == 2 and position.release is not None
    assert position.release.startswith("v1-2-")
    assert position.branch == "feature" and position.base_branch == "main" and position.snapshot
    assert git(repo, "rev-parse", "HEAD") == parent
    assert (repo / ".git/index").read_bytes() == index
    assert git(origin, "rev-parse", f"refs/etude/runs/{first}") == position.commit
    assert not git(origin, "for-each-ref", f"refs/etude/runs/{second}")
    assert contains(run, parent)
    assert git(repo, "show", position.commit + ":train") == "working"
    assert "ignored" not in git(repo, "ls-tree", "--name-only", position.commit).splitlines()
    repo.rename(repo.with_name("moved"))
    recovered = checkout(run, repo.parent / "recovered")
    assert (recovered / "train").read_text() == "working\n"
    assert (recovered / "new").read_text() == "new\n"
    assert git(recovered, "rev-parse", "HEAD") == position.commit
    with pytest.raises(LauncherError, match="already exists"):
        checkout(run, recovered)
    assert contains(run, head)
    assert _open(repo.parent / "launcher").record(first).source == position


def test_explicit_revision_and_failed_push(repository: tuple[Path, Path]) -> None:
    repo, origin = repository
    head = git(repo, "rev-parse", "HEAD")
    (repo / "train").write_text("not part of explicit HEAD\n")
    scheduler = _open(repo.parent / "launcher")
    scheduler.add_pool(Pool(name="waiting"))
    id = scheduler.queue(
        Job(command="true", max_hours=1, repo=str(repo), revision="HEAD"), "waiting"
    )
    run = scheduler.record(id)
    assert run.spec.revision == head and run.source is not None and not run.source.snapshot
    assert not git(origin, "for-each-ref", "refs/etude/runs")
    hook = origin / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o700)
    with pytest.raises(LauncherError, match="git push failed"):
        scheduler.queue(Job(command="true", max_hours=1, repo=str(repo)), "waiting")
    assert [r.id for r in scheduler.runs(done=True)] == [id]


def test_local_only_snapshot(
    repository: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = repository
    git(repo, "remote", "remove", "origin")
    (repo / "new").write_text("retained locally\n")
    scheduler = default()
    scheduler.add_pool(Pool(name="waiting"))
    id = scheduler.queue(Job(command="true", max_hours=1, repo=str(repo)), "waiting")
    record = scheduler.record(id)
    assert record.source is not None and record.source.origin is None
    assert git(repo, "rev-parse", f"refs/etude/runs/{id}") == record.spec.revision
    assert main(["status", id]) == 0
    assert "local-only" in capsys.readouterr().out
    assert (checkout(record, repo.parent / "copy") / "new").read_text() == "retained locally\n"


def test_runs_filters_and_source_cli_are_read_only(
    repository: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = repository
    scheduler = default()
    scheduler.add_pool(Pool(name="waiting"))
    head = git(repo, "rev-parse", "HEAD")
    job = Job(command="true", max_hours=1, repo=str(repo), name="training")
    first, second = scheduler.queue_cohort("pair", job.runs(1, 2), "waiting")
    scheduler.cancel(first)
    # This queued local job would execute if history commands ticked the scheduler.
    local = scheduler.queue(Job(command="true", max_hours=1))
    assert (
        main(
            [
                "runs",
                "--cohort",
                "pair",
                "--pool",
                "waiting",
                "--job",
                "training",
                "--contains",
                head,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert first in output and second in output and local not in output
    assert "source:" in output and "main@" in output and "Base training" in output
    assert scheduler.read(local).phase == "queued"
    assert main(["runs", "--job", scheduler.record(first).job]) == 0
    assert first in capsys.readouterr().out
    assert main(["runs", "--contains", "no-such-revision"]) == 1
    assert "not a commit" in capsys.readouterr().err
    destination = repo.parent / "cli-copy"
    assert main(["source", first, str(destination)]) == 0
    assert git(destination, "rev-parse", "HEAD") == head
    assert scheduler.read(local).phase == "queued"


def test_containment_distinguishes_squashed_history(repository: tuple[Path, Path]) -> None:
    repo, _ = repository
    git(repo, "switch", "-c", "feature")
    (repo / "train").write_text("feature\n")
    git(repo, "commit", "-am", "Feature")
    feature = git(repo, "rev-parse", "HEAD")
    scheduler = _open(repo.parent / "launcher")
    scheduler.add_pool(Pool(name="waiting"))
    id = scheduler.queue(Job(command="true", max_hours=1, repo=str(repo)), "waiting")
    run = scheduler.record(id)
    git(repo, "switch", "main")
    git(repo, "merge", "--squash", "feature")
    git(repo, "commit", "-m", "Squashed feature")
    squash = git(repo, "rev-parse", "HEAD")
    assert contains(run, feature) and not contains(run, squash)
    # A local-only tag must not be mistaken for publication on origin.
    git(repo, "tag", "unpublished")
    new = scheduler.queue(Job(command="true", max_hours=1, repo=str(repo)), "waiting")
    assert git(repository[1], "rev-parse", f"refs/etude/runs/{new}") == squash


def test_the_source_line_counts_real_commits_and_names_the_branch() -> None:
    snapshot = SourcePosition(
        commit="e11053da6dfa" + "0" * 28,
        origin="git@example.invalid:me/repo.git",
        branch="distributed",
        base_branch="main",
        base_commit="6c7c5ac" + "0" * 33,
        base_date="2026-10-13",
        base_subject="Tune the learning rate",
        ahead=2,  # one real commit and the snapshot
        release="v0.1.0-3-ge11053d",
        snapshot=True,
    )
    assert line(snapshot, snapshot.commit) == (
        'e11053da6dfa on distributed, main@6c7c5ac "Tune the learning rate", '
        "v0.1.0 + 2, 1 commit ahead, plus uncommitted changes"
    )

    released = snapshot.model_copy(
        update={"snapshot": False, "ahead": 0, "release": "v0.1.0", "origin": None}
    )
    assert line(released, released.commit) == (
        'e11053da6dfa on distributed, main@6c7c5ac "Tune the learning rate", v0.1.0, local-only'
    )
    assert line(None, "abc123") == "abc123"
