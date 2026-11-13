"""Start the session as a detached local process.

The disk journal is separate from startup. The SSH starter installs this same
program remotely; a rented machine starts it at boot.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from etude.launch.logging import CLOSED, log_to, write_log
from etude.launch.machines import Allocation
from etude.launch.records import BootstrapLinks
from etude.launch.schemas import Packet
from etude.launch.storage.disk import DirectoryJournal
from etude.launch.storage.files import write


class ProcessStart:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._children: list[subprocess.Popen[bytes]] = []

    def start(self, machine: Allocation, packet: Packet, bootstrap: BootstrapLinks | None) -> str:
        home = self.root / packet.session
        home.mkdir(parents=True, mode=0o700)
        for work in packet.work:
            DirectoryJournal._place_source(home, work)
        write(home / "packet.json", packet.model_dump_json())
        with log_to(home / "bootstrap.log"):
            write_log("bootstrap", "local source preparation completed")
            write_log("bootstrap", CLOSED)
        return self.spawn(packet)

    def spawn(self, packet: Packet) -> str:
        home = self.root / packet.session
        if (home / "cancel-start").exists():
            raise RuntimeError("startup cancelled before worker entry")
        limits = [d for d in (packet.deadline, packet.startup_deadline) if d is not None]
        if limits and datetime.now(UTC) >= min(limits):
            raise TimeoutError("startup or rental deadline reached before worker entry")
        with (home / "session.log").open("wb") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "etude", "worker", str(home)],
                cwd=home,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._children.append(child)
        write(home / "receipt", str(child.pid))
        return str(child.pid)
