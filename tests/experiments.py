"""Experiment files for tests that draw a report: `etude report FILE` names its experiment by the file."""

from pathlib import Path


def experiment_file(folder: Path, name: str, text: str = "") -> Path:
    """`folder/NAME.py` holding `text`; empty, it defines no report(), so the page is the default."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.py"
    path.write_text(text)
    return path
