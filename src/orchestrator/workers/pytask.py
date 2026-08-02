"""The `py:` executor: nodes whose work is deterministic code we own.

`sh:` covers commands whose exit code is the observation. `py:` covers work that
is deterministic but not a subprocess — applying a severity policy, scanning
source for a known pattern, deriving stubs from a contract. Splitting them was
the point of making `run:` declare its scheme; this is the half that was missing.

A task is an ordinary function with one argument and one return value:

    def triage_ambiguities(task: Task) -> TaskOutput: ...

It receives the artifact bodies it needs and returns facts and artifacts. It does
**not** receive the session: durable state is the scheduler's, on the scheduler's
thread, and a task reaching into it would put database writes on a worker thread
for no benefit.

Files a task writes go through `TaskOutput.files` rather than the filesystem
directly, so the same `WorkScope` that governs a code agent governs a derivation
too. A task that wrote its own files would be the one execution path where D7
did not apply.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

from orchestrator.engine.plan import Node, RunScheme
from orchestrator.gates.facts import Fact, FactSource
from orchestrator.workers.base import (
    ProducedArtifact,
    WorkerError,
    WorkerResult,
    WorkInputs,
    WorkScope,
)


@dataclass(frozen=True, slots=True)
class Task:
    """What a `py:` callable is given."""

    node: Node
    inputs: WorkInputs
    scope: WorkScope
    cwd: Path

    @property
    def params(self) -> dict[str, Any]:
        """What the plan configured this task with.

        A task that hardcoded the target's test command would put knowledge of
        the target inside the orchestrator (D3). The plan says it, the profile
        supplies it, and the task reads it here.
        """
        return self.node.params

    @staticmethod
    def needs_params(*names: str):
        """Declare the params a task cannot run without, for preflight to check.

        Discovered the expensive way: `design` declared `max_turns` and
        `timeout_s`, its verify probes inherited exactly those, and both `py:`
        checks needed `root`. Every check ERRORed *after* a twelve-minute code
        agent session had already finished — and an ERROR cannot insert a fix
        node, so the run escalated to a human over a two-word plan omission.

        A run should refuse to start when its plan names checks the engine
        cannot perform (§preflight). This is what makes that checkable for
        params as well as for predicates.
        """

        def declare(fn):
            fn.required_params = names
            return fn

        return declare

    def param(self, name: str, default: Any = None) -> Any:
        value = self.node.params.get(name, default)
        if value is None:
            raise WorkerError(
                f"task '{self.node.run_target}' needs param '{name}' on node "
                f"'{self.node.id}'. Declared: {sorted(self.node.params) or '(none)'}"
            )
        return value

    def require(self, name: str) -> str:
        """Fetch an input the task cannot work without.

        Raises rather than defaulting: a policy applied to an absent register
        would return a confident, empty answer.
        """
        if name not in self.inputs:
            raise WorkerError(
                f"task '{self.node.run_target}' needs input '{name}', which this run "
                f"has not produced. Available: {sorted(self.inputs) or '(none)'}"
            )
        return self.inputs[name]


@dataclass(slots=True)
class TaskOutput:
    """What a `py:` callable returns.

    Facts carry no provenance argument on purpose — the worker stamps them
    `DERIVED`, because that is what they are, and a task that could declare its
    own facts `TOOL`-sourced could launder its opinions into evidence (D4).
    """

    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)


class PyWorker:
    """Executes `py:` nodes by importing and calling them."""

    name = "py"

    def __init__(self, *, cwd: Path | str = ".") -> None:
        self.cwd = Path(cwd)

    def run(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> WorkerResult:
        if node.run_scheme is not RunScheme.PY:
            raise WorkerError(
                f"node '{node.id}' has run scheme '{node.run_scheme}'; "
                f"PyWorker executes 'py:' only"
            )

        task_fn = resolve(node.run_target or "")
        task = Task(node=node, inputs=inputs, scope=scope, cwd=self.cwd)

        try:
            output = task_fn(task)
        except WorkerError:
            raise
        except Exception as exc:  # noqa: BLE001 — a broken task is the ERROR path
            raise WorkerError(
                f"task '{node.run_target}' raised {type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(output, TaskOutput):
            raise WorkerError(
                f"task '{node.run_target}' returned {type(output).__name__}; "
                f"a py: task must return TaskOutput"
            )

        written, denied = self._write(output.files, scope)
        return WorkerResult(
            facts={
                **{
                    key: Fact(value, FactSource.DERIVED, node.run_target)
                    for key, value in output.facts.items()
                },
                f"{node.id}.files_written": Fact(len(written), FactSource.VALIDATOR, "scope-guard"),
                f"{node.id}.scope_denials": Fact(len(denied), FactSource.VALIDATOR, "scope-guard"),
            },
            artifacts=tuple(
                ProducedArtifact(name, body) for name, body in output.artifacts.items()
            ),
            consumed=tuple(sorted(inputs)),
        )

    def _write(self, files: dict[str, str], scope: WorkScope) -> tuple[list[str], list[str]]:
        """Write what the scope permits; record what it refused.

        The same rule a code agent gets. A derivation is deterministic, not
        privileged.
        """
        written: list[str] = []
        denied: list[str] = []

        for relative, content in files.items():
            if not scope.permits(relative):
                denied.append(relative)
                continue
            path = self.cwd / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            written.append(relative)

        if denied:
            raise WorkerError(
                f"task attempted to write outside its scope {list(scope.allowed)}: {denied}"
            )
        return written, denied

    def describe(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> dict[str, Any]:
        issues: list[str] = []
        try:
            resolve(node.run_target or "")
        except WorkerError as exc:
            issues.append(str(exc))

        return {
            "worker": self.name,
            "issues": issues,
            "calls": node.run_target,
            "cwd": str(self.cwd),
            "may_write": list(scope.allowed),
            "inputs": sorted(inputs),
        }


def resolve(target: str) -> Callable[[Task], TaskOutput]:
    """Import `module.path.callable`, or say precisely what went wrong.

    Split from the right so a dotted module path is unambiguous — the last
    segment is the attribute, everything before it is the module.
    """
    if "." not in target:
        raise WorkerError(
            f"'{target}' is not a dotted path to a callable, e.g. "
            f"'orchestrator.policy.triage_ambiguities'"
        )

    module_name, _, attribute = target.rpartition(".")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise WorkerError(f"cannot import '{module_name}' for task '{target}': {exc}") from exc

    task_fn = getattr(module, attribute, None)
    if task_fn is None:
        raise WorkerError(f"'{module_name}' has no attribute '{attribute}'")
    if not callable(task_fn):
        raise WorkerError(f"'{target}' is not callable")
    return task_fn
