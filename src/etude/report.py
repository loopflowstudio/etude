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
and ML details over what the training runs logged and Etude recorded. A job
groups its runs across seeds; evaluated checkpoints no training run saved
draw a curve per learner line, including on pages with no training runs.

Panels name data, never rendering, and draw; seatings say who plays whom
(`etude.eval.records.Seating`). Eval panels name columns of the score's eval
table (`Matrix(rows=..., columns=...)`, `Ranking(by=...)`, `Curve(split=...)`)
and which matches they count (`Matches`): a tournament's, or one seating or
tag of it, by the names `eval(state)` gives them. A curve of seatings draws
each held checkpoint's seating value where that checkpoint stood. Matches
held outside any tournament are offered by the Evals section's Matches control.
Metric panels name logged metric keys. The defaults are reinforcement learning's:
override `perf` or `ml` to name an experiment's own keys. This module imports only Pydantic and Etude's run records, so an
experiment file can import it at no cost.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, FiniteFloat, model_validator

from etude.run.records import Record


class Score(Record):
    """The eval value panels show: sum(`value`) / sum(`per`) over `table`'s rows, or its mean.

    `reference` is the value that means even, such as 0.5 for a score: drawn as
    a line, and the matrix's neutral colour. It is computed as
    `etude.eval.query.compare` computes it, with its interval over whole matches.
    `unit` labels the counted table rows; it does not change their granularity.
    """

    table: str
    value: str
    per: str | None = None
    reference: float | None = None
    unit: str = "rows"


class Matches(Record):
    """Which matches a panel counts: a tournament's, or one seating or tag of it.

    `tournament` names a benchmark or round robin; `seating` names one seating
    of a benchmark, and `tag` the seatings tagged with it (`Seating.tags`). With
    no `tournament`, a seating or tag covers every tournament holding it, each
    match counted once, and otherwise the section's control picks one. Give the names the experiment's
    `eval(state)` gives, such as `LADDER.name`.
    """

    tournament: str | None = None
    seating: str | None = None
    tag: str | None = None

    @model_validator(mode="after")
    def one_part(self) -> Self:
        if self.seating is not None and self.tag is not None:
            raise ValueError("matches_name_a_seating_or_a_tag_not_both")
        return self


class Curve(Record):
    """Scores over training, a line per job or external learner line; `split` draws one
    chart per column value.

    Where every one of its `matches` is a `Matches(seating=...)`, each point is
    a held checkpoint's seating value, its first seat's score, where that
    checkpoint stood; a held checkpoint whose relative seat has no checkpoint
    is a gap that says why.
    Otherwise each point is a checkpoint's score against the opponent the
    section's Opponent control picks. A tuple of `matches` unions their
    matches before aggregation.
    """

    kind: Literal["curve"] = "curve"
    title: str = "Score"
    split: str | None = None
    matches: Matches | tuple[Matches, ...] | None = None

    @model_validator(mode="after")
    def nonempty_matches(self) -> Self:
        if self.matches == ():
            raise ValueError("curve_matches_must_not_be_empty")
        return self


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


class Crosstable(Record):
    """Each checkpoint of one line against each other checkpoint of it, ordered by the clock.

    A row's score against a column; the diagonal is empty and an unplayed pair
    shows as such. Tabbed by learner line.
    """

    kind: Literal["crosstable"] = "crosstable"
    title: str = "Head to head between checkpoints"
    matches: Matches | None = None


class Anchors(Record):
    """Fixed players (code, or checkpoints with no position) against each other.

    Drawn once, not per job: each anchor's score against the others, and its
    strength, the score over its matches that seat no placed checkpoint. A
    curve against one anchor draws the others' scores against it as reference
    lines.
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


class SchedulePoint(Record):
    """One configured value at an absolute coordinate on the schedule's clock."""

    at: FiniteFloat
    value: FiniteFloat


class Schedule(Record):
    """Configured values per job, linearly interpolated between sampled points.

    These are plans, not observations. Markers use each run's recorded clock;
    no value is extrapolated beyond the supplied points. `jobs` is keyed by a
    job's full name or by its launch name, which covers every config launched
    under it; the full name wins.
    """

    kind: Literal["schedule"] = "schedule"
    title: str
    clock: str
    jobs: dict[str, tuple[SchedulePoint, ...]]
    unit: str = ""

    @model_validator(mode="after")
    def ordered_points(self) -> Self:
        for job, points in self.jobs.items():
            if not points or any(a.at >= b.at for a, b in zip(points, points[1:])):
                raise ValueError(f"schedule_points_must_increase: {job}")
        return self


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
    Curve | Matrix | Ranking | Crosstable | Anchors, Field(discriminator="kind")
]
type MetricPanel = Annotated[
    Series | Counters | Schedule | Flame | Pace | Elapsed | Attempts, Field(discriminator="kind")
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
    """An experiment's report.

    `score` None offers every numeric column of every eval table. `clock`
    names the x axis each section starts on (default: the first declared
    clock). `baseline` names the job drawn dotted. `baseline` and `labels`
    name a job by its launch name, which covers every config
    launched under it, or by its full name ("ppo 1a2b3c"), which covers one.
    `labels` replaces displayed job names, keeping config digests, seed counts
    and identity.
    A build reads and draws only the metrics its panels name.
    `other_metrics=True` also draws every other logged metric, last under ML
    details, and so reads each training run's whole metric index; an
    experiment without `report()` gets `Report(other_metrics=True)`.
    """

    evals: tuple[EvalPanel, ...] = (Curve(), Anchors())
    score: Score | None = None
    clock: str | None = None
    baseline: str | None = None
    labels: dict[str, str] = {}
    other_metrics: bool = False
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
