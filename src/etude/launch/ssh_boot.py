"""Standard-library SSH host bootstrap and session control.

Runs on the remote host via `python3.12 -c` using only the standard library so
session control, observation, stop, finish, logs and shutdown never require or
install the remote `.venv` and survive a full disk or client upgrade.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import types
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MISSING = 97  # remote exit status meaning "that program is not here yet"
_LOG_LIMIT = 1024 * 1024
_LOG_OPENING = 4096
_PREINSTALL_MODULES = (
    "etude.errors",
    "etude.deadline",
    "etude.storage_timeout",
    "etude.launch.logging",
    "etude.launch.ssh_boot",
)


def _read_ended(home: Path) -> str | None:
    try:
        text = (home / "ended").read_text().strip()
    except OSError:
        return None
    return text or None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _safe_segment(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or "\0" in value
    ):
        raise ValueError(f"invalid {label}")
    return value


def _safe_relative(home: Path, name: object) -> Path:
    if not isinstance(name, str) or not name or "\0" in name or "\\" in name:
        raise ValueError("invalid relative path")
    if name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"invalid relative path: {name!r}")
    base = home.resolve()
    target = (base / name).resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError(f"path escapes session directory: {name!r}")
    return home / name


def _safe_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or not all(ch in "0123456789abcdef" for ch in value)
    ):
        raise ValueError("invalid program digest")
    return value


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.partial")
    fd = -1
    try:
        fd = os.open(str(partial), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.close(fd)
        fd = -1
        os.replace(partial, path)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            partial.unlink()
        raise


def _write_ended(home: Path, at: str) -> bool:
    try:
        _write_bytes(home / "ended", at.encode())
        return True
    except OSError:
        return False


def _recorded(home: Path, name: str) -> int | None:
    try:
        text = (home / name).read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def _boot_alive(home: Path) -> bool:
    if _read_ended(home) is not None:
        return False
    path = home / "boot-claim"
    try:
        st = path.stat()
        text = path.read_text().strip()
    except OSError:
        return False
    if text.isdigit():
        return _pid_alive(int(text))
    # Empty claim written by a released worker counts as booting only while younger than 60 s.
    return not text and (time.time() - st.st_mtime) < 60


def _host_alive(home: Path) -> bool | None:
    # Check boot before reading the PID it hands off to, so its exit cannot hide the handoff.
    booting = _boot_alive(home)
    worker = (home / "pid").exists()
    if (courier := _recorded(home, "courier-pid")) is not None:
        return _pid_alive(courier)  # owns final delivery
    if not worker:
        return booting
    if (home / "courier-claim").exists():
        return True  # v0.4.6 recorded no courier PID
    return None  # pure-SSH worker: liveness and progress live on disk


def _read_bounded_log(path: Path) -> bytes | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= _LOG_LIMIT:
        try:
            return path.read_bytes()
        except OSError:
            return None
    tail_size = _LOG_LIMIT - _LOG_OPENING - 100
    try:
        with path.open("rb") as stream:
            head = stream.read(_LOG_OPENING)
            stream.seek(size - tail_size)
            tail = stream.read(tail_size)
    except OSError:
        return None
    # Align cuts to complete lines so raw truncation never strips a URL or secret prefix
    # before client-side sanitization runs.
    head_cut = head.rfind(b"\n")
    head = head[: head_cut + 1] if head_cut >= 0 else b""
    tail_cut = tail.find(b"\n")
    tail = tail[tail_cut + 1 :] if tail_cut >= 0 else b""
    omitted = size - len(head) - len(tail)
    marker = f"\n[retention truncated: {omitted} characters]\n".encode()
    return head + marker + tail


def handle_logs(home: Path, producer: str, attempt: str | None) -> dict[str, Any]:
    # A completion marker only covers output read after that marker.
    if attempt is not None:
        attempt = _safe_segment(attempt, "attempt")
        path = home / "attempts" / attempt / "log"
        closed = (home / "attempts" / attempt / "finished.json").exists()
    elif producer == "worker":
        path = home / "session.log"
        closed = _read_ended(home) is not None
    else:
        path = home / "bootstrap.log"
        closed = False
    raw = _read_bounded_log(path)
    return {
        "log": {
            "body": base64.b64encode(raw).decode() if raw is not None else None,
            "closed": closed,
        }
    }


def handle_add(home: Path, request: dict[str, Any]) -> dict[str, Any]:
    work = request["work"]
    attempt = _safe_segment(work.get("attempt"), "attempt")
    staged = [(_safe_relative(home, name), body) for name, body in request.get("files", {}).items()]
    try:
        added = home / "work"
        target = added / f"{attempt}.json"
        if not target.exists():
            for dest, body in staged:
                if not dest.exists():
                    _write_bytes(dest, base64.b64decode(body))
            _write_bytes(target, json.dumps(work).encode())
        if request.get("finish"):
            (home / "finish").touch()
    except OSError as error:
        return {"refused": f"{type(error).__name__}: {error}"}
    return {}


def handle_observe(home: Path) -> dict[str, Any]:
    alive = _host_alive(home)
    if alive is not None:
        return {"observation": {"alive": alive, "start_unknown": alive}}
    # Read liveness before attempt journals so a dead session's journal is final.
    worker_alive = _read_ended(home) is None and _pid_alive(_recorded(home, "pid"))
    attempts_dir = home / "attempts"
    attempts: list[dict[str, Any]] = []
    if attempts_dir.is_dir():
        for entry in sorted(attempts_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            claim = _read_text(entry / "claim")
            finished = _read_text(entry / "finished.json")
            result: str | None = None
            log_bytes = 0
            if finished is None:
                result = _read_text(entry / "outputs" / "etude-result.json")
                try:
                    log_bytes = (entry / "log").stat().st_size
                except OSError:
                    log_bytes = 0
            attempts.append(
                {
                    "attempt": entry.name,
                    "claim": claim,
                    "finished": finished,
                    "result": result,
                    "log_bytes": log_bytes,
                }
            )
    usage_path = home / "usage.jsonl"
    try:
        usage: str | None = usage_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        usage = None
    return {
        "observation": {
            "alive": worker_alive,
            "attempts": attempts,
            "usage": usage,
            "start_unknown": False,
        }
    }


def handle_stop(home: Path, attempt: str) -> dict[str, Any]:
    attempt = _safe_segment(attempt, "attempt")
    try:
        stop_dir = home / "stop"
        stop_dir.mkdir(exist_ok=True, mode=0o700)
        (stop_dir / attempt).touch()
    except OSError as error:
        return {"refused": f"{type(error).__name__}: {error}"}
    return {}


def handle_finish(home: Path) -> dict[str, Any]:
    if home.is_dir():
        (home / "finish").touch()
    return {"ended_at": _read_ended(home)}


def _signal_and_wait(home: Path, pids: list[int]) -> None:
    """Send SIGTERM to live `pids` and wait up to 10 s for exit or an `ended` note."""
    signalled: list[int] = []
    for pid in pids:
        if not _pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            signalled.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    if not signalled:
        return
    for _ in range(100):
        if _read_ended(home) is not None or not any(_pid_alive(pid) for pid in signalled):
            return
        time.sleep(0.1)


def _record_ended(home: Path) -> str:
    now = datetime.now(UTC).isoformat()
    _write_ended(home, now)
    return _read_ended(home) or now


def handle_end(home: Path) -> dict[str, Any]:
    if not home.is_dir():
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        (home / "cancel-start").touch()
    except OSError:
        if not ((home / "boot-claim").exists() or (home / "pid").exists()):
            raise
    ended_at = _read_ended(home)
    if ended_at is None:
        if (home / "pid").exists():
            worker = _recorded(home, "pid")
            if worker is not None:
                _signal_and_wait(home, [worker])
            ended_at = _read_ended(home)
            if ended_at is None and not _pid_alive(worker):
                ended_at = _record_ended(home)
        else:
            # The cancel stops an install; a live boot or courier stops at once.
            pids = [
                proc
                for name in ("boot-claim", "courier-pid")
                if (proc := _recorded(home, name)) is not None
            ]
            _signal_and_wait(home, pids)
            ended_at = _read_ended(home)
            if ended_at is None and _host_alive(home) is False and not (home / "pid").exists():
                # Nothing runs here, and the cancel fences any start still in flight.
                ended_at = _record_ended(home)
    return {"ended_at": ended_at}


def install_archive(
    code: bytes,
    *,
    home: Path | None = None,
    action: str | None = None,
    links: dict[str, str] | None = None,
) -> tuple[Path, dict[str, Any], types.ModuleType]:
    """Load stdlib bootstrap modules from `code` and install its digest environment."""
    with zipfile.ZipFile(io.BytesIO(code)) as archive:
        for name in ("etude", "etude.launch"):
            sys.modules.setdefault(name, types.ModuleType(name))
        for name in _PREINSTALL_MODULES:
            module = types.ModuleType(name)
            exec(
                compile(archive.read(name.replace(".", "/") + ".py"), name, "exec"), module.__dict__
            )
            sys.modules[name] = module
        logging_mod = sys.modules["etude.launch.logging"]
        source = archive.read("etude/launch/environment.py")
    namespace: dict[str, Any] = {"__name__": "etude_install"}
    exec(compile(source, "environment.py", "exec"), namespace)
    with contextlib.redirect_stdout(sys.stderr):
        publication = (
            logging_mod.start_publication(Path(os.environ["ETUDE_LOG_PATH"]), links["log_url"])
            if links
            else None
        )
        failed = False
        try:
            root: Path = namespace["install"](code)
        except Exception as error:
            failed = True
            logging_mod.write_log("client install", sys.modules["etude.errors"].explain(error))
            if action == "start" and home is not None:
                _write_ended(home, datetime.now(UTC).isoformat())
            raise
        finally:
            if os.environ.get("ETUDE_LOG_PATH") and (failed or action != "start"):
                logging_mod.write_log("bootstrap", logging_mod.CLOSED)
            if links and publication is not None:
                publication.set()
                logging_mod.publish(Path(os.environ["ETUDE_LOG_PATH"]), links["log_url"])
    return root, namespace, logging_mod


def _abort_boot(signum: int, frame: object) -> None:
    raise InterruptedError("startup cancelled")


def _start(call: dict[str, Any], home: Path) -> int:
    request = call["request"]
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (home / "cancel-start").exists() or (home / "boot-claim").exists():
            print(json.dumps({}))
            return 0
        r_fd, w_fd = os.pipe()
        pid = os.fork()
    except OSError as error:
        print(json.dumps({"refused": f"{type(error).__name__}: {error}"}), flush=True)
        return 0
    if pid:
        os.close(w_fd)
        chunks: list[bytes] = []
        while chunk := os.read(r_fd, 4096):
            chunks.append(chunk)
        os.close(r_fd)
        status = b"".join(chunks).decode(errors="replace")
        if status == "ok":
            print(json.dumps({}), flush=True)
            os._exit(0)
        os.waitpid(pid, 0)
        if status == "exists":
            print(json.dumps({}), flush=True)
            os._exit(0)
        if status.startswith("refused:"):
            print(json.dumps({"refused": status[8:]}), flush=True)
            os._exit(0)
        sys.stderr.write((status or "boot-claim failed") + "\n")
        sys.stderr.flush()
        os._exit(1)
    os.close(r_fd)
    claim = home / "boot-claim"
    created = False
    try:
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        try:
            data = str(os.getpid()).encode()
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
    except FileExistsError:
        try:
            os.write(w_fd, b"exists")
        except OSError:
            pass
        os.close(w_fd)
        os._exit(0)
    except BaseException as error:
        unlinked = not created
        if created:
            try:
                claim.unlink()
                unlinked = True
            except OSError:
                unlinked = not claim.exists()
        prefix = "refused:" if unlinked else ""
        try:
            os.write(w_fd, f"{prefix}{type(error).__name__}: {error}".encode())
        except OSError:
            pass
        os.close(w_fd)
        os._exit(1)
    os.setsid()
    with open(os.devnull, "r+b") as sink:
        for std_fd in (0, 1, 2):
            os.dup2(sink.fileno(), std_fd)
    signal.signal(signal.SIGTERM, _abort_boot)
    os.write(w_fd, b"ok")
    os.close(w_fd)
    os.environ["ETUDE_STARTUP_DEADLINE"] = call.get("startup_deadline") or ""
    os.environ["ETUDE_STARTUP_CANCEL"] = str(home / "cancel-start")
    os.environ["ETUDE_LOG_PATH"] = str(home / "bootstrap.log")
    call.pop("digest", None)
    code = base64.b64decode(call.pop("program"))
    links: dict[str, str] | None = request.get("bootstrap")
    root, namespace, logging_mod = install_archive(code, home=home, action="start", links=links)
    invoked = False
    try:
        if (home / "cancel-start").exists():
            raise RuntimeError("startup cancelled before worker entry")
        invoked = True
        done = subprocess.run(
            [str(root / ".venv/bin/python"), "-m", "etude.launch.worker.remote"],
            input=json.dumps(call).encode(),
            capture_output=True,
            timeout=namespace["startup_timeout"](),
        )
        console = logging_mod.Console("worker entry")
        console.write(done.stderr, final=True)
        if done.returncode:
            raise RuntimeError(f"worker entry exited with status {done.returncode}")
    except Exception as error:
        logging_mod.write_log("worker entry", sys.modules["etude.errors"].explain(error))
        if not invoked:
            _write_ended(home, datetime.now(UTC).isoformat())
        else:
            logging_mod.write_log("worker entry", "reply uncertain; retaining start claim")
    finally:
        logging_mod.write_log("bootstrap", logging_mod.CLOSED)
        if links:
            logging_mod.publish(home / "bootstrap.log", links["log_url"])
    return 0


def dispatch(call: dict[str, Any]) -> int:
    os.environ.update(call.get("storage_environment", {}))
    request = call["request"]
    action = request["action"]
    if action in (
        "start",
        "logs",
        "started",
        "claimed",
        "add",
        "observe",
        "stop",
        "finish",
        "ended",
        "end",
    ):
        session = _safe_segment(request.get("session"), "session")
        home = Path(call["root"]).expanduser() / session
        if action == "start":
            return _start(call, home)
        reply: dict[str, Any]
        match action:
            case "logs":
                reply = handle_logs(home, request["producer"], request.get("attempt"))
            case "started":
                reply = {"started": (home / "pid").exists()}
            case "claimed":
                reply = {"claimed": (home / "boot-claim").exists() or (home / "pid").exists()}
            case "add":
                reply = handle_add(home, request)
            case "observe":
                reply = handle_observe(home)
            case "stop":
                reply = handle_stop(home, request["attempt"])
            case "finish":
                reply = handle_finish(home)
            case "ended":
                reply = {"ended_at": _read_ended(home)}
            case "end":
                reply = handle_end(home)
        print(json.dumps(reply))
        return 0
    os.environ.pop("ETUDE_LOG_PATH", None)
    os.environ["ETUDE_STARTUP_DEADLINE"] = call.get("startup_deadline") or ""
    digest = call.pop("digest", None)
    base = Path(os.environ.get("ETUDE_HOME") or Path.home() / ".etude") / "programs"
    root = base / _safe_digest(digest) if digest is not None else None
    if root is None or not (root / "ready").exists():
        if "program" not in call:
            return MISSING
        code = base64.b64decode(call.pop("program"))
        root, _, _ = install_archive(code)
    else:
        call.pop("program", None)
    done = subprocess.run(
        [str(root / ".venv/bin/python"), "-m", "etude.launch.worker.remote"],
        input=json.dumps(call).encode(),
    )
    return done.returncode


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--install":
        os.environ["ETUDE_STARTUP_DEADLINE"] = args[1] if len(args) > 1 else ""
        install_archive(sys.stdin.buffer.read())
        return 0
    return dispatch(json.load(sys.stdin))


if __name__ == "__main__":
    raise SystemExit(main())
