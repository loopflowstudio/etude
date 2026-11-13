"""Bounded save-layout probe: repeated bytes and retirement without delete rights."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from etude.out import OutFiles
from etude.run.delivery import RunFiles, StorageDestination, ship
from etude.run.storage import DirectoryStorage


class UploadOnlyStorage(DirectoryStorage):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.model_uploads = 0

    def put_file(self, key: str, path: Path) -> None:
        if key.endswith("/model.pt"):
            self.model_uploads += 1
        super().put_file(key, path)

    def delete(self, key: str) -> bool:
        return False


def main() -> None:
    with TemporaryDirectory(prefix="etude-state-retention-") as temporary:
        base = Path(temporary)
        root = base / "local" / "run"
        state = root / "out" / "state"
        state.mkdir(parents=True)
        storage = UploadOnlyStorage(base / "remote")
        destination = StorageDestination(storage)
        files = RunFiles(root)
        previous: Path | None = None
        for generation in range(1, 4):
            save = state / f"save-{generation:08d}"
            save.mkdir()
            (save / "model.pt").write_bytes(b"unchanged-model")
            (save / "optimizer.pt").write_text(f"optimizer-{generation}")
            # This isolates retention; uploads succeed completely. The probe
            # makes no claim about the carrier's pointer publication ordering.
            (state / "current.json").write_text(json.dumps({"save": save.name}))
            assert ship(files, "run", destination) is None
            if previous is not None:
                for path in previous.iterdir():
                    key = "run/" + path.relative_to(root).as_posix()
                    assert not storage.delete(key)
                    path.unlink()
                previous.rmdir()
                assert ship(files, "run", destination) is None
            previous = save

        membership = OutFiles.model_validate_json(storage.get("run/out.json"))
        members = {file.key for file in membership.files}
        remote_models = [key for key in storage.keys("run/") if key.endswith("/model.pt")]
        assert storage.model_uploads == 3
        assert len(remote_models) == 3
        assert members == {
            "out/state/current.json",
            "out/state/save-00000003/model.pt",
            "out/state/save-00000003/optimizer.pt",
        }
        print(f"unchanged model uploads: {storage.model_uploads}")
        print(f"remote model copies: {len(remote_models)}")
        print("membership: current pointer and save 3 only")
        print("retired save 1 and 2 bytes remain remotely")


if __name__ == "__main__":
    main()
