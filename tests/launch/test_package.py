"""The private install, bare guardian and historical wire contracts."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from etude.launch.client import _open
from etude.launch.environment import install
from etude.launch.providers import package
from etude.launch.providers.package import program_archive
from etude.launch.providers.runpod import pod
from etude.launch.records import Job
from etude.launch.schemas import Packet, Result
from etude.launch.storage.files import read_result
from tests.launch.counting import job_text


def test_guardian_runs_without_site_packages() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys; from etude.launch.providers.runpod.pod import GuardianAPI, guard; "
            "from datetime import datetime, UTC; from decimal import Decimal; "
            'calls=[]; api=GuardianAPI("fixture", lambda method, *args: '
            '(calls.append(method) or (404,b"")) if method=="GET" else (204,b"")); '
            'guard(api,"pod",datetime.now(UTC),Decimal(0)); '
            'assert "pydantic" not in sys.modules and "typer" not in sys.modules',
        ],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_old_packet_loads_and_frozen_models_validate() -> None:
    packet = Packet.model_validate_json(
        '{"session":"old","deadline":"2026-10-13 00:00:00+00:00","work":[],"kept":false}'
    )
    assert packet.cap_usd == 0 and not packet.autorun
    assert Packet.model_validate_json(packet.model_dump_json()) == packet
    with pytest.raises(ValidationError):
        packet.kept = True
    job = Job.model_validate_json('{"command":"true","max_hours":1,"max_usd":"0.10"}')
    assert str(job.max_usd) == "0.10"


@pytest.mark.parametrize(
    "body",
    ["[]", '{"clocks":{"steps":true}}', '{"clocks":{"steps":"ten"}}', '{"clocks":{"steps":NaN}}'],
)
def test_untrusted_result_rejects_invalid_clocks(tmp_path: Path, body: str) -> None:
    (tmp_path / "etude-result.json").write_text(body)
    assert read_result(tmp_path) == Result()


def test_missing_uv_names_the_requirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="uv is required"):
        install(program_archive())


def test_job_uses_its_own_python_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin = tmp_path / "job-bin"
    bin.mkdir()
    executable = bin / "python"
    executable.write_text('#!/bin/sh\necho job-python\ntest -z "$VIRTUAL_ENV"\n')
    executable.chmod(0o700)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(Path(sys.executable).parent), str(bin), os.environ["PATH"]])
    )
    monkeypatch.setenv("VIRTUAL_ENV", str(Path(sys.executable).parent.parent))
    scheduler = _open(tmp_path / "launcher")
    id = scheduler.launch(Job(command="python", max_hours=0.01))
    assert scheduler.wait(id, every=0.05, timeout=10)[0].outcome == "succeeded"
    assert job_text(scheduler.logs(run=id)) == "job-python\n"


def test_runpod_guardian_payload_is_small_and_independent(tmp_path: Path) -> None:
    code = program_archive(guardian=True)
    assert len(code) < 16_000  # startup environment must stay below Linux per-value limits
    with zipfile.ZipFile(io.BytesIO(code)) as archive:
        archive.extractall(tmp_path)
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import etude.launch.providers.runpod.pod"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_transferred_package_keeps_its_digest_when_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = package.program_archive()
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        archive.extractall(tmp_path)
    metadata = tmp_path / "etude/_package"
    metadata.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        (metadata / name).write_bytes((tmp_path / name).read_bytes())
    monkeypatch.setattr(package, "__file__", str(tmp_path / "etude/launch/providers/package.py"))
    assert package.program_archive() == original


@pytest.mark.parametrize("failure", ["", "guardian", "digest", "install"])
def test_stock_boot_requires_guardian_before_download_and_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    events: list[str] = []
    marker = tmp_path / "guardian-started"
    code = b"client archive"
    monkeypatch.setattr(pod, "GUARDIAN_MARKER", marker)
    monkeypatch.setenv("RUNPOD_POD_ID", "own-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "self-key")
    monkeypatch.setenv("ETUDE_PACKAGE_URL", "https://fixture/package")
    monkeypatch.setenv(
        "ETUDE_PACKAGE_SHA256", "wrong" if failure == "digest" else hashlib.sha256(code).hexdigest()
    )
    monkeypatch.setenv("ETUDE_BOOTSTRAP_URL", "https://fixture/bootstrap")

    class Timer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            events.append("guardian spawned")

        def poll(self) -> int | None:
            if failure == "guardian":
                return 1
            marker.write_text("started")
            events.append("guardian started")
            return None

    def download(url: str, timeout: int) -> io.BytesIO:
        assert marker.exists()
        events.append("download")
        return io.BytesIO(code)

    def install(archive: bytes) -> Path:
        assert archive == code and marker.exists()
        events.append("install")
        if failure == "install":
            raise RuntimeError("installation failed")
        return tmp_path

    def start_ssh() -> None:
        assert marker.exists()
        events.append("ssh")

    popen = subprocess.Popen

    def start(command: list[str], **kwargs: Any) -> Timer | subprocess.Popen[bytes]:
        if command[-1] == "guard":
            return Timer()
        assert command == [
            str(tmp_path / ".venv/bin/python"),
            "-m",
            "etude",
            "worker",
            "--bootstrap",
            "https://fixture/bootstrap",
            "--cleanup",
            "[]",
        ]
        assert "PYTHONPATH" not in kwargs["env"]
        events.append("worker")
        return popen([sys.executable, "-c", ""], **kwargs)

    def delete(self: pod.GuardianAPI, id: str) -> None:
        assert id == "own-pod"
        events.append("delete")

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(pod, "_start_ssh", start_ssh)
    monkeypatch.setattr(pod, "ensure_uv", lambda: None)
    monkeypatch.setattr(
        pod, "send", lambda method, url, body, headers: (200, download(url, timeout=60).read())
    )
    monkeypatch.setattr(pod, "install", install)
    monkeypatch.setattr(pod.GuardianAPI, "delete_self", delete)
    assert pod.main(["pod", "boot"]) == (1 if failure else 0)
    expected = ["guardian spawned"]
    if failure != "guardian":
        expected += ["guardian started", "ssh", "download"]
        if failure != "digest":
            expected += ["install"]
            if failure != "install":
                expected += ["worker"]
    assert events == [*expected, "delete"]


@pytest.mark.parametrize("failure", [False, True])
def test_ssh_keeps_authorized_keys_private_and_reports_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    key = "ssh-ed25519 fixture-public-key"
    monkeypatch.setenv("PUBLIC_KEY", key + "\nssh-ed25519 second-public-key")
    directory = tmp_path / ".ssh"
    directory.mkdir()
    authorized = directory / "authorized_keys"
    authorized.write_text("ssh-ed25519 existing-public-key\n" + key + "\n")
    mkdir = Path.mkdir

    def make_directory(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        # /run belongs to the disposable pod, not the host running this test.
        mkdir(tmp_path / "sshd" if self == Path("/run/sshd") else self, mode, parents, exist_ok)

    def run(
        command: list[str], *, check: bool, capture_output: bool, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if failure:
            raise FileNotFoundError("image has no OpenSSH")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(Path, "mkdir", make_directory)
    monkeypatch.setattr(subprocess, "run", run)
    pod._start_ssh()
    assert authorized.read_text().splitlines() == [
        "ssh-ed25519 existing-public-key",
        key,
        "ssh-ed25519 second-public-key",
    ]
    assert directory.stat().st_mode & 0o777 == 0o700
    assert authorized.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert ("SSH unavailable" if failure else "SSH ready") in output
    assert "public-key" not in output


def test_ssh_without_account_keys_explains_setup_without_touching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PUBLIC_KEY", raising=False)
    pod._start_ssh()
    assert "add a public key to your RunPod account" in capsys.readouterr().out
    assert not (tmp_path / ".ssh").exists()


def test_pinned_uv_checksum_is_checked_before_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(pod, "send", lambda method, url, body, headers: (200, b"changed artifact"))
    with pytest.raises(ValueError, match="checksum"):
        pod.ensure_uv()
