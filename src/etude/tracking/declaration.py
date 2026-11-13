"""Declaring a run: settling its identity and writing, or checking, its `run.json`.

Every kind of run starts here; `etude.train.run.track` and
`etude.eval.match.evaluate` add what their kind writes afterwards.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from etude.errors import EtudeError
from etude.identity import resume_conflicts
from etude.records import Clock, Continuation, RunDeclaration, RunFile, SourceKey
from etude.storage import run_directory
from etude.tracking.delivery import RunFiles


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
    resume: bool = False,
) -> Declared:
    """Claim a new run's `run.json`, or validate an explicitly resumed one.

    A resumed run's declaration is the stored one. Under `etude launch`, `run`,
    `job`, `seed` and `cohort` default to what the launcher handed the process
    (`ETUDE_RUN`, `ETUDE_JOB`, `ETUDE_SEED`, `ETUDE_COHORT`); an argument given
    here wins. Elsewhere `run` defaults to a new random name. Re-using a name
    is refused unless `resume`; its identity must match the first declaration.
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
        start=handed.get("ETUDE_START"),
    )
    prior = RunFiles(root).run_file()
    if prior is not None:
        if prior.experiment != experiment:
            # Runs are kept by name alone, so a name belongs to one experiment.
            raise EtudeError("run_name_belongs_to_another_experiment", 409)
        if prior.declaration.clocks != declaration.clocks:
            raise EtudeError("run_clock_conflict", 409)
        if not resume:
            raise EtudeError("run_name_taken", detail=f"{name}; use --resume {name}")
        validate_resume(prior, config=text, seed=seed)
        return Declared(base, root, prior.declaration)
    root.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", prefix=".run-", dir=root) as stream:
        stream.write(
            RunFile(
                experiment=experiment, namespace=namespace, declaration=declaration
            ).model_dump_json()
        )
        stream.flush()
        os.fsync(stream.fileno())
        try:
            os.link(stream.name, root / "run.json")
        except FileExistsError:
            raise EtudeError("run_name_taken", detail=name) from None
    return Declared(base, root, declaration)


def validate_resume(prior: RunFile, *, config: str | None, seed: int | None) -> None:
    """Refuse changed config fields or seed before an attempt can write anything."""
    differences = resume_conflicts(
        config=config,
        seed=seed,
        prior_config=prior.declaration.config_json,
        prior_seed=prior.declaration.seed,
    )
    if differences:
        raise EtudeError("run_identity_conflict", detail="; ".join(differences))
