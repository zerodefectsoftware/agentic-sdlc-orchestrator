"""What to do about a gate verdict.

The gate reaches a **verdict**; this decides the **consequence**. Keeping them
apart is what lets policy override a node's own defaults, and it means the
evaluator never has to know about retry budgets.

The rule this module exists to enforce: **the repair loop responds to FAIL, not
to ERROR.** A failing gate means the work is wrong, and a fix node might fix it.
An erroring gate means the check could not be performed — a missing fact, an
unimplemented predicate, a broken evaluator. None of that is repaired by asking
an agent to change the code, and retrying it burns budget to arrive at the same
place with less information.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from orchestrator.engine.plan import Autonomy, ErrorPolicy, Node, NodeKind
from orchestrator.gates.evaluator import Verdict

_DEFAULT_ERROR_POLICY = ErrorPolicy()

ESCALATION_PREFIX = "escalate"


class Action(StrEnum):
    PROCEED = "proceed"          # gate held; move on
    RETRY = "retry"              # run the same node again
    INSERT_FIX = "insert_fix"    # materialise a repair node, then re-enter
    ESCALATE = "escalate"        # hand to a human
    SAFE_STOP = "safe_stop"      # halt with state intact
    ROLLBACK = "rollback"        # restore the baseline


@dataclass(frozen=True, slots=True)
class Response:
    """The decided consequence, with the reason it was decided.

    The reason is recorded: an escalation a human cannot explain is an
    interruption rather than a checkpoint.
    """

    action: Action
    reason: str

    def __str__(self) -> str:
        return f"{self.action}: {self.reason}"


def respond_to(verdict: Verdict, node: Node, *, attempt: int) -> Response:
    """Decide what happens after `attempt` of `node` reached `verdict`.

    `attempt` is 1-based — the number of the attempt that just finished.
    """
    if verdict is Verdict.PASS:
        return Response(Action.PROCEED, "gate held")

    if verdict is Verdict.ERROR:
        return _respond_to_error(node, attempt)

    return _respond_to_failure(node, attempt)


def escalation_node(node: Node, response: Response, *, attempt: int) -> Node:
    """Build the `human` node an escalation inserts.

    §3 says nothing executes outside the graph, and a run that silently parks in
    a BLOCKED status with a note attached would break that. Making escalation a
    node means every human interaction — planned checkpoint, clarification, or
    unplanned handoff — is uniform: it appears in the graph, in the evidence
    bundle, with a decider, a timestamp, and lineage.

    It inherits the escalating node's stage, because the work still belongs to
    that phase. Metrics that group by stage should attribute the handoff to
    verification, not to a category of its own.

    The decision means: **approve** — the human accepts the state and the run
    proceeds past the failed node; **reject** — the run stops. For a node whose
    `may_waive` is false (D15), this is the only route by which the finding can
    be waived at all, and it is a human doing it.
    """
    return Node(
        id=f"{ESCALATION_PREFIX}:{node.id}#{attempt}",
        kind=NodeKind.HUMAN,
        stage=node.stage,
        autonomy=Autonomy.APPROVE,
        needs=[node.id],
        optional=True,  # materialised only when something escalates to it
        presents=[f"{node.id}.gate_record", *node.outputs],
    )


def _respond_to_error(node: Node, attempt: int) -> Response:
    policy = node.on_error or _DEFAULT_ERROR_POLICY

    if attempt <= policy.retries:
        return Response(
            Action.RETRY,
            f"gate could not be evaluated; retrying transient failure "
            f"({attempt}/{policy.retries})",
        )

    return Response(
        Action(policy.then),
        "gate could not be evaluated — the harness needs attention, not the work "
        "(no fix node: an unperformable check is not a code problem)",
    )


def _respond_to_failure(node: Node, attempt: int) -> Response:
    repair = node.on_fail

    if repair is not None:
        if attempt < repair.max_attempts:
            scope = f" scoped to {repair.scoped_to}" if repair.scoped_to else ""
            return Response(
                Action.INSERT_FIX,
                f"gate failed; inserting '{repair.insert}'{scope} "
                f"(attempt {attempt}/{repair.max_attempts})",
            )
        return Response(
            Action(repair.then),
            f"gate failed and the repair budget is exhausted "
            f"({attempt}/{repair.max_attempts} attempts)",
        )

    budget = node.retry_budget or 0
    if attempt <= budget:
        return Response(Action.RETRY, f"gate failed; retrying ({attempt}/{budget})")

    return Response(
        Action.ESCALATE, f"gate failed and the retry budget is exhausted ({budget})"
    )
