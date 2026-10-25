"""What every run shares never depends on one kind of run, and no run depends on the launcher.

Eval runs depend on neither training nor the launcher, and only the top-level
command, typed jobs (`etude.jobs`), experiments (`etude.experiment`) and the
report page (`etude.page`) compose them with the rest. The eval run knows nothing of the
tournaments arranged on top of it.
"""

import ast
import sys
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
        "etude.run": ("etude.train", "etude.eval", "etude.launch"),
        "etude.train": ("etude.eval", "etude.launch"),
        "etude.eval": ("etude.train", "etude.launch"),
    }
    for package, forbidden in rules.items():
        imports = _imports(package)
        assert imports, package
        for path, names in imports.items():
            assert not [n for n in names for f in forbidden if _within(n, f)], path


def test_only_the_top_level_command_and_typed_jobs_import_evaluation() -> None:
    composers = {
        SOURCE / "etude/cli.py",
        SOURCE / "etude/jobs.py",
        SOURCE / "etude/experiment.py",
        SOURCE / "etude/page.py",
    }
    for path, names in _imports("etude").items():
        if not path.is_relative_to(SOURCE / "etude/eval") and path not in composers:
            assert not [n for n in names if _within(n, "etude.eval")], path


def test_the_eval_run_imports_nothing_arranged_on_top_of_it() -> None:
    path = SOURCE / "etude/eval/match.py"
    names = _imports("etude.eval")[path]
    above = ("etude.eval.tournament", "etude.eval.query")
    assert not [n for n in names for f in above if _within(n, f)]


def test_carriage_rules_import_only_the_standard_library() -> None:
    """Both carriers import them, so they import neither the launcher nor tracking."""
    path = SOURCE / "etude/carriage.py"
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    assert {name.split(".")[0] for name in names} <= sys.stdlib_module_names | {"__future__"}
