"""Collecting the bundle.

Reads across four stores — run state, lineage, gate records, artifacts — and
assembles what was recorded. It **computes nothing**: a traceability matrix
recomputed here could differ from the one that actually gated, and the bundle
would then document a decision nobody took (§4.5).

So the check details below are the strings the evaluator produced at the time,
read back rather than regenerated.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.evidence.bundle import (
    ApprovalRecord,
    ArtifactRecord,
    AttemptRecord,
    CheckRecord,
    EvidenceBundle,
    GateOutcome,
    NodeRecord,
)
from orchestrator.lineage import query
from orchestrator.metrics import run_metrics
from orchestrator.state import store
from orchestrator.state.models import Artifact, NodeExecution, Run


def assemble(session: Session, run: Run) -> EvidenceBundle:
    """Build the bundle for a run."""
    stale = {item.approval.id for item in query.stale_approvals(session, run)}

    return EvidenceBundle(
        run_id=run.id,
        plan=run.plan_name,
        plan_version=run.plan_version,
        requirement_path=run.requirement_path,
        target_profile=run.target_profile,
        status=str(run.status),
        stop_reason=run.stop_reason,
        started_at=run.started_at,
        finished_at=run.finished_at,
        nodes=[_node(execution) for execution in store.all_nodes(session, run)],
        approvals=[
            _approval(approval, stale=approval.id in stale) for approval in run.approvals
        ],
        artifacts=[_artifact(session, artifact) for artifact in _artifacts(session, run)],
        metrics=run_metrics(session, run).summary(),
    )


def _node(execution: NodeExecution) -> NodeRecord:
    return NodeRecord(
        node_id=execution.node_id,
        kind=execution.kind,
        stage=execution.stage,
        status=str(execution.status),
        inserted=execution.inserted,
        attempts=[
            AttemptRecord(
                number=attempt.number,
                worker=attempt.worker,
                model=attempt.model,
                effort=attempt.effort,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                error=attempt.error,
            )
            for attempt in execution.attempts
        ],
        gates=[
            GateOutcome(
                node_id=execution.node_id,
                stage=execution.stage,
                gate=record.gate,
                verdict=record.verdict,
                evaluator=record.evaluator,
                evaluated_at=record.evaluated_at,
                attempt=attempt.number,
                checks=[
                    CheckRecord(
                        check=check.get("check", ""),
                        verdict=check.get("verdict", ""),
                        detail=check.get("detail", ""),
                        observed=check.get("observed"),
                    )
                    for check in record.checks
                ],
            )
            for attempt in execution.attempts
            for record in attempt.gate_records
        ],
    )


def _approval(approval, *, stale: bool) -> ApprovalRecord:
    return ApprovalRecord(
        node_id=approval.node_id,
        decision=str(approval.decision),
        decided_by=approval.decided_by,
        decided_at=approval.decided_at,
        note=approval.note,
        covers=[binding.artifact.ref for binding in approval.bindings],
        stale=stale,
    )


def _artifact(session: Session, artifact: Artifact) -> ArtifactRecord:
    provenance = query.provenance(session, artifact)
    return ArtifactRecord(
        name=artifact.name,
        version=artifact.version,
        content_hash=artifact.content_hash,
        path=artifact.path,
        produced_by_node=provenance.produced_by_node,
        created_at=artifact.created_at,
    )


def _artifacts(session: Session, run: Run) -> list[Artifact]:
    from sqlalchemy import select

    return list(
        session.scalars(
            select(Artifact)
            .where(Artifact.run_id == run.id)
            .order_by(Artifact.name, Artifact.version)
        )
    )
