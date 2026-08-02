"""Predicate tests.

The traceability predicates matter most. They are the strongest evidence this
system produces — cheap to compute, impossible to fake — and each has two
directions worth checking: nothing dropped, and nothing invented.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.artifacts import (
    AcceptanceCriterion,
    AcceptanceSuite,
    AcceptanceTest,
    Ambiguity,
    Design,
    DesignElement,
    Disposition,
    Export,
    Finding,
    Interface,
    Module,
    Requirement,
    RequirementRegister,
    SecurityReport,
    Severity,
)
from orchestrator.gates.facts import Fact, FactSource
from orchestrator.gates.predicates import register_all
from orchestrator.gates.registry import PredicateContext, PredicateRegistry
from orchestrator.lineage import recorder
from orchestrator.state import store
from orchestrator.state.artifacts import ArtifactStore
from orchestrator.state.models import Decision, NodeStatus


@pytest.fixture
def registry() -> PredicateRegistry:
    return register_all(PredicateRegistry())


@pytest.fixture
def session():
    with store.Store.in_memory().session() as session:
        yield session


@pytest.fixture
def run(session):
    return store.start_run(
        session,
        plan_name="greenfield",
        plan_version=1,
        requirement_path="requirements/greenfield.md",
        target_profile="config/target.shortener.yaml",
        nodes=[("intake", "agent", "requirements"), ("tests", "tool", "verification")],
    )


@pytest.fixture
def artifacts(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


def record(session, run, artifacts, name: str, model) -> None:
    body = model.model_dump_json() if hasattr(model, "model_dump_json") else json.dumps(model)
    artifact = recorder.record_artifact(session, run, name=name, content=body)
    artifact.path = str(artifacts.write(run.id, name, artifact.version, body))
    session.flush()


def context(session, run, artifacts, **extra) -> PredicateContext:
    return PredicateContext(session=session, run=run, artifacts=artifacts, **extra)


def check(registry, name, ctx) -> tuple[bool, str]:
    return registry.get(name).fn(ctx)


REGISTER = RequirementRegister(
    requirements=[
        Requirement(
            id="R1",
            statement="Submit a long URL, receive a short code",
            acceptance=[AcceptanceCriterion(id="AC1.1", then="201 with a 7-character code")],
        ),
        Requirement(
            id="R2",
            statement="Visiting a short code redirects",
            acceptance=[AcceptanceCriterion(id="AC2.1", then="302 to the original URL")],
        ),
    ],
    ambiguities=[
        Ambiguity(
            id="A1",
            question="301 or 302?",
            severity=Severity.HIGH,
            disposition=Disposition.RESOLVED,
            answer="302 — 301 is cached and would break click analytics",
        )
    ],
)


# --------------------------------------------------------------------------- #
# requirements
# --------------------------------------------------------------------------- #


def test_a_requirement_without_a_testable_criterion_fails(session, run, artifacts, registry):
    register = REGISTER.model_copy(deep=True)
    register.requirements.append(Requirement(id="R3", statement="be fast"))
    record(session, run, artifacts, "intake.register", register)

    passed, detail = check(
        registry, "every_requirement_has_testable_ac", context(session, run, artifacts)
    )
    assert not passed
    assert "R3" in detail


def test_an_empty_register_is_not_vacuously_valid(session, run, artifacts, registry):
    """Zero requirements would otherwise pass every 'for all' check trivially."""
    record(session, run, artifacts, "intake.register", RequirementRegister())
    passed, detail = check(
        registry, "every_requirement_has_testable_ac", context(session, run, artifacts)
    )
    assert not passed
    assert "no requirements" in detail


def test_an_undisposed_ambiguity_blocks(session, run, artifacts, registry):
    register = REGISTER.model_copy(deep=True)
    register.ambiguities.append(
        Ambiguity(id="A2", question="idempotent?", severity=Severity.MEDIUM)
    )
    record(session, run, artifacts, "intake.register", register)

    passed, detail = check(
        registry, "no_ambiguity_without_disposition", context(session, run, artifacts)
    )
    assert not passed
    assert "A2" in detail


def test_a_recorded_assumption_counts_as_a_disposition(session, run, artifacts, registry):
    """D13: only high-severity ambiguities need a human; the rest carry assumptions."""
    register = REGISTER.model_copy(deep=True)
    register.ambiguities.append(
        Ambiguity(
            id="A2",
            question="idempotent?",
            severity=Severity.MEDIUM,
            disposition=Disposition.ASSUMPTION,
            answer="No — each submission yields a new code",
        )
    )
    record(session, run, artifacts, "intake.register", register)

    passed, _ = check(
        registry, "no_ambiguity_without_disposition", context(session, run, artifacts)
    )
    assert passed


def test_an_open_high_severity_ambiguity_triggers_escalation(session, run, artifacts, registry):
    register = REGISTER.model_copy(deep=True)
    register.ambiguities.append(
        Ambiguity(id="A3", question="rate limit scope?", severity=Severity.HIGH)
    )
    record(session, run, artifacts, "intake.register", register)

    escalates, detail = check(
        registry, "has_high_severity_ambiguity", context(session, run, artifacts)
    )
    assert escalates
    assert "A3" in detail


def test_a_disposed_high_ambiguity_does_not_escalate_again(session, run, artifacts, registry):
    record(session, run, artifacts, "intake.register", REGISTER)
    escalates, _ = check(
        registry, "has_high_severity_ambiguity", context(session, run, artifacts)
    )
    assert not escalates


# --------------------------------------------------------------------------- #
# traceability — both directions
# --------------------------------------------------------------------------- #

DESIGN = Design(
    elements=[
        DesignElement(id="E1", kind="endpoint", satisfies=["R1"]),
        DesignElement(id="E2", kind="endpoint", satisfies=["R2"]),
    ],
    modules=[Module(name="api", path="api")],
    endpoints=["/shorten", "/{code}"],
)


def test_a_requirement_with_no_design_is_caught(session, run, artifacts, registry):
    """Nothing silently dropped between stages."""
    record(session, run, artifacts, "intake.register", REGISTER)
    partial = Design(elements=[DesignElement(id="E1", kind="endpoint", satisfies=["R1"])])
    record(session, run, artifacts, "design.spec", partial)

    passed, detail = check(
        registry, "requirement_design_matrix_complete", context(session, run, artifacts)
    )
    assert not passed
    assert "R2" in detail


def test_a_design_element_nobody_asked_for_is_caught(session, run, artifacts, registry):
    """The reverse direction — gold-plating."""
    record(session, run, artifacts, "intake.register", REGISTER)
    padded = DESIGN.model_copy(deep=True)
    padded.elements.append(DesignElement(id="E9", kind="endpoint", satisfies=["R99"]))
    record(session, run, artifacts, "design.spec", padded)

    passed, detail = check(
        registry, "no_unmapped_design_elements", context(session, run, artifacts)
    )
    assert not passed
    assert "E9" in detail
    assert "nobody asked for" in detail


def test_a_complete_matrix_passes_in_both_directions(session, run, artifacts, registry):
    record(session, run, artifacts, "intake.register", REGISTER)
    record(session, run, artifacts, "design.spec", DESIGN)
    ctx = context(session, run, artifacts)

    assert check(registry, "requirement_design_matrix_complete", ctx)[0]
    assert check(registry, "no_unmapped_design_elements", ctx)[0]


def test_an_uncovered_acceptance_criterion_is_named(session, run, artifacts, registry):
    record(session, run, artifacts, "intake.register", REGISTER)
    record(
        session,
        run,
        artifacts,
        "tests-acceptance.suite",
        AcceptanceSuite(tests=[AcceptanceTest(id="t1", covers=["AC1.1"])]),
    )

    passed, detail = check(registry, "every_ac_has_a_test", context(session, run, artifacts))
    assert not passed
    assert "AC2.1" in detail


def test_full_coverage_passes(session, run, artifacts, registry):
    record(session, run, artifacts, "intake.register", REGISTER)
    record(
        session,
        run,
        artifacts,
        "tests-acceptance.suite",
        AcceptanceSuite(tests=[AcceptanceTest(id="t1", covers=["AC1.1", "AC2.1"])]),
    )
    assert check(registry, "ac_test_matrix_complete", context(session, run, artifacts))[0]


# --------------------------------------------------------------------------- #
# security
# --------------------------------------------------------------------------- #


def test_an_open_high_finding_blocks_release(session, run, artifacts, registry):
    report = SecurityReport(
        findings=[Finding(id="S1", title="open redirect", severity=Severity.HIGH)]
    )
    record(session, run, artifacts, "security.report", report)

    passed, detail = check(
        registry, "no_unapproved_high_findings", context(session, run, artifacts)
    )
    assert not passed
    assert "S1" in detail


def test_a_waiver_with_no_person_named_is_not_a_waiver(session, run, artifacts, registry):
    """D15: an agent cannot waive a finding, so an unattributed waiver is void."""
    report = SecurityReport(
        findings=[
            Finding(id="S1", title="open redirect", severity=Severity.HIGH, waived=True)
        ]
    )
    record(session, run, artifacts, "security.report", report)

    passed, detail = check(
        registry, "no_unapproved_high_findings", context(session, run, artifacts)
    )
    assert not passed
    assert "no person named" in detail


def test_a_human_waiver_is_accepted(session, run, artifacts, registry):
    report = SecurityReport(
        findings=[
            Finding(
                id="S1",
                title="open redirect",
                severity=Severity.HIGH,
                waived=True,
                waived_by="alice",
                rationale="destination allowlist ships next sprint",
            )
        ]
    )
    record(session, run, artifacts, "security.report", report)
    assert check(registry, "no_unapproved_high_findings", context(session, run, artifacts))[0]


# --------------------------------------------------------------------------- #
# release readiness
# --------------------------------------------------------------------------- #


def test_a_stale_approval_blocks_release(session, run, artifacts, registry):
    """D10, reaching G10."""
    v1 = recorder.record_artifact(session, run, name="design.openapi", content="v1")
    approval = recorder.request_approval(session, run, node_id="design-approval", artifacts=[v1])
    recorder.decide(session, approval, decision=Decision.APPROVED, decided_by="alice")
    recorder.record_artifact(session, run, name="design.openapi", content="v2")

    passed, detail = check(registry, "no_stale_approvals", context(session, run, artifacts))
    assert not passed
    assert "v2 now exists" in detail


def test_an_unfinished_node_blocks_release(session, run, artifacts, registry):
    passed, detail = check(
        registry, "no_node_in_nonterminal_state", context(session, run, artifacts)
    )
    assert not passed
    assert "intake" in detail


def test_an_orphaned_artifact_blocks_release(session, run, artifacts, registry):
    recorder.record_artifact(session, run, name="mystery", content="{}")
    passed, detail = check(registry, "lineage_complete", context(session, run, artifacts))
    assert not passed
    assert "mystery" in detail


def test_a_failed_node_blocks_release(session, run, artifacts, registry):
    store.get_node(session, run, "tests").status = NodeStatus.FAILED
    session.flush()

    passed, detail = check(
        registry, "all_upstream_gates_green", context(session, run, artifacts)
    )
    assert not passed
    assert "tests" in detail


# --------------------------------------------------------------------------- #
# documentation
# --------------------------------------------------------------------------- #


def test_the_docs_gate_needs_evidence_the_setup_actually_ran(session, run, artifacts, registry):
    """A doc gate that checks for headings is vacuous; one that trusts the author
    is worse."""
    passed, detail = check(
        registry, "setup_steps_execute_in_clean_venv", context(session, run, artifacts)
    )
    assert not passed
    assert "no recorded result" in detail


def test_failing_setup_steps_fail_the_docs_gate(session, run, artifacts, registry):
    ctx = context(session, run, artifacts)
    ctx.facts = {"setup.exit_code": Fact(1, FactSource.TOOL, "setup")}
    passed, detail = check(registry, "setup_steps_execute_in_clean_venv", ctx)
    assert not passed
    assert "exit code 1" in detail


def test_working_setup_steps_pass(session, run, artifacts, registry):
    ctx = context(session, run, artifacts)
    ctx.facts = {"setup.exit_code": Fact(0, FactSource.TOOL, "setup")}
    assert check(registry, "setup_steps_execute_in_clean_venv", ctx)[0]


def test_an_undocumented_endpoint_is_caught(session, run, artifacts, registry):
    record(session, run, artifacts, "design.spec", DESIGN)
    body = "## API\n\nPOST /shorten — create a short link\n"
    artifact = recorder.record_artifact(session, run, name="docs.readme", content=body)
    artifact.path = str(artifacts.write(run.id, "docs.readme", artifact.version, body))
    session.flush()

    passed, detail = check(
        registry, "documented_endpoints_match_openapi", context(session, run, artifacts)
    )
    assert not passed
    assert "/{code}" in detail


# --------------------------------------------------------------------------- #
# the harness itself
# --------------------------------------------------------------------------- #


def test_a_predicate_without_its_context_reports_a_harness_problem(registry):
    """Not a finding: evaluating without a run should read as ERROR, not FAIL."""
    with pytest.raises(LookupError, match="harness problem"):
        check(registry, "no_stale_approvals", PredicateContext())


def test_every_predicate_the_greenfield_plan_names_is_registered(registry):
    from orchestrator.engine.loader import load_plan

    plan = load_plan("plans/greenfield.yaml")
    assert registry.missing(plan.required_predicates) == []


def test_every_registered_predicate_describes_itself(registry):
    """The description is what a preflight check shows when something is missing."""
    for name in registry.names:
        assert registry.get(name).description


# --------------------------------------------------------------------------- #
# the API contract
# --------------------------------------------------------------------------- #


def design_with(*endpoints: str) -> Design:
    return Design(endpoints=list(endpoints))


def contract(registry, session, run, artifacts, design: Design):
    record(session, run, artifacts, "design.spec", design)
    return check(registry, "contract_is_valid", context(session, run, artifacts))


def test_a_well_formed_contract_passes(registry, session, run, artifacts):
    passed, _ = contract(
        registry, session, run, artifacts,
        design_with("POST /shorten", "GET /{code}", "GET /links/{code}/stats"),
    )
    assert passed


def test_a_design_with_no_endpoints_has_no_contract_to_keep(registry, session, run, artifacts):
    passed, detail = contract(registry, session, run, artifacts, design_with())
    assert not passed
    assert "no endpoints" in detail


def test_an_endpoint_nobody_can_parse_fails(registry, session, run, artifacts):
    """The documentation gate compares the README against these; an endpoint
    that does not parse matches nothing and would fail there instead, later."""
    passed, detail = contract(
        registry, session, run, artifacts, design_with("shorten a url", "GET /ok")
    )
    assert not passed
    assert "shorten a url" in detail


def test_a_duplicated_endpoint_fails(registry, session, run, artifacts):
    passed, detail = contract(
        registry, session, run, artifacts, design_with("GET /{code}", "GET /{code}")
    )
    assert not passed
    assert "twice" in detail


def test_an_unbalanced_path_parameter_fails(registry, session, run, artifacts):
    passed, detail = contract(registry, session, run, artifacts, design_with("GET /{code"))
    assert not passed
    assert "unbalanced" in detail or "not '<METHOD>" in detail


# --------------------------------------------------------------------------- #
# the module contract — what makes parallel implementation safe (D24)
# --------------------------------------------------------------------------- #


def module_contract(registry, session, run, artifacts, design: Design, predicate: str):
    record(session, run, artifacts, "design.spec", design)
    return check(registry, predicate, context(session, run, artifacts))


def two_modules(**interfaces: Interface) -> Design:
    return Design(
        modules=[Module(name=name, path=name) for name in interfaces],
        interfaces=list(interfaces.values()),
    )


def exporting(module: str, *names: str, depends_on: list[str] | None = None) -> Interface:
    return Interface(
        module=module,
        depends_on=depends_on or [],
        exports=[Export(name=name, kind="function", signature="() -> None") for name in names],
    )


def test_a_settled_contract_lets_the_fan_out_proceed(registry, session, run, artifacts):
    design = two_modules(
        errors=exporting("errors", "LinkNotFound"),
        links=exporting("links", "resolve", depends_on=["errors"]),
    )
    passed, detail = module_contract(
        registry, session, run, artifacts, design, "every_module_has_an_interface"
    )
    assert passed
    assert "2 modules" in detail


def test_a_module_with_no_interface_blocks_the_fan_out(registry, session, run, artifacts):
    """Exactly the failure: `errors` declared nothing, so `links` invented names."""
    design = Design(
        modules=[Module(name="errors", path="errors"), Module(name="links", path="links")],
        interfaces=[exporting("links", "resolve", depends_on=["errors"])],
    )
    passed, detail = module_contract(
        registry, session, run, artifacts, design, "every_module_has_an_interface"
    )
    assert not passed
    assert "errors" in detail


def test_a_module_that_exports_nothing_is_not_a_module(registry, session, run, artifacts):
    design = two_modules(
        errors=Interface(module="errors"),
        links=exporting("links", "resolve", depends_on=["errors"]),
    )
    passed, detail = module_contract(
        registry, session, run, artifacts, design, "every_module_has_an_interface"
    )
    assert not passed
    assert "export nothing" in detail


def test_an_acyclic_contract_passes(registry, session, run, artifacts):
    design = two_modules(
        errors=exporting("errors", "LinkNotFound"),
        links=exporting("links", "resolve", depends_on=["errors"]),
    )
    passed, _ = module_contract(
        registry, session, run, artifacts, design, "module_dependencies_are_acyclic"
    )
    assert passed


def test_a_cycle_is_caught_at_the_design_gate(registry, session, run, artifacts):
    """Two modules that must change together are one module.

    Caught here rather than at scaffold, so the architect is told before a human
    approves a decomposition that cannot be implemented in parallel.
    """
    design = two_modules(
        a=exporting("a", "f", depends_on=["b"]),
        b=exporting("b", "g", depends_on=["a"]),
    )
    passed, detail = module_contract(
        registry, session, run, artifacts, design, "module_dependencies_are_acyclic"
    )
    assert not passed
    assert "cycle" in detail


def test_a_dependency_on_a_module_nobody_will_write_fails(registry, session, run, artifacts):
    design = Design(
        modules=[Module(name="links", path="links")],
        interfaces=[exporting("links", "resolve", depends_on=["storage"])],
    )
    passed, detail = module_contract(
        registry, session, run, artifacts, design, "module_dependencies_are_acyclic"
    )
    assert not passed
    assert "storage" in detail


# --------------------------------------------------------------------------- #
# triage's exit gate — asserting the policy ran, not that nobody is needed
# --------------------------------------------------------------------------- #


def triaged(registry, session, run, artifacts, *ambiguities, threshold=None):
    from orchestrator.engine.plan import Node

    record(session, run, artifacts, "intake.register", RequirementRegister(
        ambiguities=list(ambiguities)
    ))
    node = Node.model_validate({
        "id": "ambiguity-triage",
        "kind": "tool",
        "stage": "requirements",
        "run": "py:orchestrator.policy.triage_ambiguities",
        "params": {"threshold": threshold} if threshold else {},
    })
    return check(
        registry,
        "every_ambiguity_is_disposed_or_escalated",
        context(session, run, artifacts, node=node),
    )


def test_an_open_high_ambiguity_is_the_node_working(registry, session, run, artifacts):
    """The bug a live run found: gating on 'nothing undisposed' fails exactly
    when the escalation path is doing its job, so the node burned its retry
    budget re-running a pure function and never reached `on_escalate`."""
    passed, detail = triaged(
        registry, session, run, artifacts,
        Ambiguity(id="A1", question="301 or 302?", severity=Severity.HIGH),
        Ambiguity(id="A2", question="idempotent?", severity=Severity.MEDIUM,
                  disposition=Disposition.ASSUMPTION, answer="assumed"),
    )
    assert passed
    assert "await a person" in detail


def test_a_skipped_low_severity_ambiguity_fails_the_gate(registry, session, run, artifacts):
    """Below the threshold and undisposed means the policy missed it."""
    passed, detail = triaged(
        registry, session, run, artifacts,
        Ambiguity(id="A9", question="page size?", severity=Severity.LOW),
    )
    assert not passed
    assert "A9" in detail


def test_the_threshold_the_plan_set_is_the_one_checked(registry, session, run, artifacts):
    """`ambiguous.yaml` lowers it to medium; an open MEDIUM is then legitimate."""
    passed, _ = triaged(
        registry, session, run, artifacts,
        Ambiguity(id="A2", question="scope?", severity=Severity.MEDIUM),
        threshold="medium",
    )
    assert passed


# --------------------------------------------------------------------------- #
# executable documentation (G8)
# --------------------------------------------------------------------------- #


def documented(registry, session, run, artifacts, prose: str, *endpoints: str):
    record(session, run, artifacts, "design.spec", Design(endpoints=list(endpoints)))
    artifact = recorder.record_artifact(session, run, name="docs.readme", content=prose)
    artifact.path = str(artifacts.write(run.id, "docs.readme", artifact.version, prose))
    session.flush()
    return check(registry, "documented_endpoints_match_openapi", context(session, run, artifacts))


def test_a_readme_documenting_the_contract_passes(registry, session, run, artifacts):
    """It could not, before: the check compared `/api/links` against the
    contract's `POST /api/links`, so the two sets never intersected and every
    endpoint was reported as both undocumented *and* invented."""
    prose = "## API\n\n- `POST /api/links` creates one\n- `GET /{code}` redirects\n"
    passed, detail = documented(
        registry, session, run, artifacts, prose, "POST /api/links", "GET /{code}"
    )
    assert passed, detail


def test_an_endpoint_the_readme_never_mentions_is_caught(registry, session, run, artifacts):
    prose = "## API\n\n- `POST /api/links` creates one\n"
    passed, detail = documented(
        registry, session, run, artifacts, prose, "POST /api/links", "GET /health"
    )
    assert not passed
    assert "GET /health" in detail


def test_an_endpoint_the_contract_never_promised_is_caught(registry, session, run, artifacts):
    prose = "- `POST /api/links`\n- `DELETE /api/admin/wipe`\n"
    passed, detail = documented(registry, session, run, artifacts, prose, "POST /api/links")
    assert not passed
    assert "DELETE /api/admin/wipe" in detail


def test_the_setup_predicate_says_why_the_steps_failed(registry, session, run, artifacts):
    """A retry is told what to fix, and "exit code 1" is not that."""
    facts = {
        "setup.exit_code": Fact(1, FactSource.TOOL, "setup"),
        "setup.detail": Fact("ImportError: cannot import name 'UTC'", FactSource.TOOL, "setup"),
    }
    passed, detail = check(
        registry,
        "setup_steps_execute_in_clean_venv",
        context(session, run, artifacts, facts=facts),
    )
    assert not passed
    assert "UTC" in detail
