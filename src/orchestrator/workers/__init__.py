"""The Worker interface (D18) and its implementations.

One seam between the engine and everything that does work — a model call, a
subprocess, a human. The scheduler cannot tell them apart, which is what lets
engine tests replay recorded results and never call a model.

A worker returns facts and artifacts, never a verdict. Whether the work was
acceptable is the gate's judgment, made against evidence the worker did not get
to interpret (D4).
"""

from orchestrator.workers.agent import AgentWorker
from orchestrator.workers.base import (
    ProducedArtifact,
    Worker,
    WorkerError,
    WorkerResult,
    WorkInputs,
    WorkScope,
)
from orchestrator.workers.codeagent import CodeAgentWorker
from orchestrator.workers.live import LiveWorker
from orchestrator.workers.replay import RecordingWorker, ReplayWorker
from orchestrator.workers.stub import StubWorker
from orchestrator.workers.tool import ToolWorker

__all__ = [
    "AgentWorker",
    "CodeAgentWorker",
    "LiveWorker",
    "ProducedArtifact",
    "RecordingWorker",
    "ReplayWorker",
    "StubWorker",
    "ToolWorker",
    "Worker",
    "WorkerError",
    "WorkerResult",
    "WorkInputs",
    "WorkScope",
]
