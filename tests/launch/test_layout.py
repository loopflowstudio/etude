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
    providers = {"runpod", "vast", "lium"}
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
                for package in ("etude.tracking", "etude.train")
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
                        "etude.launch.providers.runpod",
                        "etude.launch.providers.vast",
                        "etude.launch.providers.lium",
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

    forbidden = {"scheduler", "database", "client", "cli", "controller"}
    for module in modules:
        if module.startswith("etude.launch.worker."):
            assert not reachable(module) & {"etude.launch." + name for name in forbidden}, module
        if module.startswith("etude.launch.storage."):
            assert not reachable(module) & {
                "etude.launch.ssh",
                "etude.launch.startup",
                "etude.launch.machines",
            }, module
    for provider in providers:
        for module in modules:
            if module.startswith(f"etude.launch.providers.{provider}."):
                assert not any(
                    name.startswith(f"etude.launch.providers.{other}.")
                    for name in reachable(module)
                    for other in providers - {provider}
                ), module
    for entry in (
        "etude.launch.providers.runpod.pod",
        "etude.launch.providers.lium.pod",
        "etude.launch.providers.vast.instance",
        "etude.launch.providers.bootstrap",
        "etude.launch.providers.package",
        "etude.launch.environment",
    ):
        for module in reachable(entry):
            assert module in modules or module.split(".")[0] in sys.stdlib_module_names, module
