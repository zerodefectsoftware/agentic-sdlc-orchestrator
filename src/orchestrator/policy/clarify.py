"""Folding a human answer back into the requirement register.

The brief asks the ambiguous scenario to show ambiguity *identified and
normalized*, not silently resolved. Identification is `intake`; escalation is
`ambiguity-triage`; this is normalization — the step that turns a sentence typed
at a terminal into a structured, attributable disposition on a specific question.

Without it, clarification is theatre: the run stops, a person answers, and the
answer lives in the audit trail while every downstream node works from the same
unresolved register it had before.

Answers are matched by ambiguity id, one per line:

    A1: 302, so browsers do not cache the redirect
    A2: per-IP, 100/minute

An unmatched line is not discarded — it is recorded as context against every
question that was still open, because losing what a person said is worse than
attributing it a little too widely. Ambiguities left untouched by the answer stay
undisposed, which is exactly what the gate is looking for.
"""

from __future__ import annotations

import re

from orchestrator.artifacts import Disposition, RequirementRegister
from orchestrator.workers.pytask import Task, TaskOutput

ANSWER_LINE = re.compile(r"^\s*(?P<id>[A-Za-z][\w.-]*)\s*[:=]\s*(?P<answer>.+?)\s*$")
NOTE = re.compile(r"^note:\s*$", re.MULTILINE)


def normalize_clarification(task: Task) -> TaskOutput:
    register = RequirementRegister.model_validate_json(task.require("intake.register"))
    decision = task.require(f"{task.param('checkpoint')}.decision")

    answers, loose = parse_answers(decision)
    decided_by = _decided_by(decision)

    resolved: list[str] = []
    for ambiguity in register.ambiguities:
        if ambiguity.is_disposed:
            continue
        answer = answers.get(ambiguity.id) or loose
        if not answer:
            continue
        ambiguity.disposition = Disposition.RESOLVED
        ambiguity.answer = f"{answer} — answered by {decided_by}"
        resolved.append(ambiguity.id)

    outstanding = [a.id for a in register.ambiguities if not a.is_disposed]
    return TaskOutput(
        facts={
            "clarification.resolved": len(resolved),
            "clarification.outstanding": len(outstanding),
            "clarification.matched_by_id": sum(1 for key in answers if key in
                                               {a.id for a in register.ambiguities}),
        },
        artifacts={"intake.register": register.model_dump_json(indent=2)},
    )


def parse_answers(decision: str) -> tuple[dict[str, str], str]:
    """Split a checkpoint note into per-id answers and whatever else was said."""
    body = decision.split("note:", 1)[1] if "note:" in decision else ""

    answers: dict[str, str] = {}
    remainder: list[str] = []
    for line in body.splitlines():
        match = ANSWER_LINE.match(line)
        if match:
            answers[match.group("id")] = match.group("answer")
        elif line.strip():
            remainder.append(line.strip())

    return answers, " ".join(remainder)


def _decided_by(decision: str) -> str:
    for line in decision.splitlines():
        if line.startswith("by: "):
            return line[4:].strip()
    return "unknown"
