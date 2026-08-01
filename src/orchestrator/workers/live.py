"""The live worker: dispatch by node kind.

One `Worker` to the scheduler, several underneath. Which is why the kind lives
on the node rather than the worker — the plan says what sort of work this is,
and the runtime decides what can perform it.

`tool` and `derive` both route to `CommandWorker`, which picks by scheme: `sh:`
runs a subprocess, `py:` imports and calls. The kinds differ in what they mean —
a derivation produces an artifact from a contract, a tool observes something —
but they are dispatched the same way.
"""

from __future__ import annotations

from orchestrator.engine.plan import Node, NodeKind
from orchestrator.workers.agent import AgentWorker
from orchestrator.workers.base import Worker, WorkerError, WorkerResult, WorkInputs, WorkScope
from orchestrator.workers.codeagent import CodeAgentWorker
from orchestrator.workers.command import CommandWorker


class LiveWorker:
    """Routes a node to whichever runtime can execute its kind."""

    name = "live"

    def __init__(
        self,
        *,
        agent: Worker | None = None,
        command: Worker | None = None,
        codeagent: Worker | None = None,
    ) -> None:
        commands = command or CommandWorker()
        self._by_kind: dict[NodeKind, Worker] = {
            NodeKind.AGENT: agent or AgentWorker(),
            NodeKind.TOOL: commands,
            NodeKind.DERIVE: commands,
            NodeKind.CODEAGENT: codeagent or CodeAgentWorker(),
        }

    def describe(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> dict:
        """What a node would dispatch to, without dispatching."""
        if _handled_by_scheduler(node):
            return {"worker": "(none)", "issues": [], "note": "handled by the scheduler"}
        return self._by_kind[node.kind].describe(node, inputs, scope)

    def run(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> WorkerResult:
        if _handled_by_scheduler(node):
            raise WorkerError(
                f"node '{node.id}' is handled by the scheduler, not a worker; "
                f"reaching here means the scheduler failed to intercept it"
            )
        return self._by_kind[node.kind].run(node, inputs, scope)


def _handled_by_scheduler(node: Node) -> bool:
    """Nodes the engine executes itself rather than dispatching.

    `human` blocks on a person, `fanout` reshapes the graph, and a node that
    emits the evidence bundle reads four stores — all of which belong on the
    scheduler's thread. A worker asked to describe one would report a false gap.
    """
    return node.kind in (NodeKind.HUMAN, NodeKind.FANOUT) or bool(node.emits)
