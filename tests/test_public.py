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
    "etude.train.tracking": ("EtudeWarning", "TrainingRun", "local_launch", "seal_runs", "track"),
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


def test_supported_names_are_locally_declared() -> None:
    declared_modules: set[str] = set()
    for path in (ROOT / "src/etude").rglob("*.py"):
        tree = ast.parse(path.read_text())
        declarations = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
        ]
        if not declarations:
            continue
        module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
        declared_modules.add(module)
        [declaration] = declarations
        assert isinstance(declaration, ast.Tuple), module
        assert ast.literal_eval(declaration) == SURFACE[module], module
        local: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                local.add(node.name)
            elif isinstance(node, ast.TypeAlias):
                local.add(node.name.id)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                local.update(target.id for target in targets if isinstance(target, ast.Name))
        assert set(SURFACE[module]) <= local, (module, set(SURFACE[module]) - local)
    assert declared_modules == set(SURFACE)


def test_supported_names_resolve() -> None:
    # Keep broad author imports out of pytest's process; startup checks must not
    # inherit this inventory's imports. Syntax and ownership are checked above.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import importlib
for module, names in {SURFACE!r}.items():
    loaded = importlib.import_module(module)
    assert loaded.__all__ == names, module
    for name in names:
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
