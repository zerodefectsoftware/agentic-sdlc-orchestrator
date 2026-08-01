"""Recording lineage: artifacts, the attempts that produced them, and their inputs.

Append-only. `record_artifact` never updates an existing row — a re-run creates
version N+1 — which is what makes staleness computable rather than a matter of
remembering to check (§4.5, D10).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.state.models import (
    Approval,
    ApprovalBinding,
    Artifact,
    ArtifactInput,
    Attempt,
    Decision,
    Run,
    content_hash,
    utcnow,
)


def record_artifact(
    session: Session,
    run: Run,
    *,
    name: str,
    content: str | bytes,
    produced_by: Attempt | None = None,
    path: str | None = None,
) -> Artifact:
    """Record a new version of `name`.

    Identical content still creates a new version. Re-deriving an artifact is an
    event worth recording even when the bytes are unchanged — it means a node
    ran again, which is exactly what retry-frequency measures.
    """
    latest = latest_version_number(session, run, name)
    artifact = Artifact(
        run_id=run.id,
        name=name,
        version=latest + 1,
        content_hash=content_hash(content),
        path=path,
        produced_by_id=produced_by.id if produced_by else None,
    )
    session.add(artifact)
    session.flush()
    return artifact


def record_inputs(session: Session, attempt: Attempt, artifacts: list[Artifact]) -> None:
    """Record which artifact versions an attempt consumed.

    The version matters: "design consumed the requirement register" is not
    traceable, but "design consumed requirements@v2" is.
    """
    session.add_all(
        ArtifactInput(attempt_id=attempt.id, artifact_id=artifact.id) for artifact in artifacts
    )
    session.flush()


def latest_version_number(session: Session, run: Run, name: str) -> int:
    return (
        session.scalar(
            select(func.max(Artifact.version)).where(
                Artifact.run_id == run.id, Artifact.name == name
            )
        )
        or 0
    )


def latest(session: Session, run: Run, name: str) -> Artifact | None:
    return session.scalar(
        select(Artifact)
        .where(Artifact.run_id == run.id, Artifact.name == name)
        .order_by(Artifact.version.desc())
        .limit(1)
    )


def request_approval(
    session: Session, run: Run, *, node_id: str, artifacts: list[Artifact]
) -> Approval:
    """Open a pending approval bound to specific artifact versions.

    Binding happens when the approval is *requested*, so the human is shown, and
    later recorded as having seen, exactly these versions.
    """
    approval = Approval(run_id=run.id, node_id=node_id)
    session.add(approval)
    session.flush()
    session.add_all(
        ApprovalBinding(approval_id=approval.id, artifact_id=artifact.id)
        for artifact in artifacts
    )
    session.flush()
    return approval


def decide(
    session: Session,
    approval: Approval,
    *,
    decision: Decision,
    decided_by: str,
    note: str | None = None,
) -> Approval:
    approval.decision = decision
    approval.decided_by = decided_by
    approval.note = note
    approval.decided_at = utcnow()
    session.flush()
    return approval


def revert_to_pending(session: Session, approval: Approval, reason: str) -> Approval:
    """Return a stale approval to pending (§6).

    Not a deletion: the original decision stays in the audit trail with the
    reason it stopped counting. "This was approved, then the thing it approved
    was replaced" is the sequence a reviewer needs to see.
    """
    approval.decision = Decision.PENDING
    approval.note = f"reverted to pending: {reason}"
    approval.decided_at = None
    session.flush()
    return approval
