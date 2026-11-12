"""Experiment files for tests that draw a report: `etude report FILE` names its experiment by the file."""

from pathlib import Path

from etude.run.records import HOURS_SINCE_START, RUNNING_HOURS


def experiment_file(folder: Path, name: str, text: str = "") -> Path:
    """`folder/NAME.py` holding `text`; empty, it defines no report(), so the page is the default."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.py"
    path.write_text(text)
    return path


def authored(clocks: object) -> dict[str, object]:
    """A row's or checkpoint's clocks without Etude's, which depend on when it ran."""
    assert isinstance(clocks, dict)
    return {k: v for k, v in clocks.items() if k not in (RUNNING_HOURS, HOURS_SINCE_START)}
