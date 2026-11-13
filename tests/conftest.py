"""Every test keeps Etude's files, including its caches, in its own temporary home."""

import pytest


@pytest.fixture(autouse=True)
def _etude_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path_factory.mktemp("etude-home")))
