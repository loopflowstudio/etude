"""Explicit checkpoint/evaluation links and their training-report projection.

The training run owns requests under `evaluations/`. Results remain in actual
Etude evaluation runs; no evaluation score is copied into training metrics.
Coordinates come from the exact recorded checkpoint, never evaluation time.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from etude.run.listing import as_storage, declared, places
from etude.run.records import Digest, Name, Record, RunState, SourceKey
from etude.run.storage import Storage
from etude.train.read import RunData
from etude.train.records import CheckpointRecord

if TYPE_CHECKING:
    import duckdb


class Evaluation(Record):
    """A prospective request: exact run, checkpoint, evaluator/cohort and subject.

    `benchmark` identifies a frozen protocol (including cohort and inference
    settings), independently of the changing checkpoint. `subject` is the
    checkpoint agent's full evaluation identity. Register before submission so
    an absent or unfinished evaluation remains visible.
    """

    training: SourceKey
    checkpoint: CheckpointRecord
    run: Name
    benchmark: Digest
    label: str
    subject: Digest


def record(source: Storage | Path | str, training_run: str, evaluation: Evaluation) -> None:
    """Register an immutable request, refusing a changed binding on restart."""
    storage = as_storage(source)
    key = f"{training_run}/evaluations/{evaluation.run}.json"
    try:
        previous = Evaluation.model_validate_json(storage.get(key))
    except FileNotFoundError:
        storage.put(key, evaluation.model_dump_json().encode())
    else:
        if previous != evaluation:
            raise ValueError("evaluation_binding_changed")


def _results(db: duckdb.DuckDBPyConnection, link: Evaluation) -> list[dict[str, object]]:
    # DuckDB and evaluation queries are optional until a linked request exists.
    from etude.eval.match import ETUDE_TABLES  # noqa: PLC0415
    from etude.eval.query import compare  # noqa: PLC0415
    from etude.eval.report import KEYS, NUMERIC  # noqa: PLC0415

    results: list[dict[str, object]] = []
    for (table,) in db.sql("show tables").fetchall():
        if table in ETUDE_TABLES:
            continue
        for column, kind, *_ in db.sql(f'describe "{table}"').fetchall():
            if column in KEYS or kind not in NUMERIC:
                continue
            for value in compare(
                db, table=table, value=column, where="run = '" + link.run.replace("'", "''") + "'"
            ):
                if value.subject != (link.subject,) or value.opponent is None:
                    continue
                results.append({"table": table, "metric": column, **value.model_dump()})
    return results


def read(source: Storage | Path | str, training: list[RunData]) -> list[dict[str, object]]:
    """Linked requests with current results/counts; failures and absent runs are gaps."""
    storage = as_storage(source)
    links: list[tuple[RunData, Evaluation]] = []
    for trained in training:
        for key in trained.evaluation_files:
            link = Evaluation.model_validate_json(storage.get(key))
            if trained.file is None or link.training != trained.file.declaration.source_key:
                raise ValueError("evaluation_training_identity_mismatch")
            if (
                link.checkpoint not in trained.checkpoints
                or link.checkpoint.artifact.sha256 is None
            ):
                raise ValueError("evaluation_checkpoint_mismatch")
            if not any(run.name == trained.name and prior == link for run, prior in links):
                links.append((trained, link))
    if not links:
        return []
    from etude.eval.query import connect  # noqa: PLC0415
    from etude.eval.records import MatchRecord, Tournament, TournamentRun  # noqa: PLC0415

    locations = places(storage)
    db = connect(storage)
    output: list[dict[str, object]] = []
    try:
        for trained, link in links:
            folders = locations.get(link.run, [])
            files = {
                key[len(folder) :]: folder for folder in folders for key in storage.keys(folder)
            }
            state = None
            if "state.json" in files:
                state = RunState.model_validate_json(
                    storage.get(files["state.json"] + "state.json")
                )
            records: dict[str, MatchRecord] = {}
            for name, folder in sorted(files.items()):
                if name.startswith("matches/") and name.endswith(".json"):
                    match = MatchRecord.model_validate_json(storage.get(folder + name))
                    if match.match.key not in records or match.state == "done":
                        records[match.match.key] = match
            scheduled: int | None = None
            scheduled_games: int | None = None
            if "tournament.json" in files:
                held = TournamentRun.model_validate_json(
                    storage.get(files["tournament.json"] + "tournament.json")
                )
                schedule = Tournament(
                    definition=held.tournament,
                    checkpoints=(held.checkpoint,) if held.checkpoint else (),
                    entrants=held.entrants,
                ).schedule()
                assigned = schedule[held.share[0] :: held.share[1]]
                scheduled = len(assigned)
                scheduled_games = sum(s.match.games for s in assigned)
                subjects = [
                    a for s in schedule for a in s.match.agents if a.identity == link.subject
                ]
                if not subjects or any(
                    a.artifact != link.checkpoint.artifact.sha256 for a in subjects
                ):
                    raise ValueError("evaluation_subject_checkpoint_mismatch")
            done = sum(r.state == "done" for r in records.values())
            failed = sum(r.state == "failed" for r in records.values())
            declaration = declared(storage, folders)
            phase: str = state.state if state else ("partial" if records else "pending")
            if state and state.state == "finished" and (scheduled is None or done != scheduled):
                phase = "incomplete"
            output.append(
                {
                    "training_run": trained.name,
                    **link.model_dump(mode="json"),
                    "state": phase,
                    "scheduled": scheduled,
                    "scheduled_games": scheduled_games,
                    "done": done,
                    "failed": failed,
                    "pending": max(0, scheduled - done - failed) if scheduled is not None else None,
                    "latest_result_ms": max((r.wall_ms for r in records.values()), default=None),
                    "evaluation_key": declaration.declaration.source_key.model_dump()
                    if declaration
                    else None,
                    "results": _results(db, link),
                }
            )
    finally:
        db.close()
    return sorted(output, key=lambda item: (str(item["training_run"]), str(item["run"])))
