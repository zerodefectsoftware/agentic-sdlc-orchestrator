"""The live worker: dispatch by node kind.

One `Worker` to the scheduler, several underneath. Which is why the kind lives
on the node rather than the worker — the plan says what sort of work this is,
and the runtime decides what can perform it.

`derive` currently routes to the tool worker, because every derivation in the
shipped plan is a command. A derivation that is genuinely code generation from a
contract would want its own executor; there is not one yet, and pretending
otherwise would hide the gap.
"""

from __future__ import annotations

from orchestrator.engine.plan import Node, NodeKind
from orchestrator.workers.agent import AgentWorker
from orchestrator.workers.base import Worker, WorkerError, WorkerResult, WorkInputs, WorkScope
from orchestrator.workers.codeagent import CodeAgentWorker
from orchestrator.workers.tool import ToolWorker


class LiveWorker:
    """Routes a node to whichever runtime can execute its kind."""

    name = "live"

    def __init__(
        self,
        *,
        agent: Worker | None = None,
        tool: Worker | None = None,
        codeagent: Worker | None = None,
    ) -> None:
        self._by_kind: dict[NodeKind, Worker] = {
            NodeKind.AGENT: agent or AgentWorker(),
            NodeKind.TOOL: tool or ToolWorker(),
            NodeKind.DERIVE: tool or ToolWorker(),
            NodeKind.CODEAGENT: codeagent or CodeAgentWorker(),
        }

    def describe(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> dict:
        """What a node would dispatch to, without dispatching."""
        worker = self._by_kind.get(node.kind)
        if worker is None:
            return {"worker": "(none)", "issues": [], "note": "handled by the scheduler"}
        return worker.describe(node, inputs, scope)

    def run(self, node: Node, inputs: WorkInputs, scope: WorkScope) -> WorkerResult:
        worker = self._by_kind.get(node.kind)
        if worker is None:
            # human and fanout never reach a worker — the scheduler handles both.
            raise WorkerError(f"no live runtime for node kind '{node.kind}'")
        return worker.run(node, inputs, scope)
