"""What an experiment's report shows, declared in Python in the experiment file.

    # ppo.py: experiment "ppo", beside its train() and eval(state)
    def report() -> Report:
        return Report(
            score=Score(table="plays", value="won", reference=0.5),
            evals=(
                Curve(matches=Matches(tag="anchor")),
                Matrix(rows="on_play", columns="deal", matches=Matches(tournament=LADDER.name)),
                Anchors(),
            ),
        )

`etude report ppo.py` imports `report()` from the file (`etude.page`), and
draws the page in two parts: Evals, over the experiment's eval runs, then Perf
and ML details over what the training runs logged and Etude recorded. Every
panel shows every job; a job is its runs across seeds.

Panels name data, never rendering. Eval panels name columns of the score's
eval table (`Matrix(rows=..., columns=...)`, `Ranking(by=...)`,
`Curve(split=...)`), whom a curve is scored against (`Curve(opponent=...)`:
an agent's name, or a checkpoint of the same line by its position, `Earlier`
or `At`), and which matches they count (`Matches`): a tournament's, or one
matchup or tag of it, by the names `eval(state)` gives them. Matches held
outside any tournament are offered by the Evals section's Matches control. Metric
panels name logged metric keys. The defaults are reinforcement learning's:
override `perf` or `ml` to name an experiment's own keys. This module imports only Pydantic and Etude's run records, so an
experiment file can import it at no cost.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from etude.run.records import Record


class Score(Record):
    """The eval value panels show: sum(`value`) / sum(`per`) over `table`'s rows, or its mean.

    `reference` is the value that means even, such as 0.5 for a score: drawn as
    a line, and the matrix's neutral colour. It is computed as
    `etude.eval.query.compare` computes it, with its interval over whole matches.
    """

    table: str
    value: str
    per: str | None = None
    reference: float | None = None


class Matches(Record):
    """Which matches a panel counts: a tournament's, or one matchup or tag of it.

    `tournament` names a benchmark or round robin, None leaving it to the
    section's control; `matchup` names one line of a benchmark, and `tag` the
    matchups tagged with it (`Matchup.tags`). Give the names the experiment's
    `eval(state)` gives, such as `LADDER.name`.
    """

    tournament: str | None = None
    matchup: str | None = None
    tag: str | None = None

    @model_validator(mode="after")
    def one_part(self) -> Self:
        if self.matchup is not None and self.tag is not None:
            raise ValueError("matches_name_a_matchup_or_a_tag_not_both")
        return self


class Earlier(Record):
    """Each point's opponent is the checkpoint of its line that stood `by` earlier on `clock`.

    Only an exact position counts: with no checkpoint there, the point is a gap
    that says so.
    """

    kind: Literal["earlier"] = "earlier"
    clock: str
    by: float


class At(Record):
    """Each point's opponent is the checkpoint of its line that stood at `value` on `clock`."""

    kind: Literal["at"] = "at"
    clock: str
    value: float


class Curve(Record):
    """The score at each checkpoint, a line per job; `split` draws one chart per column value.

    `opponent` fixes whom each point is scored against, overriding the
    section's Opponent control: an agent's name, or a checkpoint of the same
    line (`Earlier`, `At`). None leaves it to the control.
    """

    kind: Literal["curve"] = "curve"
    title: str = "Score"
    split: str | None = None
    matches: Matches | None = None
    opponent: str | Earlier | At | None = None


class Matrix(Record):
    """`columns` across and `rows` down, at one checkpoint of each job: its final by default."""

    kind: Literal["matrix"] = "matrix"
    rows: str
    columns: str
    title: str | None = None
    matches: Matches | None = None


class Ranking(Record):
    """The score for each value of the column `by`, one bar per job; a row filters the page."""

    kind: Literal["ranking"] = "ranking"
    by: str
    title: str | None = None
    matches: Matches | None = None


class HeadToHead(Record):
    """Games between checkpoints of two jobs: each job's score against the other's."""

    kind: Literal["head_to_head"] = "head_to_head"
    title: str = "Head to head"
    matches: Matches | None = None


class Crosstable(Record):
    """Each checkpoint of one line against each other checkpoint of it, ordered by the clock.

    A row's score against a column; the diagonal is empty and an unplayed pair
    shows as such. Tabbed by line, like `Matrix`.
    """

    kind: Literal["crosstable"] = "crosstable"
    title: str = "Head to head between checkpoints"
    matches: Matches | None = None


