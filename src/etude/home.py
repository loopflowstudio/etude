"""The directory holding this computer’s Etude state."""

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser()
