"""Declaring a run: settling its identity and writing, or checking, its `run.json`.

Every kind of run starts here; `etude.train.run.track` and
`etude.eval.run.record` add what their kind writes afterwards.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from etude.run.delivery import RunFiles
from etude.run.errors import EtudeError
from etude.run.records import Clock, Continuation, RunDeclaration, RunFile, SourceKey
from etude.run.storage import run_directory


@dataclass(frozen=True)
class Declared:
    """A declared run: its runs directory, its own folder and its declaration."""

    base: Path
    root: Path
    declaration: RunDeclaration

    @property
    def name(self) -> str:
        return self.root.name


def declare(
    *,
    experiment: str,
    clocks: Sequence[Clock],
    run: str | None = None,
    job: str | None = None,
    seed: int | None = None,
    cohort: str | None = None,
    kind: str | None = None,
    config: Mapping[str, object] | str | None = None,
    continues: Continuation | None = None,
    directory: Path | str | None = None,
    namespace: str = "local",
) -> Declared:
    """Write a new run's `run.json`, or check a resumed one against it.

    A resumed run's declaration is the stored one. Under `etude launch`, `run`,
    `job`, `seed` and `cohort` default to what the launcher handed the process
    (`ETUDE_RUN`, `ETUDE_JOB`, `ETUDE_SEED`, `ETUDE_COHORT`); an argument given
    here wins. Elsewhere `run` defaults to a new random name. Re-using a name
    resumes it; its experiment and clocks must match the first declaration.
    `config` given as text is kept byte for byte; a mapping is written as JSON.
    """
    handed = os.environ
    name = run or handed.get("ETUDE_RUN") or secrets.token_hex(8)
    if seed is None and "ETUDE_SEED" in handed:
        seed = int(handed["ETUDE_SEED"])
    base, root = run_directory(directory, experiment, name)
    text = (
        config if isinstance(config, str) or config is None else json.dumps(config, sort_keys=True)
    )
    declaration = RunDeclaration(
        source_key=SourceKey(namespace=namespace, kind="run", source_id=name),
        clocks=tuple(clocks),
        job=job or handed.get("ETUDE_JOB"),
        job_name=None if job else handed.get("ETUDE_JOB_NAME"),
        seed=seed,
        cohort=cohort or handed.get("ETUDE_COHORT"),
        kind=kind,
        config_json=text,
        config_sha256=sha256(text.encode()).hexdigest() if text is not None else None,
        continues=continues,
    )
    prior = RunFiles(root).run_file()
    if prior is not None:
        if prior.experiment != experiment:
            # Runs are kept by name alone, so a name belongs to one experiment.
            raise EtudeError("run_name_belongs_to_another_experiment", 409)
        if prior.declaration.clocks != declaration.clocks:
            raise EtudeError("run_clock_conflict", 409)
        return Declared(base, root, prior.declaration)
    root.mkdir(parents=True, exist_ok=True)
    (root / "run.json").write_text(
        RunFile(
            experiment=experiment, namespace=namespace, declaration=declaration
        ).model_dump_json()
    )
    return Declared(base, root, declaration)
