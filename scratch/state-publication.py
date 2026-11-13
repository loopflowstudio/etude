"""Bounded counterexample to treating mutable out files as one atomic save."""

from pathlib import Path
from tempfile import TemporaryDirectory

from etude.tracking.delivery import RunFiles, StorageDestination, ship
from etude.tracking.storage import DirectoryStorage


class InterruptedStorage(DirectoryStorage):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.remaining: int | None = None

    def put_file(self, key: str, path: Path) -> None:
        if self.remaining == 0:
            raise OSError("controlled interruption")
        if self.remaining is not None:
            self.remaining -= 1
        super().put_file(key, path)


def main() -> None:
    with TemporaryDirectory(prefix="etude-state-atomicity-") as temporary:
        base = Path(temporary)
        root = base / "local" / "run"
        state = root / "out" / "state"
        state.mkdir(parents=True)
        names = ("model.pt", "optimizer.pt", "state.json")
        for name in names:
            (state / name).write_text("save-1")
        storage = InterruptedStorage(base / "remote")
        destination = StorageDestination(storage)
        files = RunFiles(root)
        assert ship(files, "run", destination) is None
        membership = storage.get("run/out.json")
        for name in names:
            partial = state / (name + ".partial")
            partial.write_text("save-2")
            partial.replace(state / name)
        storage.remaining = 1
        result = ship(files, "run", destination)
        retained = {name: storage.get("run/out/state/" + name).decode() for name in names}
        assert result == "unavailable: OSError"
        assert retained == {
            "model.pt": "save-2",
            "optimizer.pt": "save-1",
            "state.json": "save-1",
        }
        assert storage.get("run/out.json") == membership
        print(f"second delivery: {result}")
        print("retained files:", retained)
        print("membership unchanged: True")


if __name__ == "__main__":
    main()
