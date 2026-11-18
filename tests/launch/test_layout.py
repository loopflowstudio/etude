"""The folders have enforceable dependency rules, not merely related names."""

import ast
import sys
from pathlib import Path


def test_folder_rules() -> None:
    root = Path(__file__).resolve().parents[2] / "src"
    modules = {
        ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__"): path
        for path in root.rglob("*.py")
    }
    edges: dict[str, set[str]] = {}
    providers = {"runpod", "vast", "lium", "ec2", "lambda_cloud"}
    for module, path in modules.items():
        tree = ast.parse(path.read_text())
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, path
                parent = node.module or ""
                imports.add(parent)
                imports.update(
                    parent + "." + alias.name
                    for alias in node.names
                    if parent + "." + alias.name in modules
                )
        if module.startswith("etude.launch."):
            assert not any(
                name == package or name.startswith(package + ".")
                for name in imports
                for package in (
                    "etude.storage",
                    "etude.checkpoints",
                    "etude.train",
                )
            ), path
        edges[module] = imports
        if path.name == "__init__.py":
            assert path.read_bytes() == b"", path
        if (
            module.startswith("etude.launch.")
            and ".providers." not in module
            and module
            not in {
                "etude.launch.client",  # the only composition of provider implementations
            }
        ):
            assert not any(f".{provider}" in name for name in imports for provider in providers), (
                path
            )
            assert not any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(
                    part in node.value
                    for part in (
                        "RUNPOD_",
                        "VAST_API_KEY",
                        "LIUM_API_KEY",
                        "LAMBDA_TERMINATE_KEY",
                        "LAMBDA_API_KEY",
                        "etude.launch.provisioner.providers.runpod",
                        "etude.launch.provisioner.providers.vast",
                        "etude.launch.provisioner.providers.lium",
                        "etude.launch.provisioner.providers.ec2",
                        "etude.launch.provisioner.providers.lambda_cloud",
                    )
                )
                for node in ast.walk(tree)
            ), path

    def reachable(module: str) -> set[str]:
        seen: set[str] = set()
        pending = [module]
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            pending.extend(edges.get(name, ()))
        return seen

    forbidden = {"scheduler", "database", "client", "cli", "server"}
    for module in modules:
        if module.startswith("etude.launch.worker."):
            assert not any(
                name == f"etude.launch.{part}" or name.startswith(f"etude.launch.{part}.")
                for name in reachable(module)
                for part in forbidden
            ), module
        if module.startswith("etude.launch.storage."):
            assert not reachable(module) & {
                "etude.launch.ssh",
                "etude.launch.startup",
                "etude.launch.provisioner.machines",
            }, module
    for provider in providers:
        for module in modules:
            if module.startswith(f"etude.launch.provisioner.providers.{provider}."):
                assert not any(
                    name.startswith(f"etude.launch.provisioner.providers.{other}.")
                    for name in reachable(module)
                    for other in providers - {provider}
                ), module
    for entry in (
        "etude.launch.provisioner.providers.runpod.pod",
        "etude.launch.provisioner.providers.watchdog",
        "etude.launch.provisioner.providers.vast.instance",
        "etude.launch.provisioner.providers.bootstrap",
        "etude.launch.provisioner.providers.package",
        "etude.launch.environment",
    ):
        for module in reachable(entry):
            assert module in modules or module.split(".")[0] in sys.stdlib_module_names, module


def test_launch_levels_import_only_downward() -> None:
    root = Path(__file__).resolve().parents[2] / "src/etude"
    controller = {
        "controller",
        "experiment",
        "jobs",
        "eval",
        "page",
        "report",
        "cli",
    }
    above_machines = {
        "scheduler",
        "executor",
        "database",
        "client",
        "server",
        "cli",
        "listing",
        "source",
        "schemas",
        "ssh",
        "startup",
        "worker",
    }
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        lower = parts[0] == "launch"
        provisioner = lower and parts[1] == "provisioner"
        scheduler = lower and parts[1] == "scheduler"
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for module in modules:
                if lower:
                    assert module.split(".")[:2] not in [["etude", name] for name in controller], (
                        path
                    )
                if scheduler:
                    assert module.split(".")[:3] not in [
                        ["etude", "launch", name] for name in ("client", "server", "cli")
                    ], path
                    assert not module.startswith("etude.launch.provisioner.providers"), path
                    if path.stem in {"costs", "placement"}:
                        assert module.split(".")[:3] not in [
                            ["etude", "launch", name]
                            for name in (
                                "database",
                                "executor",
                                "ssh",
                                "startup",
                                "worker",
                                "provisioner",
                            )
                        ], path
                        assert module != "etude.launch.scheduler.loop", path
                if provisioner:
                    assert module.split(".")[:3] not in [
                        ["etude", "launch", name] for name in above_machines
                    ], path
                    if isinstance(node, ast.ImportFrom):
                        assert not {a.name for a in node.names} & {"Job", "RunRequest"}, path
                if (
                    not lower
                    and isinstance(node, ast.ImportFrom)
                    and module.startswith("etude.launch")
                ):
                    assert not any(a.name.startswith("_") for a in node.names), path
