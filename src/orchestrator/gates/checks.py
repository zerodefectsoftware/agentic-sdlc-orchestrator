"""Checks the engine runs after a node's work, to produce the facts its gate reads.

Three gate expressions in the shipped plans named facts that nothing produced —
`coverage.percent`, `imports.resolve`, `ruff.exit_code` on nodes that never ran
ruff. Those gates ERRORed, which is the correct verdict for a check that cannot
be performed and a useless one to ship.

The fix is not to let the producing node report on itself. `scaffold` derives the
package; asking the derivation whether its own output imports is the same
category of evidence as an agent saying its tests pass (D4). So the plan declares
`verify:` entries, the engine runs them after the work, and the facts they
observe are what the gate reads.

Each check is an ordinary `py:` task. `sh:` checks — ruff, pytest — need nothing
here: their exit code *is* the observation, and the tool worker already records it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from orchestrator.workers.base import WorkerError
from orchestrator.workers.pytask import Task, TaskOutput

IMPORT_PROBE = """
import importlib, json, sys

failed = {}
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except BaseException as exc:            # noqa: BLE001 — any failure is a failure
        failed[name] = f"{type(exc).__name__}: {exc}"

print(json.dumps(failed))
"""


def imports_resolve(task: Task) -> TaskOutput:
    """Import every module under the target root, in a subprocess.

    A separate interpreter because importing into this one would execute target
    code inside the orchestrator's process — the target is the thing under
    scrutiny, and a module with a side effect at import time would run it here.

    A tree with no modules is not a pass. A scaffold that produced nothing and a
    scaffold whose every module imports are different outcomes, and only one of
    them should let a gate through.
    """
    root = Path(task.param("root"))
    package_parent = task.cwd / root.parent
    modules = _modules_under(task.cwd / root)

    if not modules:
        return TaskOutput(
            facts={
                "imports.resolve": False,
                "imports.modules": 0,
                "imports.unresolved": [f"no importable modules under {root}"],
            }
        )

    completed = subprocess.run(  # noqa: S603 — a fixed probe, not a plan-supplied command
        [sys.executable, "-c", IMPORT_PROBE, json.dumps(modules)],
        cwd=package_parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise WorkerError(
            f"the import probe itself failed (exit {completed.returncode}): "
            f"{completed.stderr.strip()[:400]}"
        )

    failed = json.loads(completed.stdout or "{}")
    return TaskOutput(
        facts={
            "imports.resolve": not failed,
            "imports.modules": len(modules),
            "imports.unresolved": [f"{name}: {why}" for name, why in sorted(failed.items())],
        }
    )


def report_coverage(task: Task) -> TaskOutput:
    """Read the coverage report the test command wrote.

    Parsing a tool's own report keeps the number TOOL-shaped: the orchestrator
    does not measure coverage, it reads what the measurement said. A missing
    report is an ERROR rather than 0% — 0% is a finding about the target, and
    "the report was never written" is a finding about the run.
    """
    report = task.cwd / task.params.get("coverage_report", "coverage.json")
    if not report.exists():
        raise WorkerError(
            f"no coverage report at {report} — the test command must write one "
            f"(--cov-report=json) before a coverage gate can be evaluated"
        )

    try:
        totals = json.loads(report.read_text())["totals"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise WorkerError(f"coverage report at {report} is not readable: {exc}") from exc

    return TaskOutput(
        facts={
            "coverage.percent": round(float(totals["percent_covered"]), 2),
            "coverage.statements": totals.get("num_statements", 0),
            "coverage.missing": totals.get("missing_lines", 0),
        }
    )


def _modules_under(absolute: Path) -> list[str]:
    """`target/shortener/api/store.py` → `shortener.api.store`.

    Importable names, relative to the package's parent — which is what the probe
    runs from. `__init__` collapses to its package, as import does.
    """
    if not absolute.exists():
        return []

    modules: list[str] = []
    for path in sorted(absolute.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(absolute.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.append(".".join(parts))

    return sorted(set(modules))
