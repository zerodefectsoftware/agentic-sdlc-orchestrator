"""Gate evaluator tests.

The load-bearing assertions are about what happens when a gate *cannot* be
evaluated. A missing fact, an unimplemented predicate, or a predicate that
raises must never report PASS — an unperformed check reporting green is the
failure mode this whole design exists to prevent.
"""

from __future__ import annotations

import pytest

from orchestrator.engine.plan import Gate
from orchestrator.gates import (
    Fact,
    FactSource,
    PredicateRegistry,
    Verdict,
    evaluate_gate,
    required_predicates,
    tool_facts,
)
from orchestrator.gates.expressions import ExpressionError, evaluate, parse


def gate(*checks, any_of=None) -> Gate:
    payload: dict = {}
    if checks:
        payload["all"] = list(checks)
    if any_of:
        payload["any"] = list(any_of)
    return Gate.model_validate(payload)


@pytest.fixture
def empty_registry() -> PredicateRegistry:
    return PredicateRegistry()


# --------------------------------------------------------------------------- #
# the expression language
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("expression", "value", "expected"),
    [
        ("pytest.exit_code == 0", 0, True),
        ("pytest.exit_code == 0", 1, False),
        ("pytest.exit_code != 0", 1, True),  # the RED gate (D5)
        ("pytest.exit_code != 0", 0, False),
        ("coverage.percent >= 80", 80, True),
        ("coverage.percent >= 80", 79.9, False),
        ("coverage.percent < 80", 12, True),
        ("openapi.valid == true", True, True),
        ("openapi.valid == false", True, False),
        ("scan.findings == null", None, True),
        ("release.channel == 'beta'", "beta", True),
    ],
)
def test_expressions_evaluate(expression, value, expected):
    assert evaluate(expression, value) is expected


@pytest.mark.parametrize(
    "expression",
    [
        "pytest.exit_code",              # no operator
        "== 0",                          # no path
        "pytest.exit_code = 0",          # assignment, not comparison
        "pytest.exit_code == ",          # no literal
        "pytest.exit_code === 0",        # not an operator we support
        "__import__('os') == 0",         # not a fact path
    ],
)
def test_malformed_expressions_are_rejected(expression):
    with pytest.raises(ExpressionError):
        parse(expression)


def test_ordering_requires_numbers():
    """`coverage.percent >= 80` against a string is a harness bug, not a failure."""
    with pytest.raises(ExpressionError, match="ordering requires numbers"):
        evaluate("coverage.percent >= 80", "high")


def test_booleans_are_not_ordered():
    """`flag > 0` reads as a mistake rather than intent."""
    with pytest.raises(ExpressionError, match="ordering requires numbers"):
        evaluate("openapi.valid > 0", True)


# --------------------------------------------------------------------------- #
# a check that cannot be performed is never a pass
# --------------------------------------------------------------------------- #


def test_missing_fact_is_an_error_not_a_failure():
    result = evaluate_gate(gate("pytest.exit_code == 0"), {})
    assert result.verdict is Verdict.ERROR
    assert "no fact recorded" in result.checks[0].detail


def test_unknown_predicate_is_an_error_not_a_pass(empty_registry):
    """An unimplemented check reporting green is the most dangerous state possible."""
    result = evaluate_gate(
        gate({"predicate": "not_built_yet"}), {}, registry=empty_registry
    )
    assert result.verdict is Verdict.ERROR
    assert "no predicate registered" in result.checks[0].detail


def test_raising_predicate_is_an_error_not_a_failure(empty_registry):
    @empty_registry.register("explodes", "always raises")
    def _explodes(facts):
        raise RuntimeError("boom")

    result = evaluate_gate(gate({"predicate": "explodes"}), {}, registry=empty_registry)
    assert result.verdict is Verdict.ERROR
    assert "RuntimeError: boom" in result.checks[0].detail


def test_unparseable_expression_is_an_error():
    result = evaluate_gate(gate("nonsense"), {})
    assert result.verdict is Verdict.ERROR


def test_every_non_pass_verdict_blocks():
    assert Verdict.FAIL.blocks
    assert Verdict.ERROR.blocks
    assert not Verdict.PASS.blocks


def test_error_outranks_failure_when_reporting():
    """Both block, but an unknown needs a different response than a known-bad."""
    facts = {"ruff.exit_code": Fact(1, FactSource.TOOL)}
    result = evaluate_gate(gate("ruff.exit_code == 0", "missing.fact == 1"), facts)
    assert result.verdict is Verdict.ERROR
    assert {check.verdict for check in result.checks} == {Verdict.FAIL, Verdict.ERROR}


# --------------------------------------------------------------------------- #
# D4: a producer's own claim is not evidence about itself
# --------------------------------------------------------------------------- #


def test_agent_self_report_is_inadmissible():
    facts = {"impl.complete": Fact(True, FactSource.AGENT, produced_by="implementer")}
    result = evaluate_gate(gate("impl.complete == true"), facts)

    assert result.verdict is Verdict.ERROR
    assert "inadmissible" in result.checks[0].detail


def test_validator_output_over_an_agent_artifact_is_admissible():
    """The artifact is the subject of the check, never its author."""
    facts = {"schema.valid": Fact(True, FactSource.VALIDATOR, produced_by="jsonschema")}
    assert evaluate_gate(gate("schema.valid == true"), facts).passed


@pytest.mark.parametrize(
    "source", [FactSource.TOOL, FactSource.VALIDATOR, FactSource.DERIVED, FactSource.HUMAN]
)
def test_every_source_but_agent_is_admissible(source):
    assert source.is_admissible
    facts = {"x.y": Fact(1, source)}
    assert evaluate_gate(gate("x.y == 1"), facts).passed


