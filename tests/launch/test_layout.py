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
                for package in ("etude.runs", "etude.train")
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
            assert not any(".runpod" in name for name in imports), path
            assert not any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ("RUNPOD_" in node.value or "etude.launch.providers.runpod" in node.value)
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
    for entry in (
        "etude.launch.providers.runpod.pod",
        "etude.launch.providers.package",
        "etude.launch.environment",
    ):
        for module in reachable(entry):
            assert module in modules or module.split(".")[0] in sys.stdlib_module_names, module
