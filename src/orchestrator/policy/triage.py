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
    register = RequirementRegister.model_validate_json(task.require("intake.register"))

    escalate = []
    assumed = []

    for ambiguity in register.ambiguities:
        if ambiguity.is_disposed:
            continue
        if ambiguity.severity is ESCALATION_THRESHOLD:
            escalate.append(ambiguity.id)
            continue
        # Below the threshold: record the assumption and carry it forward, so a
        # reviewer can see what was decided on their behalf and why.
        ambiguity.disposition = Disposition.ASSUMPTION
        ambiguity.answer = ambiguity.answer or (
            f"assumed by policy: severity {ambiguity.severity} is below the "
            f"escalation threshold"
        )
        assumed.append(ambiguity.id)

    return TaskOutput(
        facts={
            "ambiguities.total": len(register.ambiguities),
            "ambiguities.escalated": len(escalate),
            "ambiguities.assumed": len(assumed),
        },
        artifacts={"intake.register": register.model_dump_json(indent=2)},
    )
