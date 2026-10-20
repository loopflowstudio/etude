"""The eval report: one self-contained page over every eval run under a source.

For each measure declared alike by every run that declares it, a matrix of
subject (row) against opponent (column), with an "all" column, then the same
pairings split by each label that takes between 2 and `SPLIT_VALUES` values,
such as a cell or who played first. Episode measures are split by label only.
Conflicting declarations are listed and not combined. Plain HTML, no script
and no network; regenerating replaces the file.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from pathlib import Path

from etude.eval.read import (
    Comparison,
    EpisodeView,
    agent_names,
    agents,
    compare,
    conflicts,
    episodes,
    measures,
)
from etude.eval.records import Measure
from etude.run.storage import Storage

SPLIT_VALUES = 12

_STYLE = """
body { font: 14px/1.4 system-ui, sans-serif; margin: 2rem; color: #2b2520; background: #fbf8f3; }
table { border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
th, td { border: 1px solid #d8cfc2; padding: 0.25rem 0.5rem; text-align: right; }
th { background: #f1ebe1; } td.name, th.name { text-align: left; }
small { color: #6b5f52; }
"""


def _cell(found: Comparison | None) -> str:
    if found is None:
        return "<td></td>"
    interval = (
        f"<br><small>[{found.interval[0]:.3g}, {found.interval[1]:.3g}]</small>"
        if found.interval
        else ""
    )
    return (
        f"<td>{found.value:.3g}{interval}"
        f"<br><small>{found.episodes} ep, {found.blocks} blk</small></td>"
    )


def _party(identities: tuple[str, ...] | None, names: dict[str, str]) -> str:
    if identities is None:
        return "all"
    return " + ".join(names[identity] for identity in identities) or "episode"


def _matrix(found: Sequence[Comparison], names: dict[str, str]) -> str:
    subjects = list(dict.fromkeys(c.subject for c in found))
    opponents = sorted({c.opponent for c in found if c.opponent is not None})
    cells = {(c.subject, c.opponent): c for c in found}
    head = "".join(f"<th>{escape(_party(o, names))}</th>" for o in [None, *opponents])
    rows = "".join(
        f"<tr><td class=name>{escape(_party(s, names))}</td>"
        + "".join(_cell(cells.get((s, o))) for o in [None, *opponents])
        + "</tr>"
        for s in subjects
    )
    return f"<table><tr><th class=name>subject \\ opponent</th>{head}</tr>{rows}</table>"


def _split(found: Sequence[Comparison], label: str, names: dict[str, str]) -> str:
    rows = "".join(
        f"<tr><td class=name>{escape(_party(c.subject, names))}</td>"
        f"<td class=name>{escape(_party(c.opponent, names))}</td>"
        f"<td class=name>{escape(str(c.labels[label]))}</td>{_cell(c)}</tr>"
        for c in found
    )
    return (
        f"<table><tr><th class=name>subject</th><th class=name>opponent</th>"
        f"<th class=name>{escape(label)}</th><th>value</th></tr>{rows}</table>"
    )


def _labels(views: Sequence[EpisodeView], measure: Measure) -> list[str]:
    """Labels worth splitting this measure by: those with a few distinct values."""
    values: dict[str, set[str]] = {}
    for view in views:
        if measure not in view.declaration.measures:
            continue
        episode = view.episode
        sources = [episode.labels]
        if measure.of == "player":
            sources += [player.labels for player in episode.players]
        elif measure.of == "group":
            sources += [group.labels for group in episode.groups]
        for labels in sources:
            for name, value in labels.items():
                values.setdefault(name, set()).add(value)
    return sorted(name for name, seen in values.items() if 2 <= len(seen) <= SPLIT_VALUES)


def render(views: Sequence[EpisodeView]) -> str:
    """The page for these episodes."""
    names = agent_names(views)
    runs = sorted({(view.experiment, view.run) for view in views})
    parts = [
        f"<h1>Evaluations</h1><p>{len(views)} episodes from {len(runs)} eval runs: "
        + escape(", ".join(f"{experiment}/{run}" for experiment, run in runs))
        + "</p>",
        "<h2>Agents</h2><table><tr><th class=name>name</th><th class=name>kind</th>"
        "<th class=name>identity</th><th class=name>labels</th></tr>"
        + "".join(
            f"<tr><td class=name>{escape(names[a.identity])}</td><td class=name>{a.kind}</td>"
            f"<td class=name><code>{a.identity[:12]}</code></td><td class=name>"
            + escape(", ".join(f"{k}={v}" for k, v in sorted(a.labels.items())))
            + "</td></tr>"
            for a in agents(views)
        )
        + "</table>",
    ]
    for measure in measures(views):
        parts.append(
            f"<h2>{escape(measure.name)}</h2><p>{escape(measure.meaning)} "
            f"<small>({measure.kind} per {measure.of})</small></p>"
        )
        parts.append(_matrix(compare(views, measure.name), names))
        for label in _labels(views, measure):
            parts.append(f"<h3>by {escape(label)}</h3>")
            parts.append(_split(compare(views, measure.name, by=[label]), label, names))
    found = conflicts(views)
    if found:
        parts.append("<h2>Conflicting declarations</h2><p>Not combined.</p><ul>")
        for conflict in found:
            versions = "; ".join(
                f"{m.kind} per {m.of}: {escape(m.meaning)}" for m in conflict.declarations
            )
            parts.append(
                f"<li>{escape(conflict.measure)} in {escape(', '.join(conflict.runs))}: "
                f"{versions}</li>"
            )
        parts.append("</ul>")
    return (
        "<!doctype html><html><head><meta charset=utf-8><title>Evaluations</title>"
        f"<style>{_STYLE}</style></head><body>{''.join(parts)}</body></html>"
    )


def write(source: Storage | Path | str, directory: Path) -> Path | None:
    """Replace `directory/eval/index.html`; None when the source holds no eval run."""
    views = episodes(source)
    if not views:
        return None
    target = directory / "eval" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_text(render(views))
    partial.replace(target)
    return target
