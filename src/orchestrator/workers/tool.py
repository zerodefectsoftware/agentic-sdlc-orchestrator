"""The tool worker: run a command, observe what happened.

The workhorse of the gate layer. Every deterministic check — pytest, ruff, a
schema validator — reaches a gate through here, and the facts it produces are
TOOL-sourced, which is what makes them admissible as evidence (D4).

Facts are namespaced by the command's basename rather than the node id, because
that is what plans actually say: the `tests` node runs pytest, and its gate reads
`pytest.exit_code`. Naming facts after the node would make every gate expression
depend on where the command happened to be wired in.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from orchestrator.config import get_settings
from orchestrator.engine.plan import Node, RunScheme
from orchestrator.gates.facts import Fact, FactSet, FactSource
from orchestrator.workers.base import WorkerError, WorkerResult, WorkInputs, WorkScope

MAX_CAPTURED = 20_000  # keep a run's state file readable; the full log stays on disk


class ToolWorker:
    """Executes `sh:` nodes as subprocesses."""

    name = "tool"

    def __init__(self, *, cwd: Path | str = ".", timeout: int | None = None) -> None:
        self.cwd = Path(cwd)
        self.timeout = timeout if timeout is not None else get_settings().tool_timeout

    def run(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> WorkerResult:
        if node.run is None or node.run_scheme is not RunScheme.SH:
            raise WorkerError(
                f"node '{node.id}' is not a shell command "
                f"(run={node.run!r}); ToolWorker handles 'sh:' only"
            )

        command = node.run_target or ""
        namespace = fact_namespace(command)
        started = time.monotonic()

        try:
            completed = subprocess.run(  # noqa: S603 — the command comes from an authored plan
                shlex.split(command),
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            # No exit code was ever produced, so there is no fact to gate on.
            # This is the ERROR path, not a failing test.
            raise WorkerError(f"command not found: {command!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WorkerError(f"command timed out after {self.timeout}s: {command!r}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        return WorkerResult(
            facts=observed(namespace, completed, command, duration_ms),
            duration_ms=duration_ms,
        )


def fact_namespace(command: str) -> str:
    """`.venv/bin/pytest target/tests` → `pytest`.

    Plans gate on the tool's own name, so the namespace follows the executable
    rather than the node. Falls back to a safe token if the command is unusual.
    """
    parts = shlex.split(command)
    if not parts:
        return "command"
    stem = Path(parts[0]).name
    return stem.replace("-", "_") or "command"


def observed(
    namespace: str, completed: subprocess.CompletedProcess[str], command: str, duration_ms: int
) -> FactSet:
    """The facts a command run makes available to a gate."""
    return {
        f"{namespace}.exit_code": Fact(completed.returncode, FactSource.TOOL, command),
        f"{namespace}.stdout": Fact(_clip(completed.stdout), FactSource.TOOL, command),
        f"{namespace}.stderr": Fact(_clip(completed.stderr), FactSource.TOOL, command),
        f"{namespace}.duration_ms": Fact(duration_ms, FactSource.TOOL, command),
    }


def _clip(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= MAX_CAPTURED:
        return text
    return f"{text[:MAX_CAPTURED]}\n… [{len(text) - MAX_CAPTURED} more characters]"
