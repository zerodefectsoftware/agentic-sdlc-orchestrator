"""Causal record: artifact <- decision <- inputs <- (agent, prompt, model).

Append-only. An artifact is never updated; a re-run produces a new version.
That rule is what makes D10 mechanical — an approval records the exact versions
it covered, so "approval of a superseded artifact is not approval" is a query
rather than a discipline.

Answers two questions: *why does this artifact exist* (`query.why`) and *which
approvals no longer cover what exists* (`query.stale_approvals`).
"""

from orchestrator.lineage.query import (
    Provenance,
    StaleApproval,
    artifact_history,
    attempts_for,
    provenance,
    stale_approvals,
    unproduced_artifacts,
    why,
)
from orchestrator.lineage.recorder import (
    decide,
    latest,
    record_artifact,
    record_inputs,
    request_approval,
    revert_to_pending,
)

__all__ = [
    "Provenance",
    "StaleApproval",
    "artifact_history",
    "attempts_for",
    "decide",
    "latest",
    "provenance",
    "record_artifact",
    "record_inputs",
    "request_approval",
    "revert_to_pending",
    "stale_approvals",
    "unproduced_artifacts",
    "why",
]
