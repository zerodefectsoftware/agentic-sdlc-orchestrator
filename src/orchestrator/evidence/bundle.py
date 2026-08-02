"""The shape of an evidence bundle.

What §5.4 says a reviewer needs in order to approve a change: the gate verdicts
and who reached them, the approvals and what they covered, the artifacts and
what produced them, and the shape of the run itself.

Everything here is a **record of something that happened**, not a judgment made
at assembly time. Aggregating recorded data — counting attempts, grouping by
stage — is collection. Re-running a gate would not be, which is why the checks
below carry the detail string the evaluator produced rather than a fresh result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CheckRecord:
    """One gate check, exactly as the evaluator reported it."""

    check: str
    verdict: str
    detail: str
    observed: str | None = None

    @property
    def held(self) -> bool:
        return self.verdict == "pass"


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """A recorded verdict — which evaluator, what it decided, and when (§5.4)."""

    node_id: str
    stage: str
    gate: str
    verdict: str
    evaluator: str
    evaluated_at: datetime
    attempt: int
    checks: list[CheckRecord] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict != "pass"

    @property
    def failures(self) -> list[CheckRecord]:
        return [check for check in self.checks if not check.held]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    number: int
    worker: str
    model: str | None
    effort: str | None
    started_at: datetime
    finished_at: datetime | None
    error: str | None

    @property
    def duration_ms(self) -> int | None:
        from orchestrator.state.models import elapsed_ms

        return elapsed_ms(self.started_at, self.finished_at)


@dataclass(frozen=True, slots=True)
class NodeRecord:
    node_id: str
    kind: str
    stage: str
    status: str
    inserted: bool
    attempts: list[AttemptRecord] = field(default_factory=list)
    gates: list[GateOutcome] = field(default_factory=list)

    @property
    def retried(self) -> bool:
        return len(self.attempts) > 1


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """A human decision and the artifact versions it covered (D10)."""

    node_id: str
    decision: str
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    covers: list[str] = field(default_factory=list)  # e.g. design.openapi@v1
    stale: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    name: str
    version: int
    content_hash: str
    path: str | None
    produced_by_node: str | None
    created_at: datetime

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"

    @property
    def orphaned(self) -> bool:
        """No producing attempt — what `lineage_complete` exists to catch."""
        return self.produced_by_node is None


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    run_id: str
    plan: str
    plan_version: int
    requirement_path: str
    target_profile: str
    status: str
    stop_reason: str | None
    started_at: datetime
    finished_at: datetime | None
    nodes: list[NodeRecord] = field(default_factory=list)
    approvals: list[ApprovalRecord] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    metrics: dict[str, object] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # views a reviewer asks for
    # ------------------------------------------------------------------ #

    @property
    def blocking_gates(self) -> list[GateOutcome]:
        """Every verdict that is *still* stopping something.

        A node's earlier verdicts are history, not blockers: a run that failed
        twice and passed on the third attempt is not blocked, and reading every
        recorded verdict said otherwise — a completed run whose every gate ended
        green rendered as NOT RELEASABLE, which is the opposite of what happened.

        The superseded attempts stay in the bundle under their node, where a
        reviewer wants them. They just do not count as blocking.
        """
        return [
            gate
            for node in self.nodes
            if node.gates and node.gates[-1].blocked
            for gate in [node.gates[-1]]
        ]

    @property
    def superseded_gates(self) -> list[GateOutcome]:
        """Verdicts a later attempt replaced — the run's argument with itself."""
        return [gate for node in self.nodes for gate in node.gates[:-1] if gate.blocked]

    @property
    def stages(self) -> dict[str, list[NodeRecord]]:
        grouped: dict[str, list[NodeRecord]] = {}
        for node in self.nodes:
            grouped.setdefault(node.stage, []).append(node)
        return grouped

    @property
    def stale_approvals(self) -> list[ApprovalRecord]:
        return [approval for approval in self.approvals if approval.stale]

    @property
    def inserted_nodes(self) -> list[NodeRecord]:
        """Work the run added in response to what happened, not from the plan."""
        return [node for node in self.nodes if node.inserted]

    @property
    def counts(self) -> dict[str, int]:
        attempts = [attempt for node in self.nodes for attempt in node.attempts]
        first_try = sum(
            1
            for node in self.nodes
            if node.attempts and node.status == "passed" and not node.retried
        )
        return {
            "nodes": len(self.nodes),
            "inserted_nodes": len(self.inserted_nodes),
            "attempts": len(attempts),
            "retries": len(attempts) - len(self.nodes),
            "gates_evaluated": sum(len(node.gates) for node in self.nodes),
            "gates_blocking": len(self.blocking_gates),
            "artifacts": len(self.artifacts),
            "approvals": len(self.approvals),
            "stale_approvals": len(self.stale_approvals),
            "passed_first_attempt": first_try,
        }

    @property
    def is_releasable(self) -> bool:
        """A summary judgment, deliberately conservative.

        Not a substitute for G10 — that gate already ran and its verdict is
        recorded. This is a reading of what was recorded, so that a bundle
        cannot look healthier than the run it describes.
        """
        return (
            not self.blocking_gates
            and not self.stale_approvals
            and not any(artifact.orphaned for artifact in self.artifacts)
            and self.status in ("completed",)
        )
