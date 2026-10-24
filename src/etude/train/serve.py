"""A small local server over a tracking directory or bucket: every experiment, always current.

It reads the same files the generated report reads and writes nothing, so a
run needs no setting to be visible here. It has no login: it listens on this
computer only unless told otherwise, and should not be exposed as it is.
"""

from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from etude.run.listing import runs
from etude.run.storage import Storage
from etude.train.report import pages, render

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh}"><title>Experiments</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}
td,th{{text-align:left;padding:.3rem 1.5rem .3rem 0}}</style></head><body>
<h1>Experiments</h1>{body}</body></html>"""


class Server(ThreadingHTTPServer):
    """Serves `/` as the list of experiments and `/EXPERIMENT` as its report."""

    def __init__(
        self, source: Storage | Path | str, address: tuple[str, int], *, refresh: float = 5.0
    ) -> None:
        super().__init__(address, _Handler)
        self.source = source
        self.refresh = refresh

    def index(self) -> str:
        counts: dict[str, int] = {}
        for experiment, _ in runs(self.source):
            counts[experiment] = counts.get(experiment, 0) + 1
        rows = "".join(
            f'<tr><td><a href="/{escape(name)}">{escape(name)}</a></td><td>{count}</td></tr>'
            for name, count in sorted(counts.items())
        )
        body = (
            f"<table><tr><th>Experiment</th><th>Runs</th></tr>{rows}</table>"
            if rows
            else "<p>No runs yet.</p>"
        )
        return INDEX.format(refresh=int(self.refresh), body=body)

    def report(self, experiment: str) -> str | None:
        page = pages(self.source, experiment).get(experiment)
        return render(page, refresh=self.refresh) if page else None


class _Handler(BaseHTTPRequestHandler):
    server: Server

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        name = unquote(self.path.split("?", 1)[0]).strip("/")
        page = self.server.index() if not name else self.server.report(name)
        body = (page or "No such experiment.").encode()
        self.send_response(200 if page else 404)
        self.send_header("Content-Type", f"text/{'html' if page else 'plain'}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Requests are not printed; the command's own output stays readable."""
