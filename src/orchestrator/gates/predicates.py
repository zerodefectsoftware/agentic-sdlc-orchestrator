"""The registered predicates.

Where an expression would be a lie about the complexity. These fall into three
groups, and the grouping says something about the design:

- **Artifact predicates** read an agent's output and check a property of it —
  traceability matrices, disposed ambiguities. The artifact is the *subject* of
  the check, never its author (D4).
- **Run predicates** query state and lineage — stale approvals, unfinished
  nodes, orphaned artifacts. None of these is expressible as a fact a tool
  emitted, which is why `PredicateContext` exists.
- **Evidence predicates** confirm that a tool actually ran. `setup_steps_execute`
  does not itself execute anything: producing the evidence is a tool node's job,
  and the predicate's job is refusing to accept a gate with no evidence behind it.

Each returns `(passed, detail)`. The detail is what a reviewer reads in the
evidence bundle, so "3 acceptance criteria have no test: AC1.2, AC3.1, AC4.4"
earns its place over `False`.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from orchestrator.artifacts import (
    AcceptanceSuite,
    Design,
    RequirementRegister,
    SecurityReport,
)
from orchestrator.gates.registry import PredicateContext, PredicateRegistry
from orchestrator.gates.registry import registry as default_registry
from orchestrator.lineage import query, recorder
from orchestrator.state import store
from orchestrator.state.models import NodeStatus


def _load(context: PredicateContext, name: str, model):
    """Read a recorded artifact and parse it against its contract."""
    session, run, artifacts = context.require("session", "run", "artifacts")
    artifact = recorder.latest(session, run, name)
    if artifact is None:
        raise LookupError(f"no artifact '{name}' has been recorded in this run")
    return model.model_validate_json(artifacts.read(artifact))


def _listed(items: list[str], limit: int = 5) -> str:
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown}, … (+{len(items) - limit} more)"


def register_all(registry: PredicateRegistry | None = None) -> PredicateRegistry:
    """Register every predicate the shipped plans name.

    Explicit rather than import-time magic: a preflight check should be able to
    build a registry, ask what a plan needs, and get a straight answer.
    """
    reg = registry if registry is not None else default_registry

    # ------------------------------------------------------------------ #
    # requirements
    # ------------------------------------------------------------------ #

    @reg.register("schema_valid", "the artifact parses against its declared contract")
    def schema_valid(context: PredicateContext) -> tuple[bool, str]:
        node = context.require("node")
        if not node.output_schema:
            return True, "no schema declared for this node"
        try:
            _load(context, f"{node.id}.register", RequirementRegister)
        except ValidationError as exc:
            return False, f"artifact does not match its schema: {exc.error_count()} errors"
        return True, "artifact matches its schema"

    @reg.register(
        "every_requirement_has_testable_ac",
        "each requirement carries at least one acceptance criterion with an outcome",
    )
    def every_requirement_has_testable_ac(context: PredicateContext) -> tuple[bool, str]:
        register = _load(context, "intake.register", RequirementRegister)
        if not register.requirements:
            return False, "the register contains no requirements"

        bare = [
            r.id
            for r in register.requirements
            if not any(ac.is_testable for ac in r.acceptance)
        ]
        if bare:
            return False, f"{len(bare)} requirements have no testable criterion: {_listed(bare)}"
        return True, f"all {len(register.requirements)} requirements have testable criteria"

    @reg.register(
        "no_ambiguity_without_disposition",
        "every ambiguity is resolved or carries a recorded assumption",
    )
    def no_ambiguity_without_disposition(context: PredicateContext) -> tuple[bool, str]:
        register = _load(context, "intake.register", RequirementRegister)
        open_items = [a.id for a in register.ambiguities if not a.is_disposed]
        if open_items:
            return False, f"{len(open_items)} ambiguities undisposed: {_listed(open_items)}"
        return True, f"all {len(register.ambiguities)} ambiguities disposed"

    @reg.register(
        "has_high_severity_ambiguity",
        "an ambiguity consequential enough to require a human (D13)",
    )
    def has_high_severity_ambiguity(context: PredicateContext) -> tuple[bool, str]:
        register = _load(context, "intake.register", RequirementRegister)
        high = [a.id for a in register.ambiguities if a.severity.is_high and not a.is_disposed]
        if high:
            return True, f"{len(high)} high-severity ambiguities: {_listed(high)}"
        return False, "no undisposed high-severity ambiguities"

    # ------------------------------------------------------------------ #
    # traceability
    # ------------------------------------------------------------------ #

    @reg.register(
        "requirement_design_matrix_complete",
        "every requirement is satisfied by at least one design element",
    )
    def requirement_design_matrix_complete(context: PredicateContext) -> tuple[bool, str]:
        register = _load(context, "intake.register", RequirementRegister)
        design = _load(context, "design.design", Design)

        satisfied = {rid for element in design.elements for rid in element.satisfies}
        orphans = [r.id for r in register.requirements if r.id not in satisfied]
        if orphans:
            return False, f"{len(orphans)} requirements have no design: {_listed(orphans)}"
        return True, f"all {len(register.requirements)} requirements map to design"

    @reg.register(
        "no_unmapped_design_elements",
        "every design element traces back to a requirement — catches gold-plating",
    )
    def no_unmapped_design_elements(context: PredicateContext) -> tuple[bool, str]:
        register = _load(context, "intake.register", RequirementRegister)
        design = _load(context, "design.design", Design)

        known = {r.id for r in register.requirements}
        invented = [
            e.id for e in design.elements if not e.satisfies or not set(e.satisfies) & known
        ]
        if invented:
            return False, (
                f"{len(invented)} design elements satisfy no requirement "
                f"(work nobody asked for): {_listed(invented)}"
            )
        return True, f"all {len(design.elements)} design elements trace to a requirement"

    @reg.register(
        "every_ac_has_a_test",
        "each acceptance criterion is covered by at least one test",
    )
    def every_ac_has_a_test(context: PredicateContext) -> tuple[bool, str]:
        register = _load(context, "intake.register", RequirementRegister)
        suite = _load(context, "tests-acceptance.suite", AcceptanceSuite)

        covered = {ac_id for test in suite.tests for ac_id in test.covers}
        uncovered = [ac.id for ac in register.acceptance_criteria if ac.id not in covered]
        if uncovered:
            return False, (
                f"{len(uncovered)} acceptance criteria have no test: {_listed(uncovered)}"
            )
        return True, f"all {len(register.acceptance_criteria)} acceptance criteria covered"

    reg.register(
        "ac_test_matrix_complete",
        "alias of every_ac_has_a_test, checked again after implementation",
    )(every_ac_has_a_test)

    # ------------------------------------------------------------------ #
    # security
    # ------------------------------------------------------------------ #

    @reg.register("has_high_severity_finding", "a finding that forces a human decision")
    def has_high_severity_finding(context: PredicateContext) -> tuple[bool, str]:
        report = _load(context, "security.report", SecurityReport)
        high = [f.id for f in report.findings if f.is_open_high]
        if high:
            return True, f"{len(high)} open high-severity findings: {_listed(high)}"
        return False, "no open high-severity findings"

    @reg.register(
        "no_unapproved_high_findings",
        "high-severity findings are fixed or waived by a person (D15)",
    )
    def no_unapproved_high_findings(context: PredicateContext) -> tuple[bool, str]:
        report = _load(context, "security.report", SecurityReport)

        unwaived = [f.id for f in report.findings if f.is_open_high]
        if unwaived:
            return False, f"{len(unwaived)} unwaived high findings: {_listed(unwaived)}"

        # D15 again, from the other side: a waiver with no human attached is not a waiver.
        agent_waived = [
            f.id for f in report.findings if f.waived and not (f.waived_by or "").strip()
        ]
        if agent_waived:
            return False, (
                f"{len(agent_waived)} findings waived with no person named: "
                f"{_listed(agent_waived)}"
            )
        return True, f"{len(report.findings)} findings, none open at high severity"

    # ------------------------------------------------------------------ #
    # release readiness — run predicates
    # ------------------------------------------------------------------ #

    @reg.register("no_stale_approvals", "no approval covers a superseded artifact (D10)")
    def no_stale_approvals(context: PredicateContext) -> tuple[bool, str]:
        session, run = context.require("session", "run")
        stale = query.stale_approvals(session, run)
        if stale:
            return False, "; ".join(str(item) for item in stale)
        return True, "every approval covers the current artifact version"

    @reg.register(
        "no_node_in_nonterminal_state", "no work is still pending, running, or blocked"
    )
    def no_node_in_nonterminal_state(context: PredicateContext) -> tuple[bool, str]:
        session, run = context.require("session", "run")
        unfinished = store.nodes_in_nonterminal_state(session, run)
        if unfinished:
            ids = [f"{n.node_id} ({n.status})" for n in unfinished]
            return False, f"{len(ids)} nodes unfinished: {_listed(ids)}"
        return True, "every node reached a terminal state"

    @reg.register("lineage_complete", "every artifact is attributable to a producing attempt")
    def lineage_complete(context: PredicateContext) -> tuple[bool, str]:
        session, run = context.require("session", "run")
        orphans = query.unproduced_artifacts(session, run)
        if orphans:
            names = [a.ref for a in orphans]
            return False, f"{len(names)} artifacts have no producer: {_listed(names)}"
        return True, "every artifact traces to the attempt that produced it"

    @reg.register("all_upstream_gates_green", "no upstream node failed or errored")
    def all_upstream_gates_green(context: PredicateContext) -> tuple[bool, str]:
        session, run = context.require("session", "run")
        broken = [
            f"{n.node_id} ({n.status})"
            for n in store.all_nodes(session, run)
            if n.status in (NodeStatus.FAILED, NodeStatus.ERRORED, NodeStatus.STALE)
        ]
        if broken:
            return False, f"{len(broken)} nodes not green: {_listed(broken)}"
        return True, "every node upstream is green"

    # ------------------------------------------------------------------ #
    # documentation — evidence predicates
    # ------------------------------------------------------------------ #

    @reg.register(
        "setup_steps_execute_in_clean_venv",
        "the documented setup was actually run, and worked",
    )
    def setup_steps_execute_in_clean_venv(context: PredicateContext) -> tuple[bool, str]:
        """Confirms evidence exists; producing it is a tool node's job.

        A documentation gate that checks for headings is vacuous, and one that
        trusts the doc's author to say it works is worse. This insists on a
        recorded exit code from an actual run.
        """
        fact = context.facts.get("setup.exit_code")
        if fact is None:
            return False, (
                "no recorded result from executing the setup steps — the docs node "
                "must run them in a clean environment before this gate can hold"
            )
        if fact.value != 0:
            return False, f"documented setup steps failed with exit code {fact.value}"
        return True, "documented setup steps executed successfully"

    @reg.register(
        "documented_endpoints_match_openapi", "the docs describe the API that exists"
    )
    def documented_endpoints_match_openapi(context: PredicateContext) -> tuple[bool, str]:
        design = _load(context, "design.design", Design)
        session, run, artifacts = context.require("session", "run", "artifacts")

        readme = recorder.latest(session, run, "docs.readme")
        if readme is None:
            return False, "no documentation artifact has been recorded"

        prose = artifacts.read(readme)
        documented = set(re.findall(r"(?:GET|POST|PUT|PATCH|DELETE)\s+(/\S*)", prose))
        expected = set(design.endpoints)

        missing = sorted(expected - documented)
        invented = sorted(documented - expected)
        if missing or invented:
            parts = []
            if missing:
                parts.append(f"undocumented: {_listed(missing)}")
            if invented:
                parts.append(f"documented but not in the contract: {_listed(invented)}")
            return False, "; ".join(parts)
        return True, f"all {len(expected)} endpoints documented"

    return reg


def load_json_artifact(content: str) -> dict:
    """Parse an artifact body, failing loudly rather than returning a default."""
    return json.loads(content)
