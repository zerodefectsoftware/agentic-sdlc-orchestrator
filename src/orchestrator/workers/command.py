"""Dispatch a command node by its declared scheme.

`tool` and `derive` nodes both say what to run; `run:` says how. Splitting the
schemes was pointless without something that reads them, and routing every
command node to the shell worker was the gap `--dry-run` found.
"""

from __future__ import annotations

from orchestrator.engine.plan import Node, RunScheme
from orchestrator.workers.base import Worker, WorkerError, WorkerResult, WorkInputs, WorkScope
from orchestrator.workers.pytask import PyWorker
from orchestrator.workers.tool import ToolWorker


class CommandWorker:
    """Routes `sh:` to a subprocess and `py:` to an imported callable."""

    name = "command"

    def __init__(self, *, shell: Worker | None = None, python: Worker | None = None) -> None:
        self._by_scheme: dict[RunScheme, Worker] = {
            RunScheme.SH: shell or ToolWorker(),
            RunScheme.PY: python or PyWorker(),
        }

    def _pick(self, node: Node) -> Worker:
        worker = self._by_scheme.get(node.run_scheme)
        if worker is None:
            raise WorkerError(
                f"node '{node.id}' declares no runnable command "
                f"(run={node.run!r}); a command node needs a 'py:' or 'sh:' target"
            )
        return worker

    def run(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> WorkerResult:
        return self._pick(node).run(node, inputs, scope)

    def describe(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> dict:
        try:
            return self._pick(node).describe(node, inputs, scope)
        except WorkerError as exc:
            return {"worker": "(unroutable)", "issues": [str(exc)], "run": node.run}
