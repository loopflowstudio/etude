"""The session side: runs one packet's attempts in turn and journals each one.

Installed separately from the job environment. Run as
`etude session DIRECTORY`. The directory holds
`packet.json` going in and, coming back:

    receipt                      this process's id, written by whoever started it
    stop/<attempt>               appears when the attempt should stop
    work/<attempt>.json          work added to a kept session after it started
    sources/<commit>.tar         the source each piece of work runs, placed by its service
    attempts/<attempt>/claim     start time; created once, so code starts at most once
    attempts/<attempt>/log       everything the job printed
    attempts/<attempt>/outputs/  what the job wrote to ETUDE_OUTPUT_DIR
    attempts/<attempt>/finished.json
    ended                        when this session ended; nothing is written after it

A job is a shell command. It reads ETUDE_OUTPUT_DIR, ETUDE_STOP_FILE,
ETUDE_DEADLINE, ETUDE_SEED, ETUDE_UNTIL_CLOCK, ETUDE_UNTIL_VALUE, ETUDE_FROM_VALUE,
ETUDE_CHECKPOINT and
ETUDE_CONFIG, and may write `etude-result.json` in its output directory as
`{"clocks": {"steps": 63000, "wall_time_ms": 5400000}, "checkpoint": "checkpoint.pt"}`.
ETUDE_UNTIL_CLOCK and ETUDE_UNTIL_VALUE are the job's target; ETUDE_FROM_VALUE is where
that clock stood at the checkpoint being resumed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import BinaryIO, Literal

from etude.launch.logging import Console, explain, log_to, sanitize
from etude.launch.records import Cause, Outcome, Output, Record
from etude.launch.runtime import PROBE, find_python, select_runtime
from etude.launch.schemas import Packet, Progress, Work
from etude.launch.storage.files import digest, read_result, write

_GRACE_SECONDS = 5.0


def _now() -> datetime:
    return datetime.now(UTC)


def _finish(
    home: Path, outcome: Outcome, cause: Cause | None = None, code: int | None = None
) -> None:
    outputs = home / "outputs"
    files = (
        sorted(
            p
            for p in outputs.rglob("*")
            if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(outputs.resolve())
        )
        if outputs.is_dir()
        else []
    )
    result = read_result(outputs)
    record = Progress(
        attempt=home.name,
        started_at=None,
        finished_at=_now(),
        outcome=outcome,
        cause=cause,
        clocks=result.clocks,
        checkpoint=result.checkpoint,
        exit_code=code,
        outputs=tuple(
            Output(
                name=p.relative_to(outputs).as_posix(),
                sha256=digest(p),
                size_bytes=p.stat().st_size,
            )
            for p in files
        ),
    )
    write(home / "finished.json", record.model_dump_json(exclude={"attempt", "started_at"}))


def _prepare(session: Path, home: Path, work: Work) -> dict[str, str]:
    """Lay out source, config and checkpoint; return the job's environment."""
    source = home / "src"
    source.mkdir()
    (home / "outputs").mkdir()
    # Only plain settings cross into the job, and what a GPU needs to be found. No keys.
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in ("PATH", "HOME", "LANG", "TMPDIR", "LD_LIBRARY_PATH")
        or name.startswith(("NVIDIA_", "CUDA_"))
    }
    # The job may activate its own environment; never inherit the client's Python.
    client_bin = Path(sys.executable).parent
    environment["PATH"] = os.pathsep.join(
        part
        for part in environment.get("PATH", os.defpath).split(os.pathsep)
        if Path(part).absolute() != client_bin.absolute()
    )
    environment["ETUDE_OUTPUT_DIR"] = str(home / "outputs")
    environment["ETUDE_STOP_FILE"] = str(session / "stop" / work.attempt)
    # Where the job's tracking files go: one place on this machine for every
    # run and every attempt, so a second attempt here continues the same files.
    # They are carried to the bucket as they are written when there is one.
    shared = session.parent.parent / "track"
    shared.mkdir(exist_ok=True)
    environment["ETUDE_TRACK_DIR"] = str(shared)
    (home / "run").write_text(work.run)
    environment["ETUDE_ATTEMPT"] = work.attempt
    for name, value in (
        ("ETUDE_RUN", work.run),
        ("ETUDE_JOB", work.job),
        ("ETUDE_JOB_NAME", work.job_name),
        ("ETUDE_COHORT", work.cohort),
    ):
        if value is not None:
            environment[name] = value
    if work.commit is not None:
        # Whoever made the session put the commit's archive beside it.
        with tarfile.open(session / "sources" / f"{work.commit}.tar") as tar:
            tar.extractall(source, filter="data")
    if work.config is not None:
        (home / "config").write_text(work.config)
        environment["ETUDE_CONFIG"] = str(home / "config")
    if work.checkpoint is not None:
        ref = work.checkpoint
        held = session.parent / ref.session / "attempts" / ref.attempt / "outputs" / ref.name
        local = home / "checkpoint" / Path(ref.name).name
        local.parent.mkdir()
        shutil.copyfile(held, local)
        if digest(local) != ref.sha256:
            raise ValueError("checkpoint_digest_mismatch")
        environment["ETUDE_CHECKPOINT"] = str(local)
    if work.seed is not None:
        environment["ETUDE_SEED"] = str(work.seed)
    if work.until is not None:
        clock, _, target = work.until.partition("=")
        environment["ETUDE_UNTIL_CLOCK"] = clock
        environment["ETUDE_UNTIL_VALUE"] = target
        if work.start is not None and clock in work.start:
            at = work.start[clock]
            environment["ETUDE_FROM_VALUE"] = str(int(at) if float(at).is_integer() else at)
    return environment


