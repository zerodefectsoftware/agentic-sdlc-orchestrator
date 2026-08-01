"""The Worker interface (D18).

One seam between the engine and everything that actually does work. Nothing in
the scheduler knows whether the thing behind it was a model call, a subprocess,
or a human.

That seam is what makes a non-deterministic system testable. Worker outputs are
recorded once and replayed, so gate evaluation, invalidation cascades, retry
budgets, rollback, and stale-approval detection are all covered by fast,
deterministic tests that never call a model.

A worker returns **facts and artifacts, never a verdict.** Deciding whether the
work was acceptable belongs to the gate, evaluated against evidence the worker
did not get to interpret (D4).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from orchestrator.engine.plan import Node
from orchestrator.gates.facts import FactSet


class WorkerError(Exception):
    """The worker could not perform the work at all.

    Distinct from work that was performed and turned out wrong: that is a gate
    FAIL, and the gate is what should say so. This is the ERROR path.
    """


@dataclass(frozen=True, slots=True)
class ProducedArtifact:
    """Something the work produced, on its way to becoming a lineage record."""

    name: str
    content: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkScope:
    """Where a node may write, and what it may not touch.

    `frozen` implements D6 — during a repair loop the target's tests are
    immutable, because the cheapest route to a green suite is a weakened test.
    `allowed` implements D7 blast radius.
    """

    allowed: tuple[str, ...] = ()
    frozen: tuple[str, ...] = ()

    @classmethod
    def for_node(cls, node: Node) -> WorkScope:
        return cls(allowed=tuple(node.write_scope), frozen=tuple(node.freeze_paths))

    def permits(self, path: str) -> bool:
        if any(fnmatch.fnmatch(path, pattern) for pattern in self.frozen):
            return False
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.allowed)

    def violations(self, paths: list[str]) -> list[str]:
        """Which of `paths` this scope forbids.

        Detection, not prevention — prevention belongs to the runtime that
        actually executes the work (the agent harness's permission layer). This
        exists so a violation is caught even when the runtime cannot enforce it,
        which is the case for an arbitrary subprocess.
        """
        return [path for path in paths if not self.permits(path)]


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """What a worker observed and produced.

    Deliberately verdict-free. `facts` is evidence for a gate; `artifacts` is
    what the run now has. Neither says whether the work was good.
    """

    facts: FactSet = field(default_factory=dict)
    artifacts: tuple[ProducedArtifact, ...] = ()
    consumed: tuple[str, ...] = ()          # artifact refs read, for lineage edges
    model: str | None = None                # what actually ran, for the record
    prompt_ref: str | None = None
    duration_ms: int | None = None

    def artifact(self, name: str) -> ProducedArtifact:
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact
        raise KeyError(f"no artifact named '{name}' in this result")


@runtime_checkable
class Worker(Protocol):
    """Anything that can execute a node.

    `name` is recorded on every attempt, so a run's evidence always shows whether
    a result came from a live model, a replayed fixture, or a stub. An evidence
    bundle that cannot distinguish those is not evidence.
    """

    name: str

    def run(self, node: Node, inputs: FactSet, scope: WorkScope) -> WorkerResult: ...
