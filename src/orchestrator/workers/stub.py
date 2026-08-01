"""The stub worker: scripted results, including failures that are hard to arrange.

This is what makes the failure paths testable. A rollback that has never been
exercised is a claim, not a control — and arranging a real gate failure, a real
retry exhaustion, or a real worker crash on demand is otherwise slow and flaky.

Not a mock in the usual sense: it implements the same interface and returns real
`WorkerResult` objects. The engine cannot tell the difference, which is the
point — tests exercise the actual scheduler, not a parallel code path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from orchestrator.engine.plan import Node
from orchestrator.gates.facts import Fact, FactSet, FactSource
from orchestrator.workers.base import (
    ProducedArtifact,
    WorkerError,
    WorkerResult,
    WorkScope,
)

Script = WorkerResult | Exception | Callable[[Node], WorkerResult]


class StubWorker:
    """Returns pre-arranged results, per node.

    A node can be given a *sequence* of results, so "fails, then passes after the
    fix node" — the repair loop's whole reason for existing — is one line of setup.
    """

    name = "stub"

    def __init__(
        self,
        results: dict[str, Script | Iterable[Script]] | None = None,
        *,
        default: Script | None = None,
    ) -> None:
        self._scripts: dict[str, list[Script]] = {}
        self._default = default
        self.calls: list[str] = []

        for node_id, script in (results or {}).items():
            self._scripts[node_id] = (
                list(script)
                if isinstance(script, list | tuple)
                else [script]  # a single result is reused for every call
            )

    def run(self, node: Node, inputs: FactSet, scope: WorkScope) -> WorkerResult:
        self.calls.append(node.id)
        script = self._next(node.id)

        if isinstance(script, Exception):
            raise script
        if callable(script):
            return script(node)
        return script

    def _next(self, node_id: str) -> Script:
        queued = self._scripts.get(node_id)
        if queued:
            # The last entry repeats, so a node can be given a terminal steady state.
            return queued.pop(0) if len(queued) > 1 else queued[0]
        if self._default is not None:
            return self._default
        raise WorkerError(
            f"StubWorker has no scripted result for '{node_id}'; "
            f"scripted: {sorted(self._scripts) or '(none)'}"
        )


def passing(namespace: str = "pytest", **artifacts: str) -> WorkerResult:
    """A clean run: exit code 0, plus any artifacts named as keyword arguments."""
    return WorkerResult(
        facts={f"{namespace}.exit_code": Fact(0, FactSource.TOOL, "stub")},
        artifacts=tuple(ProducedArtifact(name, content) for name, content in artifacts.items()),
    )


def failing(namespace: str = "pytest", exit_code: int = 1) -> WorkerResult:
    """Work that ran and did not hold — the gate should FAIL."""
    return WorkerResult(facts={f"{namespace}.exit_code": Fact(exit_code, FactSource.TOOL, "stub")})


def unevaluable() -> WorkerResult:
    """Work that produced no facts — the gate should ERROR, not FAIL.

    The distinction the failure policy rests on: nothing here is repairable by
    changing the target's code.
    """
    return WorkerResult()


def self_reported(claim: str = "impl.complete") -> WorkerResult:
    """An agent asserting its own success — inadmissible as gate evidence (D4)."""
    return WorkerResult(facts={claim: Fact(True, FactSource.AGENT, "stub-agent")})
