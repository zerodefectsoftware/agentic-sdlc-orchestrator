"""Ambiguity triage — the calibration point of controlled autonomy (D13).

Deterministic on purpose. Whether a question needs a person is a *policy*
decision, and routing it through a model would make the escalation threshold
itself probabilistic — the one knob that must be predictable if "controlled
autonomy" is to mean anything.

The rule: high severity escalates, everything else carries a recorded assumption
forward. A system that asks forty questions is as useless as one that asks none.
"""

from __future__ import annotations

from orchestrator.artifacts import Disposition, RequirementRegister, Severity
from orchestrator.workers.pytask import Task, TaskOutput

ESCALATION_THRESHOLD = Severity.HIGH

def triage_ambiguities(task: Task) -> TaskOutput:
    """Dispose of every ambiguity: escalate it, or record an assumption.

    The threshold is a plan parameter rather than a constant, because it is the
    calibration knob of controlled autonomy and different scenarios want it in
    different places. A vague requirement is worth more interruptions than a
    clear one — `ambiguous.yaml` lowers it to MEDIUM, and says so in data where
    a reviewer can see it.
    """
    register = RequirementRegister.model_validate_json(task.require("intake.register"))
    threshold = Severity(str(task.params.get("threshold", ESCALATION_THRESHOLD)).lower())

    escalate = []
    assumed = []

    for ambiguity in register.ambiguities:
        if ambiguity.is_disposed:
            continue
        if ambiguity.severity.rank >= threshold.rank:
            escalate.append(ambiguity.id)
            continue
        # Below the threshold: record the assumption and carry it forward, so a
        # reviewer can see what was decided on their behalf and why.
        ambiguity.disposition = Disposition.ASSUMPTION
        ambiguity.answer = ambiguity.answer or (
            f"assumed by policy: severity {ambiguity.severity} is below the "
            f"escalation threshold ({threshold})"
        )
        assumed.append(ambiguity.id)

    return TaskOutput(
        facts={
            "ambiguities.total": len(register.ambiguities),
            "ambiguities.escalated": len(escalate),
            "ambiguities.assumed": len(assumed),
            "ambiguities.threshold": str(threshold),
        },
        artifacts={"intake.register": register.model_dump_json(indent=2)},
    )
