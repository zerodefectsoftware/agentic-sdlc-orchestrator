"""Baseline capture and regression comparison — the brownfield failure controls.

Two things greenfield never needs, because greenfield has nothing to break.

**Capture** records the state a rollback returns to, before any node writes. It
stores the file bodies rather than a reference to them: a rollback that can only
name the state it wanted is a rollback in the documentation only. It also records
which tests were *already* red, which is the part that makes the next step honest.

**Comparison** asks the only question that distinguishes a regression from an
inherited failure: is this test newly red? A gate on `pytest.exit_code == 0`
cannot answer it. Against a suite that was red on arrival, that gate blocks every
change forever; against one that was green, it silently accepts a change that
broke something the run then "fixed" by deleting a test. The set difference is
the check, and both sides of it come from running the target's own suite.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from pathlib import Path

from orchestrator.artifacts import Baseline
from orchestrator.workers.base import WorkerError
from orchestrator.workers.pytask import Task, TaskOutput

# pytest's own summary lines: `FAILED target/tests/test_x.py::test_y - AssertionError`.
FAILURE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

SNAPSHOT_SUFFIXES = (".py", ".md", ".toml", ".cfg", ".yaml", ".yml", ".json", ".txt")


def capture_baseline(task: Task) -> TaskOutput:
    """Record the target's current state and current failures."""
    command = task.param("command")
    root = Path(task.param("root"))

    completed = _run(command, task.cwd)
    files = _snapshot(task.cwd, root)
    failing = sorted(set(FAILURE.findall(completed.stdout or "")))

    baseline = Baseline(
        green=completed.returncode == 0,
        snapshot_ref=_digest(files),
        failing=failing,
        files=files,
    )
    return TaskOutput(
        facts={
            "baseline.green": baseline.green,
            "baseline.failing": len(failing),
            "baseline.files": len(files),
            "baseline.snapshot_ref": baseline.snapshot_ref,
        },
        artifacts={"baseline.snapshot": baseline.model_dump_json(indent=2)},
    )


def verify_no_regression(task: Task) -> TaskOutput:
    """Re-run the suite and compare its failures against the baseline's."""
    command = task.param("command")
    baseline = Baseline.model_validate_json(task.require("baseline.snapshot"))

    completed = _run(command, task.cwd)
    failing = sorted(set(FAILURE.findall(completed.stdout or "")))

    inherited = set(baseline.failing)
    regressed = sorted(set(failing) - inherited)
    fixed = sorted(inherited - set(failing))

    return TaskOutput(
        facts={
            "regression.new_failures": len(regressed),
            "regression.ids": regressed,
            "regression.inherited": len(inherited & set(failing)),
            "regression.fixed": len(fixed),
            "tests.exit_code": completed.returncode,
        }
    )


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _run(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the target's own test command.

    A missing command is an ERROR, not a failed check: a suite that never ran
    tells us nothing about whether anything regressed, and reporting that as
    "no regressions" is the one answer that must never be produced.
    """
    try:
        return subprocess.run(  # noqa: S603 — the command comes from the target profile
            shlex.split(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise WorkerError(f"could not run the target's test command {command!r}: {exc}") from exc


def _snapshot(cwd: Path, root: Path) -> dict[str, str]:
    absolute = cwd / root
    if not absolute.exists():
        return {}

    captured: dict[str, str] = {}
    for path in sorted(absolute.rglob("*")):
        if not path.is_file() or path.suffix not in SNAPSHOT_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        captured[str(path.relative_to(cwd))] = path.read_text()
    return captured


def _digest(files: dict[str, str]) -> str:
    """Content-addressed, so two identical trees produce the same ref."""
    hasher = hashlib.sha256()
    for path in sorted(files):
        hasher.update(path.encode())
        hasher.update(files[path].encode())
    return hasher.hexdigest()[:16]
