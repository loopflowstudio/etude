"""Image preparation selects immutable platform metadata without machine effects."""

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from etude.launch.database import Database
from etude.launch.images import PreparedRuntime, prepare_pytorch, resolve_image
from etude.launch.records import Image, LauncherError, MachineTemplate
from tests.launch.fake import bench

DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize("index", [False, True])
def test_prepare_selects_platform_with_one_metadata_command(
    monkeypatch: pytest.MonkeyPatch, index: bool
) -> None:
    calls = 0
    platform = {"os": "linux", "architecture": "amd64"}
    manifest: dict[str, object] = {"digest": DIGEST}
    if index:
        manifest["manifests"] = [
            {"digest": DIGEST, "platform": platform},
            {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
        ]

    def inspect(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            "docker",
            0,
            json.dumps(
                {
                    "name": "docker.io/pytorch/pytorch:fixture",
                    "manifest": manifest,
                    "image": {"linux/amd64": platform} if index else platform,
                }
            ).encode(),
        )

    monkeypatch.setattr(subprocess, "run", inspect)
    selected = prepare_pytorch(torch="2.10.0", cuda="12.8")
    assert selected.image.requested == "pytorch/pytorch:2.10.0-cuda12.8-cudnn9-runtime"
    assert selected.image.resolved == "docker.io/pytorch/pytorch@" + DIGEST
    assert selected.runtime.packages == {"torch": "2.10.0+cu128"}
    assert selected.runtime.cuda == "12.8"
    assert selected.image.platform == "linux/amd64" and calls == 1
    assert PreparedRuntime.model_validate_json(selected.model_dump_json()) == selected
    with pytest.raises(LauncherError, match="linux/arm64"):
        resolve_image("custom:tag", platform="linux/arm64")


def test_pinned_legacy_input_validates_without_registry_access() -> None:
    model = MachineTemplate.model_validate({"provider": "runpod", "image": "r/e@" + DIGEST})
    assert model.image == Image(requested="r/e@" + DIGEST, resolved="r/e@" + DIGEST)
    with pytest.raises(ValidationError):
        MachineTemplate.model_validate({"provider": "runpod", "image": "r/e:mutable"})


def test_machine_image_survives_ondemand_changes_and_database_restart(tmp_path: Path) -> None:
    b = bench(tmp_path)
    first = b.scheduler.rent("l4", hours=1)
    original = b.store.machine(first).image
    selected = Image(
        requested="custom:chosen",
        resolved="registry/custom@sha256:" + "b" * 64,
        platform="linux/amd64",
    )
    b.scheduler.set_ondemand_image("l4", selected)
    second = b.scheduler.rent("l4", hours=1)
    restarted = Database(tmp_path / "launcher.db")
    assert restarted.machine(first).image == original
    assert restarted.machine(second).image == selected
    assert restarted.ondemand("l4").template.image == selected
    assert b.service.creates == 2


def test_resolution_errors_preserve_sanitized_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIXTURE_TOKEN", "private-registry-fixture")

    def denied(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(
            1,
            ["docker", "private-registry-fixture"],
            stderr=b"denied token=private-registry-fixture",
        )

    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(LauncherError) as caught:
        resolve_image("private/image:tag")
    assert "denied" in str(caught.value)
    assert "private-registry-fixture" not in str(caught.value)
