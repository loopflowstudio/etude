"""Pin supported defining-module imports independently of their declarations."""

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Changing support is a deliberate API decision, separate from adding a definition.
SURFACE: dict[str, tuple[str, ...]] = {
    "etude.jobs": (
        "TrainingJob",
        "EvalJob",
        "Agents",
        "ExperimentState",
        "continue_from",
        "TrainingRunSummary",
        "EvalRunSummary",
        "Policy",
        "Checkpoints",
        "OutMembers",
    ),
    "etude.experiment": ("observe",),
    "etude.train.tracking": ("track", "TrainingRun"),
    "etude.train.records": ("Time", "Step"),
    "etude.train.read": (
        "read",
        "read_all",
        "read_batches",
        "RunData",
        "Batch",
        "Points",
        "buckets",
        "Buckets",
        "lines",
    ),
    "etude.checkpoints": ("CheckpointRef", "CheckpointRecord", "PolicyKey"),
    "etude.records": ("Record", "ArtifactRef", "Clock", "SourceKey"),
    "etude.storage": (
        "default_root",
        "storage_from_uri",
        "Storage",
        "DirectoryStorage",
        "S3Storage",
    ),
    "etude.stopping": ("Stopped",),
    "etude.errors": ("EtudeError", "LauncherError"),
    "etude.deadline": ("limit_time",),
    "etude.eval.agents": ("Forward",),
    "etude.eval.records": (
        "CheckpointPlayer",
        "Code",
        "Held",
        "Composition",
        "compose",
        "Benchmark",
        "RoundRobin",
        "Seating",
        "Seat",
        "Earlier",
        "Fraction",
        "At",
        "Final",
        "Aligned",
        "Match",
        "Environment",
        "Player",
        "Rollup",
    ),
    "etude.eval.match": ("environment",),
    "etude.eval.result": ("Tables", "Row", "Value"),
    "etude.eval.tournament": (
        "evaluating",
        "scores",
        "standings",
        "ScorePoint",
        "Scores",
        "Standing",
        "Standings",
    ),
    "etude.eval.query": ("statsdb", "read_blobs", "estimate", "compare", "Estimate", "Comparison"),
    "etude.report": (
        "Report",
        "Score",
        "Matches",
        "Curve",
        "Matrix",
        "Ranking",
        "Anchors",
        "Planned",
        "PlannedPoint",
        "ML",
        "Crosstable",
        "Series",
        "Counters",
        "Flame",
        "Pace",
        "Elapsed",
        "Attempts",
    ),
    "etude.spec": (
        "Job",
        "RunRequest",
        "Until",
        "Input",
        "Variable",
        "Runtime",
        "Preparation",
        "SmokeForm",
    ),
    "etude.launch.client": (
        "queue",
        "launch",
        "quote",
        "availability",
        "status",
        "wait",
        "cancel",
        "diagnostics",
        "logs",
        "fetch",
        "default",
        "rent",
        "terminate",
        "usage",
        "standing",
    ),
    "etude.launch.records": (
        "MachineTemplate",
        "OnDemand",
        "Provider",
        "Pool",
        "Storage",
        "Image",
        "GPU",
        "CPU",
        "Availability",
        "Offer",
        "Status",
        "Quote",
        "Diagnostic",
        "Machine",
        "RentalCost",
        "LogStream",
        "Standing",
    ),
    "etude.launch.database": ("Database", "inspect", "upgrade", "Schema", "Upgrade"),
    "etude.launch.provisioner.images": ("resolve_image", "prepare_pytorch"),
    "etude.launch.usage": (
        "Usage",
        "R2",
        "S3_US_WEST_2",
        "Access",
        "reconcile",
        "Rates",
        "Transfer",
        "Reconciliation",
    ),
}


def test_supported_names_resolve_in_their_defining_modules() -> None:
    declared_modules = set()
    for path in (ROOT / "src/etude").rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
            for node in tree.body
        ):
            declared_modules.add(".".join(path.relative_to(ROOT / "src").with_suffix("").parts))
    assert declared_modules == set(SURFACE)
    # Keep these broad author imports out of pytest's process: startup checks own
    # their fresh interpreter and must never inherit this inventory's imports.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import ast
import importlib
from pathlib import Path
surface = {SURFACE!r}
for module, expected in surface.items():
    loaded = importlib.import_module(module)
    tree = ast.parse(Path(loaded.__file__).read_text())
    declared = next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == '__all__' for t in node.targets))
    assert isinstance(declared.value, ast.Tuple), module
    assert ast.literal_eval(declared.value) == expected, module
    local = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            local.add(node.name)
        elif isinstance(node, ast.TypeAlias):
            local.add(node.name.id)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            local.update(t.id for t in targets if isinstance(t, ast.Name))
    assert set(expected) <= local, (module, set(expected) - local)
    for name in expected:
        getattr(loaded, name)
""",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _authored_imports(source: str) -> list[ast.ImportFrom]:
    # Parse statements rather than whole snippets: guides can contain ellipses,
    # top-level await and omitted bodies, and imports can span several lines.
    statements = re.findall(r"(?m)^ *from etude[\w.]* import (?:\([^)]*\)|[^\n]+)", source)
    return [
        node
        for statement in statements
        for node in ast.walk(ast.parse(statement.strip()))
        if isinstance(node, ast.ImportFrom)
    ]


def test_authored_examples_use_supported_imports() -> None:
    sources = [(path, path.read_text()) for path in (ROOT / "examples").glob("*.py")]
    for path in [ROOT / "README.md", *(ROOT / "docs").glob("*.md"), ROOT / "release/UNRELEASED.md"]:
        text = path.read_text()
        for block in re.finditer(r"```python\n(.*?)```", text, re.DOTALL):
            if text[: block.start()].rstrip().endswith("<!-- implementation-reference -->"):
                continue
            sources.append((path, block[1]))
    for path, source in sources:
        for node in _authored_imports(source):
            assert node.module in SURFACE, (path, node.module)
            assert {alias.name for alias in node.names} <= set(SURFACE[node.module]), (
                path,
                node.module,
            )
