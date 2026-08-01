"""Querying lineage.

Two questions this graph exists to answer:

1. **Why does this artifact exist?** — walk backwards through produced-by and
   consumed edges to the requirement. This is what makes the brownfield scenario
   work: "why is this redirect a 301?" resolves to a decision record, and from
   there to the assumption that produced it.

2. **Which approvals no longer cover what exists?** — the D10 check, and the
   strongest governance control in the system (§5.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.state.models import (
    Approval,
    Artifact,
    ArtifactInput,
    Attempt,
    Decision,
    NodeExecution,
    Run,
)


@dataclass(frozen=True, slots=True)
class StaleApproval:
    """An approval granted against an artifact version that has been superseded."""

    approval: Approval
    artifact_name: str
    approved_version: int
    current_version: int

    def __str__(self) -> str:
        return (
            f"{self.approval.node_id}: approved {self.artifact_name}@v{self.approved_version}, "
            f"but v{self.current_version} now exists"
        )


@dataclass
class Provenance:
    """One step in an artifact's causal history."""

    artifact: Artifact
    produced_by_node: str | None = None
    attempt_number: int | None = None
    model: str | None = None
    inputs: list[Artifact] = field(default_factory=list)

    def __str__(self) -> str:
        origin = self.produced_by_node or "unknown"
        via = f" using {self.model}" if self.model else ""
        consumed = ", ".join(a.ref for a in self.inputs) or "nothing recorded"
        return f"{self.artifact.ref} ← {origin} (attempt {self.attempt_number}){via} ← {consumed}"


def stale_approvals(session: Session, run: Run) -> list[StaleApproval]:
    """Approvals whose bound artifacts have since been superseded (D10).

    This is what turns "the human approved it" into "the human approved *this*".
    An approval captured as a boolean cannot answer the question at all — which
    is why most human-in-the-loop systems don't.
    """
    stale: list[StaleApproval] = []
    approvals = session.scalars(
        select(Approval).where(
            Approval.run_id == run.id, Approval.decision == Decision.APPROVED
        )
    )

    for approval in approvals:
        for binding in approval.bindings:
            approved = binding.artifact
            current = session.scalar(
                select(Artifact)
                .where(Artifact.run_id == run.id, Artifact.name == approved.name)
                .order_by(Artifact.version.desc())
                .limit(1)
            )
            if current is not None and current.version > approved.version:
                stale.append(
                    StaleApproval(
                        approval=approval,
                        artifact_name=approved.name,
                        approved_version=approved.version,
                        current_version=current.version,
                    )
                )
    return stale


def provenance(session: Session, artifact: Artifact) -> Provenance:
    """One hop: what produced this artifact, and what did that consume."""
    attempt = artifact.produced_by
    if attempt is None:
        return Provenance(artifact=artifact)

    node = session.get(NodeExecution, attempt.node_execution_id)
    inputs = list(
        session.scalars(
            select(Artifact)
            .join(ArtifactInput, ArtifactInput.artifact_id == Artifact.id)
            .where(ArtifactInput.attempt_id == attempt.id)
        )
    )
    return Provenance(
        artifact=artifact,
        produced_by_node=node.node_id if node else None,
        attempt_number=attempt.number,
        model=attempt.model,
        inputs=inputs,
    )


def why(session: Session, artifact: Artifact, *, max_depth: int = 20) -> list[Provenance]:
    """The full causal chain behind an artifact, nearest cause first.

    Depth-bounded and cycle-guarded: lineage should be acyclic, but a traversal
    that hangs on malformed data is worse than one that stops and says so.
    """
    chain: list[Provenance] = []
    seen: set[str] = set()
    frontier = [artifact]

    while frontier and len(chain) < max_depth:
        current = frontier.pop(0)
        if current.id in seen:
            continue
        seen.add(current.id)

        step = provenance(session, current)
        chain.append(step)
        frontier.extend(inp for inp in step.inputs if inp.id not in seen)

    return chain


def artifact_history(session: Session, run: Run, name: str) -> list[Artifact]:
    """Every version of an artifact, oldest first — the re-derivation record."""
    return list(
        session.scalars(
            select(Artifact)
            .where(Artifact.run_id == run.id, Artifact.name == name)
            .order_by(Artifact.version)
        )
    )


def unproduced_artifacts(session: Session, run: Run) -> list[Artifact]:
    """Artifacts with no producing attempt — feeds G10's `lineage_complete`.

    An artifact nobody can account for is exactly what audit-grade traceability
    is supposed to make impossible.
    """
    return list(
        session.scalars(
            select(Artifact).where(
                Artifact.run_id == run.id, Artifact.produced_by_id.is_(None)
            )
        )
    )


def attempts_for(session: Session, run: Run) -> list[Attempt]:
    """Every attempt in a run, for metrics and the evidence bundle."""
    return list(
        session.scalars(
            select(Attempt)
            .join(NodeExecution, NodeExecution.id == Attempt.node_execution_id)
            .where(NodeExecution.run_id == run.id)
            .order_by(Attempt.started_at)
        )
    )
