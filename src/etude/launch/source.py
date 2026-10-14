"""Git owns run source; archives are only the worker/controller transport cache.

Submission freezes working bytes and history before recording a run. The cache
is keyed by repository and absolute commit, never by a movable branch name.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from etude.launch.measurement import timed
from etude.launch.records import LauncherError, Run, RunRecord, SourcePosition


def home() -> Path:
    return Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser()


def stored(repo: str, commit: str) -> Path:
    key = hashlib.sha256((repo + "\0" + commit).encode()).hexdigest()
    return home() / "sources" / f"{key}.tar"


def retain(repo: str, commit: str, body: bytes) -> None:
    """Publish a complete archive once; concurrent clients must agree on its bytes."""
    target = stored(repo, commit)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=target.parent) as temporary:
        temporary.write(body)
        temporary.flush()
        try:
            os.link(temporary.name, target)
        except FileExistsError:
            if target.read_bytes() != body:
                raise LauncherError("frozen source archive conflict") from None


@timed("git.resolve")
def resolve(repo: str, revision: str) -> str:
    if stored(repo, revision).is_file():
        return revision
    found = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", revision + "^{commit}"],
        capture_output=True,
        text=True,
    )
    if found.returncode:
        raise LauncherError(f"{revision!r} is not a commit in {repo}")
    return found.stdout.strip()


@timed("git.source_archive")
def source_archive(repo: str, commit: str) -> bytes:
    cached = stored(repo, commit)
    if cached.is_file():
        return cached.read_bytes()
    done = subprocess.run(
        ["git", "-C", repo, "archive", "--format=tar", commit], capture_output=True
    )
    if done.returncode:
        raise LauncherError(f"cannot archive commit {commit} in {repo}")
    return done.stdout


@timed("git.command")
def _git(repo: str, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env)
    if done.returncode:
        # Git's stderr may contain credential-bearing URLs or helper output.
        raise LauncherError(f"git {args[0]} failed while handling run source")
    return done.stdout.strip()


@timed("git.optional")
def _optional(repo: str, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def _origin(repo: str) -> str | None:
    address = _optional(repo, "remote", "get-url", "origin")
    if not address:
        return None
    parsed = urlsplit(address)
    if parsed.scheme in ("http", "https") and (parsed.username or parsed.password or parsed.query):
        raise LauncherError("origin must use a git credential helper, not credentials in its URL")
    if not parsed.scheme and ":" not in address:
        address = str((Path(repo) / address).resolve())
    return address


def _snapshot(repo: str, parent: str) -> str:
    """A private index captures final working bytes, including nonignored new files."""
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _git(repo, "read-tree", parent, env=env)
        _git(repo, "add", "-A", "--", ".", env=env)
        tree = _git(repo, "write-tree", env=env)
    if tree == _git(repo, "rev-parse", parent + "^{tree}"):
        return parent
    date = _git(repo, "show", "-s", "--format=%cI", parent)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Etude snapshot",
        "GIT_AUTHOR_EMAIL": "snapshot@etude.local",
        "GIT_COMMITTER_NAME": "Etude snapshot",
        "GIT_COMMITTER_EMAIL": "snapshot@etude.local",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    return _git(
        repo,
        "-c",
        "commit.gpgsign=false",
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        "Etude working tree snapshot",
        env=env,
    )


@contextmanager
def _history(origin: str) -> Iterator[str]:
    """Read remote history in a disposable object database; leave local refs untouched."""
    with tempfile.TemporaryDirectory() as directory:
        _git(directory, "init", "--bare", "-q")
        _git(
            directory,
            "fetch",
            "--quiet",
            "--",
            origin,
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
            "+refs/etude/runs/*:refs/etude/runs/*",
        )
        yield directory


def _publish(
    repo: str,
    commit: str,
    run: str,
    *,
    previous: SourcePosition | None,
    snapshot: bool,
) -> SourcePosition:
    """Observe history and establish reachability using one fetch of origin.

    Continuations retain their parent's observations; publication still checks
    that source is available before a new run is recorded.
    """
    source = previous or SourcePosition(
        commit=commit,
        origin=_origin(repo),
        branch=_optional(repo, "symbolic-ref", "--quiet", "--short", "HEAD"),
        base_branch=None,
        base_commit=None,
        base_date=None,
        base_subject=None,
        ahead=None,
        release=_optional(repo, "describe", "--tags", commit),
        snapshot=snapshot,
    )
    ref = f"refs/etude/runs/{run}"
    if source.origin is not None:
        with _history(source.origin) as history:
            # FETCH_HEAD supplies the run's objects but is not an advertised ref.
            _git(history, "fetch", "--quiet", "--no-tags", "--", repo, commit)
            reachable = bool(_git(history, "for-each-ref", "--contains=" + commit))
            if previous is None:
                advertised = _git(repo, "ls-remote", "--symref", "origin", "HEAD")
                branch = next(
                    (
                        line.split()[1]
                        for line in advertised.splitlines()
                        if line.startswith("ref: refs/heads/")
                    ),
                    None,
                )
                base = _optional(history, "merge-base", commit, branch) if branch else None
                source = source.model_copy(
                    update={
                        "base_branch": branch.removeprefix("refs/heads/") if branch else None,
                        "base_commit": base,
                        "base_date": _git(history, "show", "-s", "--format=%cI", base)
                        if base
                        else None,
                        "base_subject": _git(history, "show", "-s", "--format=%s", base)
                        if base
                        else None,
                        "ahead": int(_git(history, "rev-list", "--count", base + ".." + commit))
                        if base
                        else None,
                    }
                )
        if not reachable:
            _git(repo, "push", "--porcelain", "origin", f"{commit}:{ref}")
            if _git(repo, "ls-remote", "origin", ref).split() != [commit, ref]:
                raise LauncherError(
                    "pushed source is not readable on origin; check origin's push URL"
                )
    # Keep queued source available for archiving even after local garbage collection.
    _git(repo, "update-ref", ref, commit)
    return source


def freeze(run: Run) -> Run:
    """Freeze at the submitting client; a controller receives this same identity."""
    if run.id is not None:
        if run.job.repo is not None and (
            run.job.revision is None
            or (run.source is not None and run.source.commit != run.job.revision)
        ):
            raise LauncherError("prepared run source does not match its revision")
        return run
    id = uuid4().hex[:12]
    job = run.job
    source = run.source
    if job.repo is not None:
        repo = str(Path(job.repo).resolve())
        commit = resolve(repo, job.revision or "HEAD")
        if source is not None and source.commit != commit:
            raise LauncherError("run source does not match its revision")
        if Path(repo).is_dir():
            repo = _git(repo, "rev-parse", "--show-toplevel")
            parent = commit
            if source is None and job.revision is None:
                commit = _snapshot(repo, parent)
            source = _publish(repo, commit, id, previous=source, snapshot=commit != parent)
        job = job.model_copy(update={"repo": repo, "revision": commit})
    else:
        job = job.model_copy(update={"revision": job.revision or "HEAD"})
    return run.model_copy(update={"job": job, "source": source, "id": id})


def line(source: SourcePosition | None, commit: str) -> str:
    """Where a run's source sits in history, in words.

    Counts are of real commits. A snapshot is one more commit in git, but to a
    reader it is "uncommitted changes", so it is taken out of both counts.
    """
    if source is None:
        return commit
    extra = 1 if source.snapshot else 0
    said = source.commit[:12]
    if source.branch:
        said += f" on {source.branch}"
    parts = [said]
    if source.base_commit:
        parts.append(f'{source.base_branch}@{source.base_commit[:7]} "{source.base_subject}"')
    if source.release:
        tagged = re.fullmatch(r"(.+)-(\d+)-g[0-9a-f]+", source.release)
        tag, past = (
            (tagged.group(1), int(tagged.group(2)) - extra) if tagged else (source.release, 0)
        )
        parts.append(f"{tag} + {past}" if past > 0 else tag)
    if source.ahead is not None and source.ahead - extra > 0:
        count = source.ahead - extra
        parts.append(f"{count} commit{'' if count == 1 else 's'} ahead")
    if source.snapshot:
        parts.append("plus uncommitted changes")
    if source.origin is None:
        parts.append("local-only")
    return ", ".join(parts)


def checkout(run: RunRecord, destination: Path) -> Path:
    """Fetch git objects into a new checkout, never overwrite an existing directory."""
    if run.spec.repo is None or run.spec.revision is None:
        raise LauncherError(f"run {run.id} has no source repository")
    origin = run.source.origin if run.source else _origin(run.spec.repo)
    address = origin or run.spec.repo
    if destination.exists():
        raise LauncherError("source destination already exists; choose a new directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        _git(temporary, "init", "-q")
        _git(temporary, "remote", "add", "origin", address)
        _git(temporary, "fetch", "--quiet", "origin", run.spec.revision)
        _git(temporary, "checkout", "--quiet", "--detach", run.spec.revision)
        # Rename only the finished checkout. Never use the tar cache as source truth.
        os.rename(temporary, destination)
    return destination


def contains(run: RunRecord, revision: str) -> bool:
    """Ancestry, not feature activation. Unknown revisions fail rather than mean false."""
    if run.spec.repo is None or run.spec.revision is None:
        return False
    repo = run.spec.repo
    if Path(repo).is_dir():
        ancestor = resolve(repo, revision)
        return _ancestor(repo, ancestor, run.spec.revision)
    if run.source is None or run.source.origin is None:
        raise LauncherError(f"source history for local-only run {run.id} is unavailable")
    with _history(run.source.origin) as history:
        _git(history, "fetch", "--quiet", "--", run.source.origin, run.spec.revision)
        return _ancestor(history, resolve(history, revision), run.spec.revision)


@timed("git.ancestor")
def _ancestor(repo: str, ancestor: str, commit: str) -> bool:
    done = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, commit], capture_output=True
    )
    if done.returncode not in (0, 1):
        raise LauncherError("cannot read run source ancestry")
    return done.returncode == 0
