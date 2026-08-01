"""Autonomy classes, escalation, and what to do about a gate verdict.

The gate reaches a verdict; policy decides the consequence. That separation is
what lets a HIGH security finding force an approval on a node whose default is
REVIEW — and it keeps the evaluator ignorant of retry budgets.

Escalation inserts a `human` node rather than parking the run in a status, so
every human interaction is uniform in the graph and in the evidence bundle.
"""

from orchestrator.policy.baseline import capture_baseline, verify_no_regression
from orchestrator.policy.clarify import normalize_clarification
from orchestrator.policy.failure import (
    ESCALATION_PREFIX,
    Action,
    Response,
    escalation_node,
    respond_to,
)
from orchestrator.policy.triage import triage_ambiguities

__all__ = [
    "ESCALATION_PREFIX",
    "capture_baseline",
    "normalize_clarification",
    "verify_no_regression",
    "Action",
    "Response",
    "escalation_node",
    "respond_to",
    "triage_ambiguities",
]
