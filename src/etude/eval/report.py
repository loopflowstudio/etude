"""The eval report: one self-contained page over every tournament under a source.

For each author table and each of its numeric columns, per environment, a
matrix of subject (row) against opponent (column) with an "all" column, then
the same pairings split by each column that takes between 2 and
`SPLIT_VALUES` values, such as who was on the play. Plain HTML, no script and
no network; regenerating replaces the file.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from etude.eval.query import Comparison, compare, connect
from etude.eval.tournament import ETUDE_TABLES
from etude.run.storage import Storage

if TYPE_CHECKING:
    import duckdb

SPLIT_VALUES = 12
NUMERIC = ("BOOLEAN", "BIGINT", "DOUBLE")
# Keys and coordinates: never a value or a split.
KEYS = ("player", "group", "game", "tournament", "match")

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
        f"<br><small>{found.rows} rows, {found.matches} matches</small></td>"
    )


def _party(identities: tuple[str, ...] | None, names: dict[str, str]) -> str:
    if identities is None:
        return "all"
    return " + ".join(names.get(identity, identity[:12]) for identity in identities)


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


def _split(found: Sequence[Comparison], column: str, names: dict[str, str]) -> str:
    rows = "".join(
        f"<tr><td class=name>{escape(_party(c.subject, names))}</td>"
        f"<td class=name>{escape(_party(c.opponent, names))}</td>"
        f"<td class=name>{escape(str(c.split[column]))}</td>{_cell(c)}</tr>"
        for c in found
    )
    return (
        f"<table><tr><th class=name>subject</th><th class=name>opponent</th>"
        f"<th class=name>{escape(column)}</th><th>value</th></tr>{rows}</table>"
    )


def render(db: duckdb.DuckDBPyConnection) -> str:
    """The page for every tournament in a database `etude.eval.query.connect` opened."""
    names: dict[str, str] = dict(db.sql("select agent, name from agents").fetchall())
    environments = db.sql(
        "select environment, any_value(environment_name), count(*) from tournaments group by all"
    ).fetchall()
    parts = [
        "<h1>Tournaments</h1><p>"
        + escape(
            "; ".join(
                f"{name} ({identity[:12]}): {count} tournaments"
                for identity, name, count in environments
            )
        )  # fmt: skip
        + "</p>",
        "<h2>Agents</h2><table><tr><th class=name>name</th><th class=name>kind</th>"
        "<th class=name>identity</th><th class=name>config</th></tr>"
        + "".join(
            f"<tr><td class=name>{escape(name)}</td><td class=name>{kind}</td>"
            f"<td class=name><code>{identity[:12]}</code></td>"
            f"<td class=name>{escape(str(config))}</td></tr>"
            for identity, name, kind, config in db.sql(
                "select agent, name, kind, config from agents order by name"
            ).fetchall()
        )
        + "</table>",
    ]
    tables = [name for (name,) in db.sql("show tables").fetchall() if name not in ETUDE_TABLES]
    for table in sorted(tables):
        columns = {name: kind for name, kind, *_ in db.sql(f'describe "{table}"').fetchall()}
        splits = [
            column
            for column in columns
            if column not in KEYS and 2 <= _distinct(db, table, column) <= SPLIT_VALUES
        ]
        for value in (c for c, kind in columns.items() if kind in NUMERIC and c not in KEYS):
            found = compare(db, table=table, value=value)
            by = {
                column: compare(db, table=table, value=value, by=[column])
                for column in splits
                if column != value
            }
            for identity, name, _ in environments:
                parts.append(
                    f"<h2>{escape(table)}: {escape(value)}</h2><p><small>environment "
                    f"{escape(name)} ({identity[:12]})</small></p>"
                )
                parts.append(_matrix([c for c in found if c.environment == identity], names))
                for column, split in by.items():
                    parts.append(f"<h3>by {escape(column)}</h3>")
                    parts.append(
                        _split([c for c in split if c.environment == identity], column, names)
                    )
    return (
        "<!doctype html><html><head><meta charset=utf-8><title>Tournaments</title>"
        f"<style>{_STYLE}</style></head><body>{''.join(parts)}</body></html>"
    )


def _distinct(db: duckdb.DuckDBPyConnection, table: str, column: str) -> int:
    found = db.sql(f'select count(distinct "{column}") from "{table}"').fetchone()
    return 0 if found is None else int(found[0])


def write(source: Storage | Path | str, directory: Path) -> Path | None:
    """Replace `directory/eval/index.html`; None when the source holds no tournament."""
    db = connect(source)
    found = db.sql("select count(*) from tournaments").fetchone()
    if found is None or found[0] == 0:
        return None
    target = directory / "eval" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_text(render(db))
    partial.replace(target)
    return target
