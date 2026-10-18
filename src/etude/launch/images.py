"""Resolve registry references before rental using Docker's credential-aware client.

resolve_image reads manifest/config metadata without pulling layers or requiring
an engine. prepare_pytorch selects an official framework image; actual interpreter
and framework compatibility are checked when the job uses that environment.
"""

from __future__ import annotations

import math
import subprocess
from typing import Literal

from pydantic import Field

from etude.launch.logging import explain
from etude.launch.records import Image, LauncherError, Record


class Platform(Record):
    os: str
    architecture: str
    variant: str = ""

    @property
    def name(self) -> str:
        return "/".join(part for part in (self.os, self.architecture, self.variant) if part)


class Descriptor(Record):
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Platform | None = None


class Manifest(Record):
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifests: tuple[Descriptor, ...] = ()


class Inspection(Record):
    name: str
    manifest: Manifest
    image: Platform | dict[str, Platform]


_FORMAT = '{"name":{{json .Name}},"manifest":{{json .Manifest}},"image":{{json .Image}}}'


def resolve_image(reference: str, *, platform: str = "linux/amd64", timeout: float = 30) -> Image:
    """Wait for registry metadata, using docker login credentials; never rent or pull layers."""
    if not reference or reference.startswith("-") or any(c.isspace() for c in reference):
        raise ValueError("use one container image reference")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("image resolution timeout must be positive and finite")
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--format", _FORMAT, "--", reference],
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise LauncherError(
            "image preparation requires Docker CLI with Buildx; no daemon is needed"
        ) from None
    except (OSError, subprocess.SubprocessError) as error:
        raise LauncherError("image preparation failed: " + explain(error)) from None
    try:
        inspected = Inspection.model_validate_json(result.stdout)
    except ValueError:
        raise LauncherError("registry returned invalid image metadata") from None
    if inspected.manifest.manifests:
        matching = [
            m
            for m in inspected.manifest.manifests
            if m.platform is not None and m.platform.name == platform
        ]
        if len(matching) != 1:
            raise LauncherError(f"image must contain exactly one {platform} manifest")
        digest = matching[0].digest
        config = (
            inspected.image.get(platform) if isinstance(inspected.image, dict) else inspected.image
        )
    else:
        digest = inspected.manifest.digest
        config = (
            inspected.image
            if isinstance(inspected.image, Platform)
            else inspected.image.get(platform)
        )
    if config is None or config.name != platform:
        raise LauncherError(f"image configuration does not match {platform}")
    repository = inspected.name.split("@", 1)[0]
    # Remove a tag, preserving any registry port before the last slash.
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    return Image(requested=reference, resolved=f"{repository}@{digest}", platform=platform)


def prepare_pytorch(
    *,
    torch: str,
    cuda: str,
    cudnn: str = "9",
    kind: Literal["runtime", "devel"] = "runtime",
    timeout: float = 30,
) -> Image:
    """Resolve an official stable PyTorch/CUDA combination, including devel when needed."""
    return resolve_image(f"pytorch/pytorch:{torch}-cuda{cuda}-cudnn{cudnn}-{kind}", timeout=timeout)
