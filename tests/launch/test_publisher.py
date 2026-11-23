import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from etude.launch.schemas import Finished
from etude.launch.worker.publisher import publish_attempt
from etude.observations import ViewContext, ViewGrantTarget


class _Receiver(ThreadingHTTPServer):
    """A view that is restarting when the attempt ends: it refuses its first final observations."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.refused = 0
        self.phases: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    server: _Receiver

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/api/observations":
            phase = json.loads(body)["phase"]
            if phase == "done" and self.server.refused < 2:
                self.server.refused += 1
                self.send_response(503)
                self.end_headers()
                return
            self.server.phases.append(phase)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _receiver() -> Iterator[_Receiver]:
    server = _Receiver()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_final_observation_survives_a_brief_receiver_outage(tmp_path: Path) -> None:
    session = tmp_path / "sessions" / "s"
    journal = session / "attempts" / "a"
    journal.mkdir(parents=True)
    (journal / "claim").write_text(datetime.now(UTC).isoformat())
    with _receiver() as receiver:
        context = ViewContext(
            machine="local", hardware=None, max_attempts=1, until=None, source=None, config=None
        )
        origin = f"http://127.0.0.1:{receiver.server_address[1]}"
        ViewGrantTarget(origin=origin, token="t", context=context).place(session, "a")
        with publish_attempt(session, "a", "lab/coin@0", threading.Event(), None):
            finished = Finished(
                finished_at=datetime.now(UTC),
                outcome="succeeded",
                cause=None,
                clocks={},
                outputs=(),
                exit_code=0,
            )
            (journal / "finished.json").write_text(finished.model_dump_json())
        assert receiver.phases[-1:] == ["done"]