def _end(process: subprocess.Popen[bytes]) -> None:
    """Stop the job and everything it started: ask, wait briefly, then kill."""
    for sign in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sign)
        except ProcessLookupError:
            return
        try:
            process.wait(_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


class Exit(Record):
    code: int | None
    stopped: Literal["cancelled", "deadline"] | None = None


def _execute(
    command: list[str],
    *,
    home: Path,
    environment: dict[str, str],
    output: BinaryIO,
    errors: BinaryIO,
    stop: Path,
    deadline: datetime,
) -> Exit:
    if stop.exists() or _now() >= deadline:
        return Exit(code=None, stopped="cancelled" if stop.exists() else "deadline")
    process = subprocess.Popen(
        command,
        cwd=home / "src",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=errors,
        start_new_session=True,
    )
    stopped: Literal["cancelled", "deadline"] | None = None
    try:
        while process.poll() is None:
            if stop.exists():
                stopped = "cancelled"
            elif _now() >= deadline:
                stopped = "deadline"
            if stopped is not None:
                break
            time.sleep(0.1)
    finally:
        _end(process)
    return Exit(code=process.returncode, stopped=stopped)


def _runtime(
    home: Path,
    work: Work,
    environment: dict[str, str],
    log: BinaryIO,
    stop: Path,
    deadline: datetime,
) -> Exit:
    assert work.runtime is not None
    runtime = work.runtime
    executable = find_python(runtime, environment)
    log.write(sanitize(f"runtime: inspecting {executable}\n").encode())
    log.flush()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = _execute(
            [
                executable,
                "-I",
                "-c",
                PROBE,
                json.dumps(sorted(runtime.packages)),
                "cuda" if runtime.cuda is not None else "",
            ],
            home=home,
            environment=environment,
            output=output,
            errors=errors,
            stop=stop,
            deadline=deadline,
        )
        errors.seek(0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console"
            with log_to(path):
                console = Console("runtime")
                while body := errors.read(65536):
                    console.write(body)
                console.write(b"", final=True)
            if path.exists():
                log.write(path.read_bytes())
        if result.stopped or result.code:
            return result
        output.seek(0)
        info = select_runtime(runtime, output.read(), environment)
    write(home / "runtime.json", info.model_dump_json())
    log.write(sanitize("runtime: " + info.model_dump_json() + "\n").encode())
    log.flush()
    return result


def _run(session: Path, packet: Packet, work: Work) -> None:
    home = session / "attempts" / work.attempt
    stop = session / "stop" / work.attempt
    if (home / "claim").exists() or (home / "finished.json").exists():
        return  # handled once already; code never starts twice
    if packet.deadline is not None and _now() >= packet.deadline:
        return  # no time left: the launcher decides what that means
    home.mkdir(parents=True, exist_ok=True)
    if stop.exists():
        return _finish(home, "cancelled")
    try:
        environment = _prepare(session, home, work)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        (home / "log").write_text(f"setup failed: {error!r}\n")
        return _finish(home, "lost", "setup_failed")
    started = _now()
    limit = started + timedelta(seconds=work.limit_seconds)
    if packet.deadline is not None:
        limit = min(limit, packet.deadline)
    environment["ETUDE_DEADLINE"] = limit.isoformat()
    result = Exit(code=None)
    with (home / "log").open("wb") as log:
        try:
            if work.runtime is not None:
                result = _runtime(home, work, environment, log, stop, limit)
            if result.stopped is None and (work.runtime is None or result.code == 0):
                # The claim denotes user code, not the read-only runtime probe.
                with tempfile.NamedTemporaryFile(mode="w", dir=home) as claim:
                    claim.write(_now().isoformat())
                    claim.flush()
                    os.link(claim.name, home / "claim")
                result = _execute(
                    ["sh", "-c", work.command],
                    home=home,
                    environment=environment,
                    output=log,
                    errors=log,
                    stop=stop,
                    deadline=limit,
                )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            log.write(("runtime failed: " + explain(error) + "\n").encode())
            return _finish(home, "failed")
    outcome: Outcome
    if result.stopped == "cancelled":
        outcome = "cancelled"
    elif result.stopped == "deadline":
        outcome = (
            "lost"
            if packet.deadline is not None
            and packet.deadline < started + timedelta(seconds=work.limit_seconds)
            else work.limit_kind
        )
    else:
        outcome = "succeeded" if result.code == 0 else "failed"
    _finish(home, outcome, "machine_lost" if outcome == "lost" else None, code=result.code)


def _terminate(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def run(directory: Path) -> int:
    session = directory.expanduser().resolve()
    signal.signal(signal.SIGTERM, _terminate)
    packet = Packet.model_validate_json((session / "packet.json").read_text())
    (session / "stop").mkdir(exist_ok=True)
    (session / "work").mkdir(exist_ok=True)
    try:
        for work in packet.work:
            _run(session, packet, work)
        while packet.kept and (packet.deadline is None or _now() < packet.deadline):
            # Work added later arrives as files, taken oldest first; each runs once.
            added = sorted((session / "work").glob("*.json"), key=lambda p: p.stat().st_mtime)
            for path in added:
                _run(session, packet, Work.model_validate_json(path.read_text()))
            time.sleep(0.2)
    finally:
        write(session / "ended", _now().isoformat())
    return 0
