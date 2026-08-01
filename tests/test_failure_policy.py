"""Failure policy tests.

The point of this module is one rule: the repair loop responds to FAIL, not to
ERROR. Everything else here is bookkeeping around bounds.
"""

from __future__ import annotations

import pytest

from orchestrator.engine.plan import Autonomy, Node, NodeKind, Stage
from orchestrator.gates.evaluator import Verdict
from orchestrator.policy.failure import (
    ESCALATION_PREFIX,
    Action,
    escalation_node,
    respond_to,
)


def node(**overrides) -> Node:
    payload = {
        "id": "tests",
        "kind": "tool",
        "stage": "verification",
        "run": "sh:pytest",
        "retry_budget": 2,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


REPAIR = {"insert": "fix", "scoped_to": "failing_module", "max_attempts": 2}


def test_a_holding_gate_proceeds():
    assert respond_to(Verdict.PASS, node(), attempt=1).action is Action.PROCEED


# --------------------------------------------------------------------------- #
# FAIL — the work might be wrong, so repair is reasonable
# --------------------------------------------------------------------------- #


def test_failure_inserts_a_fix_node_within_budget():
    response = respond_to(Verdict.FAIL, node(on_fail=REPAIR), attempt=1)
    assert response.action is Action.INSERT_FIX
    assert "failing_module" in response.reason


def test_failure_escalates_once_the_repair_budget_is_exhausted():
    """An unbounded repair loop has no MTTR and no safe-stop."""
    response = respond_to(Verdict.FAIL, node(on_fail=REPAIR), attempt=2)
    assert response.action is Action.ESCALATE
    assert "exhausted" in response.reason


def test_repair_policy_can_terminate_with_rollback():
    policy = {**REPAIR, "then": "rollback"}
    assert respond_to(Verdict.FAIL, node(on_fail=policy), attempt=2).action is Action.ROLLBACK


def test_failure_without_a_repair_policy_retries_within_the_node_budget():
    assert respond_to(Verdict.FAIL, node(retry_budget=2), attempt=1).action is Action.RETRY
    assert respond_to(Verdict.FAIL, node(retry_budget=2), attempt=2).action is Action.RETRY
    assert respond_to(Verdict.FAIL, node(retry_budget=2), attempt=3).action is Action.ESCALATE


def test_a_zero_retry_budget_escalates_immediately():
    assert respond_to(Verdict.FAIL, node(retry_budget=0), attempt=1).action is Action.ESCALATE


# --------------------------------------------------------------------------- #
# ERROR — the check could not be performed, so repair is pointless
# --------------------------------------------------------------------------- #


def test_error_never_inserts_a_fix_node():
    """An unimplemented predicate is not repaired by changing the code."""
    response = respond_to(Verdict.ERROR, node(on_fail=REPAIR), attempt=1)
    assert response.action is Action.ESCALATE
    assert "not a code problem" in response.reason


def test_error_escalates_immediately_by_default():
    """A missing fact is exactly as missing on attempt two."""
    assert respond_to(Verdict.ERROR, node(), attempt=1).action is Action.ESCALATE


def test_error_does_not_consume_the_retry_budget():
    """The failing case this whole split exists to prevent: burning two model
    calls on a harness problem, then escalating with less information."""
    generous = node(retry_budget=5, on_fail={**REPAIR, "max_attempts": 5})
    assert respond_to(Verdict.ERROR, generous, attempt=1).action is Action.ESCALATE


def test_error_can_retry_when_the_plan_declares_a_transient_failure():
    """A test command that crashed before recording an exit code may be flaky."""
    flaky = node(on_error={"retries": 2})
    assert respond_to(Verdict.ERROR, flaky, attempt=1).action is Action.RETRY
    assert respond_to(Verdict.ERROR, flaky, attempt=2).action is Action.RETRY
    assert respond_to(Verdict.ERROR, flaky, attempt=3).action is Action.ESCALATE


def test_error_policy_can_safe_stop():
    stopper = node(on_error={"then": "safe_stop"})
    assert respond_to(Verdict.ERROR, stopper, attempt=1).action is Action.SAFE_STOP


def test_error_policy_cannot_declare_a_fix_node():
    """The plan schema forbids it, so it cannot be misconfigured."""
    with pytest.raises(ValueError, match="insert"):
        node(on_error={"insert": "fix", "retries": 1})


# --------------------------------------------------------------------------- #
# escalation is a node, not a status
# --------------------------------------------------------------------------- #


def test_escalation_produces_a_human_node():
    """§3: nothing executes outside the graph. A run parked in a status with a
    note attached would be an exception to that."""
    failing = node(retry_budget=0, outputs=["coverage_report"])
    response = respond_to(Verdict.FAIL, failing, attempt=1)
    escalation = escalation_node(failing, response, attempt=1)

    assert escalation.kind is NodeKind.HUMAN
    assert escalation.autonomy is Autonomy.APPROVE
    assert escalation.needs == ["tests"]
    assert escalation.optional


def test_escalation_inherits_the_stage_of_the_work_that_escalated():
    """Metrics grouped by stage should attribute the handoff to verification,
    not to a category of its own."""
    failing = node(stage="verification")
    escalation = escalation_node(failing, respond_to(Verdict.FAIL, failing, attempt=1), attempt=1)
    assert escalation.stage is Stage.VERIFICATION


def test_escalation_presents_the_gate_record_and_the_outputs():
    """A human asked to decide needs to see why, not just that."""
    failing = node(outputs=["coverage_report"])
    escalation = escalation_node(failing, respond_to(Verdict.FAIL, failing, attempt=1), attempt=1)
    assert escalation.presents == ["tests.gate_record", "coverage_report"]


def test_escalation_ids_do_not_collide_across_attempts():
    """Re-planning can re-run a node and escalate again; (run_id, node_id) is unique."""
    failing = node()
    first = escalation_node(failing, respond_to(Verdict.FAIL, failing, attempt=1), attempt=1)
    second = escalation_node(failing, respond_to(Verdict.FAIL, failing, attempt=3), attempt=3)
    assert first.id != second.id
    assert first.id.startswith(ESCALATION_PREFIX)


def test_an_error_escalation_is_the_same_shape_as_a_failure_escalation():
    """Both hand off to a human; only the reason differs."""
    failing = node()
    from_error = escalation_node(failing, respond_to(Verdict.ERROR, failing, attempt=1), attempt=1)
    assert from_error.kind is NodeKind.HUMAN


# --------------------------------------------------------------------------- #
# every response explains itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("verdict", [Verdict.PASS, Verdict.FAIL, Verdict.ERROR])
def test_every_response_carries_a_reason(verdict):
    """An escalation a human cannot explain is an interruption, not a checkpoint."""
    response = respond_to(verdict, node(on_fail=REPAIR), attempt=1)
    assert response.reason
    assert str(response).startswith(response.action)