def test_agent_source_is_the_only_inadmissible_one():
    assert not FactSource.AGENT.is_admissible


# --------------------------------------------------------------------------- #
# combining checks
# --------------------------------------------------------------------------- #


def test_all_checks_must_pass():
    facts = tool_facts("pytest", **{"pytest.exit_code": 0, "coverage.percent": 62})
    result = evaluate_gate(gate("pytest.exit_code == 0", "coverage.percent >= 80"), facts)
    assert result.verdict is Verdict.FAIL
    assert len(result.failures) == 1


def test_any_checks_need_one_passing_member():
    facts = tool_facts("scan", **{"scan.high": 0, "scan.waived": False})
    result = evaluate_gate(gate(any_of=["scan.high == 0", "scan.waived == true"]), facts)
    assert result.passed


def test_any_checks_fail_when_none_hold():
    facts = tool_facts("scan", **{"scan.high": 3, "scan.waived": False})
    result = evaluate_gate(gate(any_of=["scan.high == 0", "scan.waived == true"]), facts)
    assert result.verdict is Verdict.FAIL


def test_all_and_any_must_both_be_satisfied():
    facts = tool_facts("scan", **{"ruff.exit_code": 0, "scan.high": 3, "scan.waived": False})
    result = evaluate_gate(
        gate("ruff.exit_code == 0", any_of=["scan.high == 0", "scan.waived == true"]),
        facts,
    )
    assert result.verdict is Verdict.FAIL


# --------------------------------------------------------------------------- #
# what the evidence bundle reads
# --------------------------------------------------------------------------- #


def test_result_records_the_observed_value_and_its_provenance():
    """'FAIL' alone is useless to a reviewer; 'observed 1 (tool from pytest)' is not."""
    facts = tool_facts("pytest", **{"pytest.exit_code": 1})
    check = evaluate_gate(gate("pytest.exit_code == 0"), facts).checks[0]

    assert check.observed is not None
    assert "1" in check.observed
    assert "tool" in check.observed
    assert "pytest" in check.observed


def test_result_records_evaluator_and_time():
    """§5.4: the bundle must show which evaluator reached what verdict, and when."""
    result = evaluate_gate(gate("x.y == 1"), {"x.y": Fact(1, FactSource.TOOL)})
    assert result.evaluator == "orchestrator.gates"
    assert result.evaluated_at.tzinfo is not None


def test_predicate_detail_reaches_the_summary(empty_registry):
    @empty_registry.register("ac_test_matrix_complete", "every AC has a test")
    def _matrix(facts):
        return False, "2 acceptance criteria have no test: AC1.2, AC3.1"

    result = evaluate_gate(
        gate({"predicate": "ac_test_matrix_complete"}), {}, registry=empty_registry
    )
    assert "AC1.2" in result.summary()


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_registry_reports_what_a_plan_needs_but_lacks(empty_registry):
    """Preflight: a run should refuse to start when its plan names checks we lack."""

    @empty_registry.register("schema_valid", "artifact matches its schema")
    def _schema_valid(facts):
        return True, "ok"

    required = ["schema_valid", "no_stale_approvals", "lineage_complete"]
    assert empty_registry.missing(required) == ["lineage_complete", "no_stale_approvals"]


def test_registry_rejects_duplicate_registration(empty_registry):
    @empty_registry.register("dup", "first")
    def _first(facts):
        return True, ""

    with pytest.raises(ValueError, match="already registered"):
        empty_registry.register("dup", "second")(lambda facts: (True, ""))


def test_required_predicates_extracts_names_from_a_gate():
    g = gate("ruff.exit_code == 0", {"predicate": "a"}, {"predicate": "b"})
    assert required_predicates(g) == ["a", "b"]


# --------------------------------------------------------------------------- #
# against the real plan
# --------------------------------------------------------------------------- #


def test_real_greenfield_gates_are_evaluable():
    """Every expression in the shipped plan parses; nothing waits until runtime."""
    from orchestrator.engine.loader import load_plan
    from orchestrator.engine.plan import ExpressionCheck

    plan = load_plan("plans/greenfield.yaml")
    for node in plan.nodes:
        for gate_obj in (node.gate, node.entry_gate):
            if gate_obj is None:
                continue
            for check in gate_obj.checks:
                if isinstance(check, ExpressionCheck):
                    parse(_strip_templates(check.expression))


def _strip_templates(expression: str) -> str:
    """Profile placeholders are interpolated before evaluation; stand in a number."""
    import re

    return re.sub(r"\{[^}]+\}", "80", expression)


def test_scaffold_gate_passes_against_a_clean_build():
    from orchestrator.engine.loader import load_plan

    plan = load_plan("plans/greenfield.yaml")
    facts = tool_facts("scaffold", **{"imports.resolve": True, "ruff.exit_code": 0})
    assert evaluate_gate(plan.node("scaffold").gate, facts).passed


def test_red_gate_rejects_a_suite_that_already_passes():
    """D5: a suite passing against an empty scaffold asserts nothing about new behaviour."""
    from orchestrator.engine.loader import load_plan

    plan = load_plan("plans/greenfield.yaml")
    red = plan.node("tests-acceptance").gate

    passing = tool_facts("pytest", **{"pytest.exit_code": 0})
    failing = tool_facts("pytest", **{"pytest.exit_code": 1})

    # The expression half of the RED gate rejects a green suite and accepts a red one.
    expression_only = Gate.model_validate({"all": ["pytest.exit_code != 0"]})
    assert not evaluate_gate(expression_only, passing).passed
    assert evaluate_gate(expression_only, failing).passed

    # The full gate still errors, because every_ac_has_a_test is not implemented yet.
    assert evaluate_gate(red, failing).verdict is Verdict.ERROR
