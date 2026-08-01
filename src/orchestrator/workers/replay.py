"""Recording and replaying worker results (D18).

Record a run once against live workers; replay it forever. Two payoffs:

- **Engine tests never call a model.** The scheduler, gates, invalidation, and
  rollback are all exercised deterministically and in milliseconds.
- **A demo is reproducible.** The same scenario runs identically in front of a
  reviewer, with no API calls, no latency, and no chance that today's sampling
  produces a different graph than yesterday's.

A missing fixture is an error, never a pass. Silently succeeding on unrecorded
input would make replay runs meaningless — and worse, would look like a green run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from orchestrator.engine.plan import Node
from orchestrator.gates.facts import Fact, FactSet, FactSource
from orchestrator.workers.base import (
    ProducedArtifact,
    Worker,
    WorkerError,
    WorkerResult,
    WorkScope,
)


def fixture_key(node: Node, inputs: FactSet) -> str:
    """Identify a recording by the node and the inputs it saw.

    Inputs are part of the key because the same node given different upstream
    artifacts is different work — replaying the first recording over the second
    would fabricate a result that never happened.
    """
    seen = {key: repr(fact.value) for key, fact in sorted(inputs.items())}
    payload = json.dumps({"node": node.id, "inputs": seen}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class ReplayWorker:
    """Serves recorded results from `fixtures/<node_id>/<key>.json`."""

    name = "replay"

    def __init__(self, fixtures_dir: Path | str = "fixtures") -> None:
        self.fixtures_dir = Path(fixtures_dir)

    def run(self, node: Node, inputs: FactSet, scope: WorkScope) -> WorkerResult:
        path = self.path_for(node, inputs)
        if not path.exists():
            raise WorkerError(
                f"no recording for '{node.id}' at {path}. Record one with "
                f"RecordingWorker, or run this node live — replaying a node that "
                f"was never recorded would invent a result."
            )
        return decode(json.loads(path.read_text()))

    def path_for(self, node: Node, inputs: FactSet) -> Path:
        return self.fixtures_dir / node.id / f"{fixture_key(node, inputs)}.json"


class RecordingWorker:
    """Delegates to a real worker and saves what came back.

    Wrap a live worker for one run; every result lands in `fixtures/` and every
    later run can replay it.
    """

    def __init__(self, inner: Worker, fixtures_dir: Path | str = "fixtures") -> None:
        self.inner = inner
        self.fixtures_dir = Path(fixtures_dir)
        self.name = f"recording:{inner.name}"

    def run(self, node: Node, inputs: FactSet, scope: WorkScope) -> WorkerResult:
        result = self.inner.run(node, inputs, scope)

        path = self.fixtures_dir / node.id / f"{fixture_key(node, inputs)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(encode(result), indent=2, sort_keys=True))
        return result


def encode(result: WorkerResult) -> dict:
    """Serialise a result, preserving fact provenance.

    Provenance has to survive the round trip: a replayed AGENT fact must stay
    inadmissible (D4), or replay would quietly launder self-reports into evidence.
    """
    return {
        "facts": {
            key: {
                "value": fact.value,
                "source": str(fact.source),
                "produced_by": fact.produced_by,
            }
            for key, fact in result.facts.items()
        },
        "artifacts": [
            {"name": a.name, "content": a.content, "path": a.path} for a in result.artifacts
        ],
        "consumed": list(result.consumed),
        "model": result.model,
        "prompt_ref": result.prompt_ref,
        "duration_ms": result.duration_ms,
    }


def decode(payload: dict) -> WorkerResult:
    return WorkerResult(
        facts={
            key: Fact(
                value=entry["value"],
                source=FactSource(entry["source"]),
                produced_by=entry.get("produced_by"),
            )
            for key, entry in payload.get("facts", {}).items()
        },
        artifacts=tuple(
            ProducedArtifact(name=a["name"], content=a["content"], path=a.get("path"))
            for a in payload.get("artifacts", [])
        ),
        consumed=tuple(payload.get("consumed", [])),
        model=payload.get("model"),
        prompt_ref=payload.get("prompt_ref"),
        duration_ms=payload.get("duration_ms"),
    )
