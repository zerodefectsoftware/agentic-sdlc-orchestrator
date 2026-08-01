"""The run store: sessions, schema creation, and the run lifecycle.

SQLite by default (D2). Run state has to survive a safe-stop and be queryable
afterwards for the reliability metrics — in-memory state cannot demonstrate
resumability, and a run you cannot resume is not safely stoppable.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.state.models import (
    Attempt,
    Base,
    GateRecord,
    NodeExecution,
    NodeStatus,
    Run,
    RunStatus,
    utcnow,
)


class Store:
    """Owns the database connection and hands out sessions."""

    def __init__(self, url: str = "sqlite:///runs/orchestrator.db") -> None:
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, future=True)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(self.engine)

    @classmethod
    def in_memory(cls) -> Store:
        """For tests. A shared connection so every session sees the same schema."""
        return cls("sqlite:///:memory:")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def start_run(
    session: Session,
    *,
    plan_name: str,
    plan_version: int,
    requirement_path: str,
    target_profile: str,
    node_ids: list[tuple[str, str]],
) -> Run:
    """Create a run and materialise its node executions in PENDING.

    `node_ids` is a list of (node_id, kind). The run graph starts as a copy of the
    plan's shape; nodes inserted later (fan-out children, repair nodes) are added
    as they are materialised and flagged `inserted`.
    """
    run = Run(
        plan_name=plan_name,
        plan_version=plan_version,
        requirement_path=requirement_path,
        target_profile=target_profile,
    )
    session.add(run)
    session.flush()

    session.add_all(
        NodeExecution(run_id=run.id, node_id=node_id, kind=kind)
        for node_id, kind in node_ids
    )
    session.flush()
    return run


def insert_node(session: Session, run: Run, node_id: str, kind: str) -> NodeExecution:
    """Materialise a node that was not in the authored plan (§6).

    Fan-out children and repair nodes arrive this way. They are ordinary nodes —
    flagged only so the evidence bundle can show which parts of the run were
    planned and which were a response to what happened.
    """
    node = NodeExecution(run_id=run.id, node_id=node_id, kind=kind, inserted=True)
    session.add(node)
    session.flush()
    return node


def get_node(session: Session, run: Run, node_id: str) -> NodeExecution | None:
    return session.scalar(
        select(NodeExecution).where(
            NodeExecution.run_id == run.id, NodeExecution.node_id == node_id
        )
    )


def begin_attempt(
    session: Session,
    node: NodeExecution,
    *,
    worker: str,
    model: str | None = None,
    effort: str | None = None,
    prompt_ref: str | None = None,
) -> Attempt:
    """Start a new attempt. Retries append rather than overwrite, so the sequence
    remains available for retry-frequency and MTTR (§8).

    The next number comes from a query rather than from `node.attempts`: the
    relationship can be stale within a session, and a silently reused attempt
    number would corrupt exactly the sequence the metrics depend on.
    """
    highest = session.scalar(
        select(func.max(Attempt.number)).where(Attempt.node_execution_id == node.id)
    )
    attempt = Attempt(
        node_execution_id=node.id,
        number=(highest or 0) + 1,
        worker=worker,
        model=model,
        effort=effort,
        prompt_ref=prompt_ref,
    )
    node.status = NodeStatus.RUNNING
    session.add(attempt)
    session.flush()
    return attempt


def finish_attempt(
    session: Session,
    attempt: Attempt,
    *,
    status: NodeStatus,
    error: str | None = None,
) -> Attempt:
    attempt.finished_at = utcnow()
    attempt.error = error
    attempt.node.status = status
    session.flush()
    return attempt


def record_gate(
    session: Session,
    attempt: Attempt,
    *,
    verdict: str,
    evaluator: str,
    checks: list[dict],
    gate: str = "exit",
) -> GateRecord:
    """Persist a gate verdict exactly as reached.

    The evidence bundle reads this back rather than re-evaluating, so it always
    shows the verdict that actually governed (§5.4).
    """
    record = GateRecord(
        attempt_id=attempt.id,
        gate=gate,
        verdict=verdict,
        evaluator=evaluator,
        checks=checks,
    )
    session.add(record)
    session.flush()
    return record


def finish_run(
    session: Session, run: Run, *, status: RunStatus, stop_reason: str | None = None
) -> Run:
    run.status = status
    run.stop_reason = stop_reason
    run.finished_at = utcnow()
    session.flush()
    return run


def nodes_in_nonterminal_state(session: Session, run: Run) -> list[NodeExecution]:
    """Feeds G10's `no_node_in_nonterminal_state`.

    A run cannot be release-ready while work is still pending, running, or
    blocked — a bundle assembled mid-flight would document an incomplete run.
    """
    unfinished = (NodeStatus.PENDING, NodeStatus.RUNNING, NodeStatus.BLOCKED, NodeStatus.STALE)
    return list(
        session.scalars(
            select(NodeExecution).where(
                NodeExecution.run_id == run.id, NodeExecution.status.in_(unfinished)
            )
        )
    )
