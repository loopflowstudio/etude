"""Two tournaments sharing one local serving child; no model libraries or rental needed.

    uv run etude eval examples/served.py --source /tmp/served

The socket protocol and its batching policy belong to this example, not Etude.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing import get_context
from multiprocessing.connection import Client, Connection, Listener

from pydantic import BaseModel, ConfigDict

from etude.errors import RetryRequested
from etude.eval.match import environment
from etude.eval.records import Code, Match, RoundRobin
from etude.eval.result import Tables
from etude.jobs import EvalJob, ExperimentState, InferenceServer


class ServeConfig(BaseModel):
    bias: int = 1
    startup_seconds: float | None = None


class Endpoint(BaseModel):
    model_config = ConfigDict(frozen=True)
    address: tuple[str, int]


def _serve(ready: Connection, bias: int) -> None:
    with Listener(("127.0.0.1", 0)) as listener:
        ready.send(listener.address)
        ready.close()
        while True:
            with listener.accept() as connection:
                request = connection.recv()
                if request is None:
                    return
                connection.send(int(request) + bias)


@contextmanager
def serve(config: ServeConfig) -> Iterator[Endpoint]:
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_serve, args=(child, config.bias))
    process.start()
    child.close()
    try:
        if not parent.poll(config.startup_seconds):
            raise RuntimeError("serving child did not publish its address")
        endpoint = Endpoint(address=parent.recv())
        yield endpoint
    finally:
        parent.close()
        unexpected = process.exitcode is not None
        process.terminate()
        process.join()
        if unexpected:
            raise RetryRequested(f"serving child exited unexpectedly: {process.exitcode}")


def play(match: Match, inference: Endpoint) -> Tables:
    try:
        with Client(inference.address) as connection:
            connection.send(match.games)
            score = int(connection.recv())
    except (ConnectionError, EOFError) as error:
        raise RetryRequested("serving connection closed") from error
    return {"scores": [{"seat": seat, "score": score} for seat in range(len(match.players))]}


net = InferenceServer(name="net", serve=serve, config=ServeConfig())


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    entrants = (
        Code(name="left", config={"strength": 1}),
        Code(name="right", config={"strength": 2}),
    )
    return [
        EvalJob(
            tournament=RoundRobin(
                name=name, environment=environment("served"), configs=({"lane": name},), games=1
            ),
            enter=entrants,
            play=play,
            server=net,
            workers=2,
            max_hours=0.1,
        )
        for name in ("ladder", "gauntlet")
    ]
