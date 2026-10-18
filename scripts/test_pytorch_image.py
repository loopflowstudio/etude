"""CPU-only, offline-runtime proof for the documented official PyTorch image.

Run with uv run python scripts/test_pytorch_image.py on a Docker host. Pulling the
pinned image needs registry access; both containers then run without networking.
"""

import json
import subprocess
from pathlib import Path

from etude.launch.images import prepare_pytorch
from etude.launch.runtime import PROBE, select_runtime

IMAGE = "pytorch/pytorch@sha256:b85566342b86d13a67712e9315d40cdc2dad7f8d86df1aff3831f80835edbcca"


def main() -> None:
    prepared = prepare_pytorch(torch="2.10.0", cuda="12.8", timeout=120)
    assert prepared.image.resolved == IMAGE
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", IMAGE], check=True)
    base = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none"]
    probe = subprocess.run(
        [
            *base,
            "--entrypoint",
            prepared.runtime.python,
            IMAGE,
            "-I",
            "-c",
            PROBE,
            '["torch"]',
            "cuda",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )
    environment: dict[str, str] = {}
    info = select_runtime(
        prepared.runtime,
        probe.stdout,
        environment,
    )
    source = Path(__file__).resolve().parents[1] / "src"
    subprocess.run(
        [
            *base,
            "--volume",
            f"{source}:/source:ro",
            "--entrypoint",
            info.python,
            IMAGE,
            "-S",
            "-c",
            "import sys; sys.path.insert(0,'/source'); "
            "from etude.launch.providers.runpod.pod import guard; "
            "assert not any(m in sys.modules for m in ('pydantic','numpy','torch')); "
            "print('guardian imports without site packages')",
        ],
        check=True,
        timeout=60,
    )
    subprocess.run(
        [
            *base,
            "--entrypoint",
            info.python,
            IMAGE,
            "-I",
            "-c",
            "import torch; assert torch.ones(3).sum().item()==3; print('offline CPU tensor succeeded')",
        ],
        check=True,
        timeout=120,
    )
    print(json.dumps({"image": IMAGE, "runtime": info.model_dump(mode="json")}, sort_keys=True))


if __name__ == "__main__":
    main()
