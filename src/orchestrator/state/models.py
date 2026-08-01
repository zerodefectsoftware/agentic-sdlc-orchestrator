"""Durable state for runs, and the append-only records that become evidence.

Two of the three graphs from §3 live here, and the distinction between them is
the reason this schema looks the way it does:

- The **run graph** mutates. `NodeExecution.status` changes as work proceeds.
- The **lineage graph** is append-only. An artifact is never updated; a re-run
  produces a *new version*. Nothing that has been recorded is ever rewritten.

That append-only rule is what makes D10 mechanical rather than aspirational. An
approval records the exact artifact versions it was granted against, so
"approval of a superseded artifact is not approval" becomes a query rather than
a discipline.

One SQLAlchemy Base for both: they reference each other across the boundary
(an artifact is produced by an attempt), and splitting the metadata would buy
purity at the cost of foreign keys that actually work.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(moment: datetime) -> datetime:
    """Normalise a timestamp for arithmetic.

    SQLite does not preserve tzinfo, so a timestamp written as aware comes back
    naive. Comparing a freshly created timestamp with a reloaded one therefore
    raises — which would break metrics on exactly the runs that matter most, the
    ones resumed after a safe-stop.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    """Milliseconds between two recorded moments, or None if either is missing."""
    if start is None or end is None:
        return None
    return int((as_utc(end) - as_utc(start)).total_seconds() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


def content_hash(content: str | bytes) -> str:
    """Artifact identity. Two artifacts with the same hash are the same artifact."""
    payload = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(payload).hexdigest()


def _enum(enum_type: type[StrEnum]) -> Enum:
    """Store enums as readable strings — a governance database should be greppable."""
    return Enum(enum_type, native_enum=False, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# status vocabularies
# --------------------------------------------------------------------------- #


class RunStatus(StrEnum):
    RUNNING = "running"
    BLOCKED = "blocked"          # waiting on a human decision
    COMPLETED = "completed"
    FAILED = "failed"            # a gate failed past its retry budget
    STOPPED = "stopped"          # safe-stop: halted with state intact
    ROLLED_BACK = "rolled_back"


class NodeStatus(StrEnum):
    PENDING = "pending"          # dependencies not yet satisfied
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"            # gate said no
    ERRORED = "errored"          # gate could not be evaluated — see Verdict.ERROR
    BLOCKED = "blocked"          # awaiting approval
    STALE = "stale"              # invalidated by an upstream change (§6)
    SKIPPED = "skipped"          # optional node whose trigger never fired


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING = "pending"


# --------------------------------------------------------------------------- #
# the run graph — mutable
# --------------------------------------------------------------------------- #


class Run(Base):
    """One execution of one plan against one target."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    plan_name: Mapped[str] = mapped_column(String(64))
    plan_version: Mapped[int] = mapped_column(Integer)
    requirement_path: Mapped[str] = mapped_column(String(255))
    target_profile: Mapped[str] = mapped_column(String(255))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.RUNNING)
    stop_reason: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    nodes: Mapped[list[NodeExecution]] = relationship(back_populates="run", cascade="all, delete")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="run", cascade="all, delete")
    approvals: Mapped[list[Approval]] = relationship(back_populates="run", cascade="all, delete")


class NodeExecution(Base):
    """A node's state within a run.

    `node_id` is the plan's id for authored nodes, and a derived id for nodes the
    engine materialises at runtime — `impl:storage` for a fan-out child, `fix:tests`
    for an inserted repair node. Both are nodes; only one was in the template (§3).
    """

    __tablename__ = "node_executions"
    __table_args__ = (UniqueConstraint("run_id", "node_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[NodeStatus] = mapped_column(_enum(NodeStatus), default=NodeStatus.PENDING)
    inserted: Mapped[bool] = mapped_column(default=False)  # not in the authored plan

    # An inserted node has no entry in the plan file, so its definition is stored
    # here. Without this a resumed process could see the node but not dispatch it —
    # which would make safe-stop resumable only for runs that never inserted
    # anything, i.e. only the runs that never needed it.
    config: Mapped[dict | None] = mapped_column(JSON, default=None)
    extra_needs: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    run: Mapped[Run] = relationship(back_populates="nodes")
    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="node", cascade="all, delete", order_by="Attempt.number"
    )

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


class Attempt(Base):
    """One try at executing a node.

    Retries are separate attempts rather than mutations, because retry frequency
    and MTTR (§8) are computed from the sequence — overwriting would erase the
    measurement.
    """

    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("node_execution_id", "number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    node_execution_id: Mapped[str] = mapped_column(ForeignKey("node_executions.id"), index=True)
    number: Mapped[int] = mapped_column(Integer, default=1)
    worker: Mapped[str] = mapped_column(String(32))              # live | replay | stub
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    effort: Mapped[str | None] = mapped_column(String(16), default=None)
    prompt_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    node: Mapped[NodeExecution] = relationship(back_populates="attempts")
    gate_records: Mapped[list[GateRecord]] = relationship(
        back_populates="attempt", cascade="all, delete"
    )
    produced: Mapped[list[Artifact]] = relationship(back_populates="produced_by")
    consumed: Mapped[list[ArtifactInput]] = relationship(
        back_populates="attempt", cascade="all, delete"
    )


class GateRecord(Base):
    """A gate's verdict, kept exactly as it was reached.

    The evidence bundle reads these back rather than re-evaluating (§5.4). A gate
    re-run later can reach a different verdict, and the bundle must show the one
    that actually governed.
    """

    __tablename__ = "gate_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), index=True)
    gate: Mapped[str] = mapped_column(String(16))   # entry | exit
    verdict: Mapped[str] = mapped_column(String(16))
    evaluator: Mapped[str] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checks: Mapped[list[dict]] = mapped_column(JSON, default=list)

    attempt: Mapped[Attempt] = relationship(back_populates="gate_records")


# --------------------------------------------------------------------------- #
# the lineage graph — append-only
# --------------------------------------------------------------------------- #


class Artifact(Base):
    """A versioned artifact. Never updated — a re-run produces a new version.

    This is what makes staleness computable: `design.openapi` v1 approved, then
    re-derived as v2, means the approval no longer covers what exists.
    """

    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "name", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    path: Mapped[str | None] = mapped_column(String(512), default=None)
    produced_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("attempts.id"), default=None, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="artifacts")
    produced_by: Mapped[Attempt | None] = relationship(back_populates="produced")

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"


class ArtifactInput(Base):
    """A lineage edge: this attempt consumed this artifact version.

    Together with `Artifact.produced_by`, these edges answer the question the
    whole lineage graph exists for — *why does this exist, and from what?*
    """

    __tablename__ = "artifact_inputs"
    __table_args__ = (UniqueConstraint("attempt_id", "artifact_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)

    attempt: Mapped[Attempt] = relationship(back_populates="consumed")
    artifact: Mapped[Artifact] = relationship()


class Approval(Base):
    """A human decision, bound to the artifact versions it was granted against (D10)."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(128))
    decision: Mapped[Decision] = mapped_column(_enum(Decision), default=Decision.PENDING)
    decided_by: Mapped[str | None] = mapped_column(String(128), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="approvals")
    bindings: Mapped[list[ApprovalBinding]] = relationship(
        back_populates="approval", cascade="all, delete"
    )

    @property
    def is_decided(self) -> bool:
        return self.decision is not Decision.PENDING


class ApprovalBinding(Base):
    """Which artifact version an approval covered.

    Recorded at decision time. If a newer version of that artifact exists, the
    approval is stale — it was granted against a document that no longer exists.
    """

    __tablename__ = "approval_bindings"
    __table_args__ = (UniqueConstraint("approval_id", "artifact_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)

    approval: Mapped[Approval] = relationship(back_populates="bindings")
    artifact: Mapped[Artifact] = relationship()
