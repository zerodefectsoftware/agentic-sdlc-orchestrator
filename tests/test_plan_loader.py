"""Plan loader tests.

Two halves. The first loads the real greenfield plan and asserts the design
decisions it encodes are actually present — a plan that silently loses D5's RED
gate or D6's frozen tests would still be valid YAML.

The second half is the more important one: malformed plans must fail at load
time with a message that names the problem. A scheduler should never have to
defend against a typo.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from orchestrator.engine.loader import (
    PlanError,
    dependency_graph,
    execution_order,
    load_plan,
)
from orchestrator.engine.plan import (
    Autonomy,
    Effort,
    ExpressionCheck,
    NodeKind,
    PredicateCheck,
    RunScheme,
    Stage,
)

REPO = Path(__file__).resolve().parents[1]
GREENFIELD = REPO / "plans" / "greenfield.yaml"


@pytest.fixture
def plan():
    return load_plan(GREENFIELD)


def write_plan(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(textwrap.dedent(body))
    return path


# --------------------------------------------------------------------------- #
# the real plan
# --------------------------------------------------------------------------- #


def test_greenfield_plan_loads(plan):
    assert plan.name == "greenfield"
    assert plan.version == 1
    assert plan.node_ids == [
        "intake",
        "ambiguity-triage",
        "clarify-with-human",
        "design",
        "design-approval",
        "scaffold",
        "tests-acceptance",
        "impl",
        "tests",
        "docs",
        "security",
        "release-readiness",
        "accept",
    ]


def test_execution_order_respects_dependencies(plan):
    order = execution_order(plan)
    assert order.index("intake") < order.index("design")
    assert order.index("design") < order.index("design-approval")
    assert order.index("design-approval") < order.index("scaffold")
    assert order.index("tests-acceptance") < order.index("impl")
    for upstream in ("tests", "docs", "security"):
        assert order.index(upstream) < order.index("release-readiness")
    assert order[-1] == "accept"


def test_verification_branches_run_in_parallel(plan):
    """tests, docs, and security depend only on impl — no edges between them."""
    graph = dependency_graph(plan)
    branches = ["tests", "docs", "security"]
    for node_id in branches:
        assert list(graph.predecessors(node_id)) == ["impl"]
    for a in branches:
        for b in branches:
            assert a == b or not graph.has_edge(a, b)


def test_red_gate_requires_the_acceptance_suite_to_fail(plan):
    """D5: a suite that passes against an empty scaffold asserts nothing."""
    gate = plan.node("tests-acceptance").gate
    assert ExpressionCheck(expression="pytest.exit_code != 0") in gate.all_checks


def test_repair_loop_freezes_the_target_tests(plan):
    """D6: the cheapest green suite is a weakened test."""
    assert plan.node("tests").freeze_paths == ["target/tests/**"]


def test_implementer_and_test_author_are_different_roles(plan):
    """D5: the implementer must satisfy tests it did not write."""
    assert plan.node("tests-acceptance").role == "test-author"
    assert plan.node("impl").template.role == "implementer"


def test_design_approval_is_bound_to_artifact_versions(plan):
    """D10: approval of a superseded artifact is not approval."""
    node = plan.node("design-approval")
    assert node.autonomy is Autonomy.APPROVE
    assert node.binds_to == ["design.artifacts.openapi", "design.artifacts.decisions"]


def test_security_findings_cannot_be_waived_by_an_agent(plan):
    """D15: segregation of duties."""
    assert plan.node("security").may_waive is False


def test_release_readiness_is_deterministic(plan):
    """D9: the final gate must never be a judgment call."""
    node = plan.node("release-readiness")
    assert node.kind is NodeKind.DERIVE
    assert not node.is_model_backed
    assert PredicateCheck(predicate="no_stale_approvals") in node.gate.all_checks


def test_gates_accept_both_expressions_and_predicates(plan):
    checks = plan.node("scaffold").gate.all_checks
    assert all(isinstance(check, ExpressionCheck) for check in checks)
    assert all(isinstance(c, PredicateCheck) for c in plan.node("intake").gate.all_checks)


def test_repair_policy_is_bounded(plan):
    """An unbounded retry is not a control."""
    policy = plan.node("tests").on_fail
    assert policy.max_attempts == 2
    assert policy.then == "escalate"


def test_escalation_uses_the_gate_vocabulary(plan):
    """One expression language, not two: escalation is a condition over facts."""
    assert plan.node("ambiguity-triage").escalate_when == PredicateCheck(
        predicate="has_high_severity_ambiguity"
    )
    assert plan.node("security").escalate_when == PredicateCheck(
        predicate="has_high_severity_finding"
    )


def test_escalation_also_accepts_a_bare_expression(tmp_path):
    """A plain comparison stays honest as an expression — no predicate ceremony."""
    path = write_plan(
        tmp_path,
        """
        plan: t
        version: 1
        nodes:
          - id: a
            kind: tool
            stage: verification
            run: sh:echo
            escalate_when: "contract_diff.breaking == true"
            on_escalate: b
          - id: b
            kind: human
            stage: release
        """,
    )
    node = load_plan(path).node("a")
    assert node.escalate_when == ExpressionCheck(expression="contract_diff.breaking == true")


def test_the_plan_covers_every_lifecycle_stage(plan):
    """§11 asserts lifecycle coverage; this makes the plan demonstrate it."""
    assert plan.missing_stages == []
    assert [s.value for s in plan.stages_covered] == [
        "requirements",
        "design",
        "implementation",
        "verification",
        "documentation",
        "release",
    ]


def test_stage_is_a_label_not_an_ordering_constraint(plan):
    """D5 puts VERIFICATION work before IMPLEMENTATION; order comes from `needs`."""
    order = execution_order(plan)
    assert plan.node("tests-acceptance").stage is Stage.VERIFICATION
    assert plan.node("impl").stage is Stage.IMPLEMENTATION
    assert order.index("tests-acceptance") < order.index("impl")


def test_security_is_verification_since_the_brief_has_no_slot_for_it(plan):
    assert [n.id for n in plan.nodes_in(Stage.VERIFICATION)] == [
        "tests-acceptance",
        "tests",
        "security",
    ]


def test_missing_stage_is_rejected(tmp_path):
    """An unlabelled node would silently create a hole in lifecycle coverage."""
    path = write_plan(
        tmp_path,
        """
        plan: t
        version: 1
        nodes:
          - id: a
            kind: tool
            run: sh:echo
        """,
    )
    with pytest.raises(PlanError, match="stage"):
        load_plan(path)


def test_required_predicates_spans_gates_escalations_and_templates(plan):
    required = plan.required_predicates
    assert "has_high_severity_ambiguity" in required   # from escalate_when
    assert "no_stale_approvals" in required            # from a gate
    assert "every_ac_has_a_test" in required           # from a gate
    assert required == sorted(set(required))


def test_run_scheme_separates_python_callables_from_shell_commands(plan):
    """Guessing between the two at dispatch time is how baffling failures happen."""
    security = plan.node("security")
    assert security.run_scheme is RunScheme.PY
    assert security.run_target == "orchestrator.gates.security_scan"

    tests = plan.node("tests")
    assert tests.run_scheme is RunScheme.SH
    assert tests.run_target == "{target.commands.test_cov}"


# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #


def test_defaults_apply_to_model_backed_nodes(plan):
    design = plan.node("design")
    assert design.model == "claude-opus-5"
    assert design.effort is Effort.HIGH          # from defaults
    assert plan.node("intake").effort is Effort.MEDIUM  # explicit override


def test_defaults_do_not_attach_a_model_to_deterministic_nodes(plan):
    """A model recorded against a subprocess would be a lie in the lineage."""
    for node_id in ("tests", "security", "release-readiness", "accept"):
        assert plan.node(node_id).model is None


def test_autonomy_and_retry_budget_are_resolved(plan):
    assert plan.node("design").autonomy is Autonomy.AUTO
    assert plan.node("design").retry_budget == 2
    assert plan.node("security").autonomy is Autonomy.REVIEW  # explicit override


# --------------------------------------------------------------------------- #
# malformed plans fail loudly
# --------------------------------------------------------------------------- #

MINIMAL = """
    plan: t
    version: 1
    nodes:
      - id: a
        kind: tool
        stage: verification
        run: sh:echo
