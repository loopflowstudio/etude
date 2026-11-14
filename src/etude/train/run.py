"""Deprecated training imports; use etude.train.tracking (removed in 0.5)."""

import warnings

from etude.stopping import Stopped as Stopped
from etude.train.tracking import EtudeWarning as EtudeWarning
from etude.train.tracking import TrainingRun
from etude.train.tracking import local_launch as local_launch
from etude.train.tracking import track as track

warnings.warn(
    "etude.train.run is replaced by etude.train.tracking; removed in 0.5",
    DeprecationWarning,
    stacklevel=2,
)

Run = TrainingRun