class Anchors(Record):
    """Fixed agents (code, or checkpoints with no position) against each other.

    Drawn once, not per job: each anchor's score against the others, and its
    strength, the score over its matches that seat no placed checkpoint. A curve against one anchor draws the others' scores against it
    as reference lines.
    """

    kind: Literal["anchors"] = "anchors"
    title: str = "Anchors"
    matches: Matches | None = None


class Series(Record):
    """Logged metrics on one axis, so give keys of one unit; absent keys are listed, not drawn."""

    kind: Literal["series"] = "series"
    title: str
    keys: tuple[str, ...]
    unit: str = ""
    scale: Literal["linear", "log"] = "linear"


class Counters(Record):
    """Metrics expected to stay at zero, shown as a status per job and where they were not."""

    kind: Literal["counters"] = "counters"
    title: str
    keys: tuple[str, ...]


class Flame(Record):
    """Where the time went: an icicle of the seconds logged under `prefix`, per job.

    Each key under the prefix is seconds spent since the previous row, nested by
    `/` (`time/collection`, `time/collection/inference`). A node's total is the
    sum over the run's rows; what its children do not account for is drawn as
    "unattributed", and wall time no top-level timer covers as "untimed".
    """

    kind: Literal["flame"] = "flame"
    title: str = "Where the time went"
    prefix: str = "time/"


class Pace(Record):
    """Each step clock per wall second between checkpoints; a gap where an attempt began."""

    kind: Literal["pace"] = "pace"
    title: str = "Pace"


class Elapsed(Record):
    """Wall hours from a run's first start to each of its checkpoints."""

    kind: Literal["elapsed"] = "elapsed"
    title: str = "Time to each checkpoint"


class Attempts(Record):
    """Each run's attempts: when each started, on which host, and until its last row."""

    kind: Literal["attempts"] = "attempts"
    title: str = "Attempts"


type EvalPanel = Annotated[
    Curve | Matrix | Ranking | HeadToHead | Crosstable | Anchors, Field(discriminator="kind")
]
type MetricPanel = Annotated[
    Series | Counters | Flame | Pace | Elapsed | Attempts, Field(discriminator="kind")
]

PERF: tuple[MetricPanel, ...] = (Pace(), Elapsed(), Attempts(), Flame())
ML: tuple[MetricPanel, ...] = (
    Counters(title="Numerical stability", keys=("rl/nonfinite_count",)),
    Series(title="Losses", keys=("rl/loss", "rl/policy_loss", "rl/value_loss")),
    Series(title="Policy entropy", keys=("rl/entropy",), unit="nats"),
    Series(title="KL divergence", keys=("rl/kl",), scale="log"),
    Series(title="Gradient norm", keys=("rl/gradient_norm",), unit="L2"),
    Series(title="Learning rate", keys=("rl/learning_rate",)),
)


class Report(Record):
    """An experiment's report. `Report()` is what an experiment without `report()` gets.

    `score` None offers every numeric column of every eval table. `clock`
    names the x axis each section starts on (default: the first declared
    clock). `baseline` names the job drawn dotted, by its name or key.
    Metrics logged but named by no panel are drawn last, under ML details.
    """

    evals: tuple[EvalPanel, ...] = (Curve(), Anchors())
    score: Score | None = None
    clock: str | None = None
    baseline: str | None = None
    perf: tuple[MetricPanel, ...] = PERF
    ml: tuple[MetricPanel, ...] = ML

    def columns(self) -> tuple[str, ...]:
        """Every eval table column a panel names, in first-named order."""
        named: list[str | None] = []
        for panel in self.evals:
            if isinstance(panel, Curve):
                named.append(panel.split)
            elif isinstance(panel, Matrix):
                named += [panel.rows, panel.columns]
            elif isinstance(panel, Ranking):
                named.append(panel.by)
        return tuple(dict.fromkeys(name for name in named if name is not None))

    def keys(self) -> tuple[str, ...]:
        """Every metric key a Perf or ML panel names."""
        return tuple(
            dict.fromkeys(
                key
                for panel in (*self.perf, *self.ml)
                if isinstance(panel, Series | Counters)
                for key in panel.keys
            )
        )
