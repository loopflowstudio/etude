"""What every run shares never depends on one kind of run, and no run depends on the launcher."""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src"


def _imports(package: str) -> dict[Path, set[str]]:
    """Every module each file of a package names in an import, at any depth in the file."""
    found: dict[Path, set[str]] = {}
    for path in (SOURCE / package.replace(".", "/")).rglob("*.py"):
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        found[path] = names
    return found


def _within(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def test_shared_run_code_imports_no_kind_of_run_and_training_imports_no_evaluation() -> None:
    rules = {
        "etude.runs": ("etude.train", "etude.eval", "etude.launch"),
        "etude.train": ("etude.eval", "etude.launch"),
    }
    for package, forbidden in rules.items():
        imports = _imports(package)
        assert imports, package
        for path, names in imports.items():
            assert not [n for n in names for f in forbidden if _within(n, f)], path