"""


def test_unknown_field_is_rejected(tmp_path):
    """A typo must not silently disable a gate."""
    path = write_plan(tmp_path, MINIMAL + "        gaet: {all: ['x == 1']}\n")
    with pytest.raises(PlanError, match="gaet"):
        load_plan(path)


def test_unknown_dependency_is_rejected(tmp_path):
    orphan = (
        "      - id: b\n        kind: tool\n        stage: verification\n"
        "        run: sh:echo\n        needs: [ghost]\n"
    )
    path = write_plan(tmp_path, MINIMAL + orphan)
    with pytest.raises(PlanError, match="unknown node 'ghost'"):
        load_plan(path)


def test_cycle_is_rejected(tmp_path):
    path = write_plan(
        tmp_path,
        """
        plan: t
        version: 1
        nodes:
          - id: a
            kind: tool
            stage: verification
            run: sh:echo
            needs: [b]
          - id: b
            kind: tool
            stage: verification
            run: sh:echo
            needs: [a]
        """,
    )
    with pytest.raises(PlanError, match="cycle"):
        load_plan(path)


def test_duplicate_ids_are_rejected(tmp_path):
    duplicate = (
        "      - id: a\n        kind: tool\n        stage: verification\n        run: sh:echo\n"
    )
    path = write_plan(tmp_path, MINIMAL + duplicate)
    with pytest.raises(PlanError, match="duplicate node ids: a"):
        load_plan(path)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("      - id: x\n        kind: agent\n        stage: implementation\n", "requires 'role'"),
        ("      - id: x\n        kind: tool\n        stage: implementation\n", "requires 'run'"),
        ("      - id: x\n        kind: fanout\n        stage: implementation\n", "requires 'from'"),
        (
            "      - id: x\n        kind: codeagent\n        stage: implementation\n"
            "        role: r\n",
            "requires 'write_scope'",
        ),
        (
            "      - id: x\n        kind: derive\n        stage: implementation\n",
            "requires 'from' or 'run'",
        ),
    ],
)
def test_kind_specific_requirements_are_enforced(tmp_path, body, expected):
    with pytest.raises(PlanError, match=expected):
        load_plan(write_plan(tmp_path, MINIMAL + body))


def test_unprefixed_run_is_rejected(tmp_path):
    """A shell command that looks like a module path must not be guessable."""
    path = write_plan(
        tmp_path,
        """
        plan: t
        version: 1
        nodes:
          - id: a
            kind: tool
            stage: verification
            run: orchestrator.gates.security_scan
        """,
    )
    with pytest.raises(PlanError, match="must name its scheme"):
        load_plan(path)


def test_human_node_cannot_be_auto(tmp_path):
    auto_human = (
        "      - id: x\n        kind: human\n        stage: release\n        autonomy: AUTO\n"
    )
    path = write_plan(tmp_path, MINIMAL + auto_human)
    with pytest.raises(PlanError, match="only APPROVE is meaningful"):
        load_plan(path)


def test_empty_gate_is_rejected(tmp_path):
    """A gate that checks nothing is worse than no gate — it looks like governance."""
    path = write_plan(tmp_path, MINIMAL + "        gate: {}\n")
    with pytest.raises(PlanError, match="neither 'all' nor 'any'"):
        load_plan(path)


def test_write_scope_outside_the_ceiling_is_rejected(tmp_path):
    """The orchestrator must not be modifiable by the agents it governs."""
    path = write_plan(
        tmp_path,
        """
        plan: t
        version: 1
        nodes:
          - id: a
            kind: codeagent
            stage: implementation
            role: r
            write_scope: ["src/orchestrator/**"]
        """,
    )
    with pytest.raises(PlanError, match="outside the write ceiling"):
        load_plan(path, write_ceiling=["target/**"])


def test_real_plan_satisfies_the_write_ceiling():
    assert load_plan(GREENFIELD, write_ceiling=["target/**"])


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(PlanError, match="plan file not found"):
        load_plan(tmp_path / "nope.yaml")


def test_invalid_yaml_is_reported_clearly(tmp_path):
    path = write_plan(tmp_path, "plan: [unclosed\n")
    with pytest.raises(PlanError, match="invalid YAML"):
        load_plan(path)
