"""Recording an eval run: `record`, then `Evaluation.add` episodes, then `finish`.

An eval run shares a training run's identity, files and delivery
(`etude.run.declaration.declare`, `etude.run.delivery`), with no clocks and `kind`
"evaluation". Its agents and measures are written once to `evaluation.json`.

Recording is strict and synchronous. `add` checks every episode against the
declaration and raises on the first problem, storing nothing from that call;
otherwise it writes the episodes as one immutable `episodes/NS-WRITER.jsonl`
and offers the run's files to each destination. `NS` is the wall-clock time in
nanoseconds, so a later file, from this process or a resumed one, sorts after
an earlier one and replaces any key the two share.
"""

from __future__ import annotations

import secrets
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from etude.eval.records import Agent, Count, Episode, EvaluationFile, Measure, Values
from etude.run.declaration import declare
from etude.run.delivery import RunFiles, StorageDestination, destinations, ship
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, RunCounters, RunState
from etude.run.storage import DirectoryStorage, Storage

KIND = "evaluation"


class Evaluation:
    """A live eval run, created by `record`. Use from one thread."""

    def __init__(
        self,
        *,
        experiment: str,
        name: str,
        root: Path,
        declaration: EvaluationFile,
        destinations: Sequence[StorageDestination],
    ) -> None:
        self.experiment = experiment
        self.name = name
        self.files = RunFiles(root)
        self.declaration = declaration
        # Destination name to the reason its last delivery pass did not complete.
        self.problems: dict[str, str] = {}
        self._destinations = list(destinations)
        self._agents = {agent.name for agent in declaration.agents}
        self._measures = {measure.name: measure for measure in declaration.measures}
        self._writer = secrets.token_hex(6)
        self._keys = {
            Episode.model_validate_json(line).key
            for path in sorted((root / "episodes").glob("*.jsonl"))
            for line in path.read_text().splitlines()
        }
        self._finished = False

    def add(self, episodes: Iterable[Episode]) -> None:
        """Store finished episodes, replacing any already stored under the same key.

        Raises `EtudeError` for an undeclared agent or measure, a measure given
        at the wrong level or of the wrong kind, or a key repeated in this call;
        then nothing from this call is stored.
        """
        if self._finished:
            raise EtudeError("evaluation_finished", 409)
        batch = list(episodes)
        if len({episode.key for episode in batch}) != len(batch):
            raise EtudeError("duplicate_episode_key", 422)
        for episode in batch:
            self._check(episode)
        if not batch:
            return
        body = "".join(episode.model_dump_json() + "\n" for episode in batch)
        DirectoryStorage(self.files.root).put(
            f"episodes/{time.time_ns():020d}-{self._writer}.jsonl", body.encode()
        )
        self._keys.update(episode.key for episode in batch)
        self._ship()

    def keys(self) -> frozenset[str]:
        """Keys of the episodes this run has stored here, to skip on resume."""
        return frozenset(self._keys)

    def _check(self, episode: Episode) -> None:
        for player in episode.players:
            if player.agent not in self._agents:
                raise EtudeError("undeclared_agent", 422) from KeyError(player.agent)
            self._values(episode, "player", player.measures)
        for group in episode.groups:
            self._values(episode, "group", group.measures)
        self._values(episode, "episode", episode.measures)

    def _values(
        self, episode: Episode, level: Literal["player", "group", "episode"], values: Values
    ) -> None:
        for name, value in values.items():
            measure = self._measures.get(name)
            where = ValueError(f"episode {episode.key!r}: measure {name!r} at {level}")
            if measure is None:
                raise EtudeError("undeclared_measure", 422) from where
            if measure.of != level:
                raise EtudeError("measure_at_wrong_level", 422) from where
            if (measure.kind == "rate") != isinstance(value, Count):
                raise EtudeError("measure_kind_mismatch", 422) from where

    def _ship(self) -> bool:
        for destination in self._destinations:
            reason = ship(self.files, self.name, destination)
            if reason is None:
                self.problems.pop(destination.name, None)
            else:
                self.problems[destination.name] = reason
        return not self.problems

    def finish(self, state: Literal["finished", "failed"] = "finished") -> None:
        """Record the final state and deliver. A later `add` raises.

        Delivery failures do not raise: the files stay in the run's directory
        for `etude sync`, and stderr says so.
        """
        if self._finished:
            return
        self._finished = True
        DirectoryStorage(self.files.root).put(
            "state.json",
            RunState(state=state, counters=RunCounters(), writer=self._writer)
            .model_dump_json()
            .encode(),
        )
        if not self._ship():
            print(
                f"etude: not delivered: {self.problems}. Files are in {self.files.root}; "
                "deliver them with `etude sync`.",
                file=sys.stderr,
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.finish("failed" if error is not None else "finished")


def record(
    *,
    experiment: str,
    agents: Sequence[Agent],
    measures: Sequence[Measure],
    run: str | None = None,
    job: str | None = None,
    seed: int | None = None,
    cohort: str | None = None,
    config: Mapping[str, object] | str | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    namespace: str = "local",
) -> Evaluation:
    """Declare or resume an eval run of these agents on these measures.

    Run identity follows `etude.run.declaration.declare`, including the launcher's
    defaults. Resuming requires the same agents and measures, and refuses a
    run that is not an eval run.
    """
    declaration = EvaluationFile(agents=tuple(agents), measures=tuple(measures))
    declared = declare(
        experiment=experiment,
        clocks=(),
        run=run,
        job=job,
        seed=seed,
        cohort=cohort,
        kind=KIND,
        config=config,
        directory=directory,
        namespace=namespace,
    )
    if declared.declaration.kind != KIND:
        raise EtudeError("run_is_not_an_evaluation", 409)
    root = declared.root
    path = root / "evaluation.json"
    if path.is_file():
        if EvaluationFile.model_validate_json(path.read_bytes()) != declaration:
            raise EtudeError("evaluation_declaration_conflict", 409)
    else:
        DirectoryStorage(root).put("evaluation.json", declaration.model_dump_json().encode())
    return Evaluation(
        experiment=experiment,
        name=declared.name,
        root=root,
        declaration=declaration,
        destinations=destinations(declared.base, storages),
    )


def checkpoint_agent(name: str, artifact: ArtifactRef, **labels: str) -> Agent:
    """A saved model as an agent, identified by its artifact's digest."""
    if artifact.sha256 is None:
        raise EtudeError("checkpoint_agent_requires_digest", 422)
    return Agent(
        name=name, kind="checkpoint", identity=artifact.sha256, uri=artifact.uri, labels=labels
    )


def code_agent(name: str, paths: Sequence[Path | str], **labels: str) -> Agent:
    """Code as an agent, identified by the digest of its source files.

    A directory stands for every file below it. Each file contributes its path
    as given and its bytes, so give paths relative to the repository for an
    identity that is the same on every machine.
    """
    files: list[Path] = []
    for given in map(Path, paths):
        files += (
            sorted(path for path in given.rglob("*") if path.is_file())
            if given.is_dir()
            else [given]
        )
    if not files:
        raise EtudeError("code_agent_without_files", 422)
    digest = sha256()
    for path in sorted(set(files), key=Path.as_posix):
        data = path.read_bytes()
        digest.update(f"{path.as_posix()}\0{len(data)}\0".encode() + data)
    return Agent(name=name, kind="code", identity=digest.hexdigest(), labels=labels)
