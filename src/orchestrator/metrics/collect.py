"""Reliability metrics (§8).

Every definition here is written down because a metric whose definition is vague
is worse than no metric — it invites a number to be quoted without anyone
knowing what it measures.

Three choices worth arguing with:

**Unrecovered incidents are excluded from MTTR and reported separately.** A node
that failed and never came back has no recovery time, and quietly dropping it
would let MTTR improve as reliability got worse — the worst incidents would
simply stop counting.

**Human wait time is separated from end-to-end latency.** A run blocked for three
hours on an approval is not a slow system. Reporting them together would make
the most governed runs look like the least performant ones.

**Sample sizes travel with the rates.** Across three scenario runs these are
instrumentation, not statistics, and a rate quoted without its denominator is
how "100% success" comes to mean "one node passed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.state import store
from orchestrator.state.models import (
    Attempt,
    Decision,
    NodeExecution,
    NodeStatus,
    Run,
    RunStatus,
    elapsed_ms,
)

BLOCKING_VERDICTS = ("fail", "error")


@dataclass(frozen=True, slots=True)
class Incident:
    """A gate that went red, and what happened next.

    Recovery is measured from the end of the failing attempt to the end of the
    attempt that passed — the span during which the run was in a known-bad state.
    """

    node_id: str
    stage: str
    failed_at_attempt: int
    recovered_at_attempt: int | None
    recovery_ms: int | None

    @property
    def recovered(self) -> bool:
        return self.recovered_at_attempt is not None


@dataclass(frozen=True, slots=True)
class StageMetrics:
    stage: str
    nodes: int
    attempts: int
    retries: int
    first_attempt_passes: int
    incidents: int

    @property
    def success_rate(self) -> float | None:
        """Nodes passing their gate on the first attempt ÷ nodes that ran."""
        return self.first_attempt_passes / self.nodes if self.nodes else None


@dataclass(frozen=True, slots=True)
class RunMetrics:
    run_id: str
    status: str

    nodes_executed: int
    attempts: int
    retries: int
    first_attempt_passes: int

    incidents: list[Incident] = field(default_factory=list)
    elapsed_ms: int | None = None
    human_wait_ms: int = 0
    pending_approvals: int = 0
    by_stage: dict[str, StageMetrics] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # rates — each returns None rather than a misleading zero
    # ------------------------------------------------------------------ #

    @property
    def success_rate(self) -> float | None:
        """First-attempt passes ÷ nodes that ran.

        None when nothing ran: a rate over an empty set is not zero, it is
        undefined, and reporting 0.0 would read as total failure.
        """
        if not self.nodes_executed:
            return None
        return self.first_attempt_passes / self.nodes_executed

    @property
    def retry_rate(self) -> float | None:
        if not self.nodes_executed:
            return None
        return self.retries / self.nodes_executed

    @property
    def recovered_incidents(self) -> list[Incident]:
        return [incident for incident in self.incidents if incident.recovered]

    @property
    def unrecovered_incidents(self) -> list[Incident]:
        return [incident for incident in self.incidents if not incident.recovered]

    @property
    def mttr_ms(self) -> float | None:
        """Mean recovery time over *recovered* incidents only.

        None when nothing recovered. Read it beside `unrecovered_incidents` —
        alone, it describes only the failures that ended well.
        """
        recovered = [i.recovery_ms for i in self.recovered_incidents if i.recovery_ms is not None]
        return mean(recovered) if recovered else None

    @property
    def system_ms(self) -> int | None:
        """Elapsed time minus what was spent waiting on people."""
        if self.elapsed_ms is None:
            return None
        return max(0, self.elapsed_ms - self.human_wait_ms)

    def summary(self) -> dict[str, object]:
        """A flat view for the evidence bundle, with denominators attached."""
        return {
            "nodes_executed": self.nodes_executed,
            "attempts": self.attempts,
            "retries": self.retries,
            "success_rate": self.success_rate,
            "success_rate_basis": f"{self.first_attempt_passes}/{self.nodes_executed}",
            "retry_rate": self.retry_rate,
            "incidents": len(self.incidents),
            "incidents_recovered": len(self.recovered_incidents),
            "incidents_unrecovered": len(self.unrecovered_incidents),
            "mttr_ms": self.mttr_ms,
            "elapsed_ms": self.elapsed_ms,
            "human_wait_ms": self.human_wait_ms,
            "system_ms": self.system_ms,
            "pending_approvals": self.pending_approvals,
        }


@dataclass(frozen=True, slots=True)
class FleetMetrics:
    """Across runs. Rollback frequency only means anything at this level."""

    runs: int
    completed: int
    rolled_back: int
    stopped: int
    blocked: int
    failed: int

    @property
    def rollback_rate(self) -> float | None:
        return self.rolled_back / self.runs if self.runs else None

    @property
    def completion_rate(self) -> float | None:
        return self.completed / self.runs if self.runs else None

    @property
    def is_statistically_meaningful(self) -> bool:
        """Deliberately conservative, and deliberately present.

        Three scenario runs produce instrumentation, not statistics. Making the
        sample size a property means a caller has to actively ignore it.
        """
        return self.runs >= 30


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #


def run_metrics(session: Session, run: Run) -> RunMetrics:
    executions = [node for node in store.all_nodes(session, run) if node.attempts]

    incidents: list[Incident] = []
    attempts = 0
    first_pass = 0

    for execution in executions:
        ordered = sorted(execution.attempts, key=lambda a: a.number)
        attempts += len(ordered)
        if _outcome(ordered[0]) == "pass":
            first_pass += 1
        incident = _incident_for(execution, ordered)
        if incident is not None:
            incidents.append(incident)

    return RunMetrics(
        run_id=run.id,
        status=str(run.status),
        nodes_executed=len(executions),
        attempts=attempts,
        retries=attempts - len(executions),
        first_attempt_passes=first_pass,
        incidents=incidents,
        elapsed_ms=_elapsed_ms(run),
        human_wait_ms=_human_wait_ms(run),
        pending_approvals=sum(1 for a in run.approvals if not a.is_decided),
        by_stage=_by_stage(executions, incidents),
    )


def fleet_metrics(session: Session) -> FleetMetrics:
    runs = list(session.scalars(select(Run)))
    counted = {status: sum(1 for r in runs if r.status is status) for status in RunStatus}
    return FleetMetrics(
        runs=len(runs),
        completed=counted[RunStatus.COMPLETED],
        rolled_back=counted[RunStatus.ROLLED_BACK],
        stopped=counted[RunStatus.STOPPED],
        blocked=counted[RunStatus.BLOCKED],
        failed=counted[RunStatus.FAILED],
    )


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _outcome(attempt: Attempt) -> str:
    """An attempt's verdict, taken from the gate that judged it.

    A node without a gate has no verdict to read, so completion without an error
    counts as a pass — the same rule the scheduler applies.
    """
    if attempt.gate_records:
        return attempt.gate_records[0].verdict
    return "error" if attempt.error else "pass"


def _incident_for(execution: NodeExecution, ordered: list[Attempt]) -> Incident | None:
    """The first red gate on this node, and the attempt that cleared it."""
    failure = next((a for a in ordered if _outcome(a) in BLOCKING_VERDICTS), None)
    if failure is None:
        return None

    recovery = next(
        (a for a in ordered if a.number > failure.number and _outcome(a) == "pass"), None
    )
    recovery_ms = (
        elapsed_ms(failure.finished_at, recovery.finished_at) if recovery is not None else None
    )

    return Incident(
        node_id=execution.node_id,
        stage=execution.stage,
        failed_at_attempt=failure.number,
        recovered_at_attempt=recovery.number if recovery else None,
        recovery_ms=recovery_ms,
    )


def _elapsed_ms(run: Run) -> int | None:
    return elapsed_ms(run.started_at, run.finished_at)


def _human_wait_ms(run: Run) -> int:
    """Time spent waiting on people, from approval requested to decided.

    Pending approvals contribute nothing: an undecided approval has no duration
    yet, and guessing one would make a blocked run look slow rather than blocked.
    """
    total = 0
    for approval in run.approvals:
        if approval.decision is not Decision.PENDING and approval.decided_at:
            total += elapsed_ms(approval.requested_at, approval.decided_at) or 0
    return total


def _by_stage(
    executions: list[NodeExecution], incidents: list[Incident]
) -> dict[str, StageMetrics]:
    """Grouped by lifecycle phase — where retries concentrate is the useful number."""
    stages: dict[str, list[NodeExecution]] = {}
    for execution in executions:
        stages.setdefault(execution.stage, []).append(execution)

    result: dict[str, StageMetrics] = {}
    for stage, nodes in stages.items():
        attempts = sum(len(node.attempts) for node in nodes)
        first_pass = sum(
            1
            for node in nodes
            if _outcome(sorted(node.attempts, key=lambda a: a.number)[0]) == "pass"
        )
        result[stage] = StageMetrics(
            stage=stage,
            nodes=len(nodes),
            attempts=attempts,
            retries=attempts - len(nodes),
            first_attempt_passes=first_pass,
            incidents=sum(1 for incident in incidents if incident.stage == stage),
        )
    return result


def unfinished_nodes(session: Session, run: Run) -> list[str]:
    """Convenience for reporting why a run is not complete."""
    return [node.node_id for node in store.nodes_in_nonterminal_state(session, run)]


__all__ = [
    "FleetMetrics",
    "Incident",
    "NodeStatus",
    "RunMetrics",
    "StageMetrics",
    "fleet_metrics",
    "run_metrics",
    "unfinished_nodes",
]
