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
from pathlib import Path

import networkx as nx
from pydantic import ValidationError

from orchestrator.artifacts import (
    SCHEMAS,
    AcceptanceSuite,
    Baseline,
    CodeMap,
    Design,
    ImpactAnalysis,
    RequirementRegister,
    SecurityReport,
    Severity,
)
from orchestrator.engine.loader import runtime_graph
from orchestrator.gates.registry import PredicateContext, PredicateRegistry
from orchestrator.gates.registry import registry as default_registry
from orchestrator.lineage import query, recorder
from orchestrator.state import store
from orchestrator.state.models import NodeStatus

ENDPOINT = re.compile(r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/\S*")


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
        """Validates the node's primary output against the schema it declares.

        The primary output is the first one listed, because the others are
        projections of it — `design` declares `[spec, modules]` and the second is
        a field of the first. Reading the schema from the node rather than
        hardcoding one per node is what lets a scenario plan add an agent node
        without also adding a predicate.
        """
        node = context.require("node")
        if not node.output_schema:
            return True, "no schema declared for this node"

        name = Path(node.output_schema).stem
        model = SCHEMAS.get(name)
        if model is None:
            # Unknown schema, so the check cannot be performed. ERROR, never green.
            raise LookupError(
                f"node '{node.id}' declares schema '{node.output_schema}', which is "
                f"not a known contract; known: {', '.join(sorted(SCHEMAS))}"
            )

        output = node.outputs[0] if node.outputs else "artifact"
        try:
            _load(context, f"{node.id}.{output}", model)
        except ValidationError as exc:
            return False, (
                f"'{node.id}.{output}' does not match {name}: {exc.error_count()} errors"
            )
        return True, f"'{node.id}.{output}' matches {name}"

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
        "requirement_is_ambiguous",
        "intake found at least one thing the requirement does not say",
    )
    def requirement_is_ambiguous(context: PredicateContext) -> tuple[bool, str]:
        """The ambiguous scenario's own gate: an intake that found nothing has not read it.

        A predicate rather than `ambiguities.total > 0`, because nothing produces
        that fact — and nothing should. The count is a property of the artifact,
        and the only thing that could report it as a fact is the agent that wrote
        it, whose word about its own output is inadmissible (D4). The artifact is
        the subject of the check, never its author.
        """
        register = _load(context, "intake.register", RequirementRegister)
        if not register.ambiguities:
            return False, (
                "intake surfaced no ambiguities from a requirement chosen for being "
                "vague — the work here is noticing what it does not say"
            )
        return True, f"{len(register.ambiguities)} ambiguities surfaced"

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
        "every_ambiguity_is_disposed_or_escalated",
        "triage disposed of everything below the threshold and left the rest for a person",
    )
    def every_ambiguity_is_disposed_or_escalated(
        context: PredicateContext,
    ) -> tuple[bool, str]:
        """The exit gate for a node whose job is to escalate.

        `no_ambiguity_without_disposition` cannot be that gate: an ambiguity
        above the threshold is *supposed* to stay open until a human answers, so
        the gate fails exactly when the escalation path is working. The node then
        burns its retry budget re-running a pure function, and the failure escape
        hatch fires instead of the `on_escalate` target the plan named.

        What this asserts is that the policy was applied: everything below the
        threshold carries a recorded assumption, and everything at or above it is
        left for a person. Whether those questions were answered is a release
        question — `no_ambiguity_without_disposition` holds that line at G10.
        """
        register = _load(context, "intake.register", RequirementRegister)
        node = context.node
        threshold = Severity(
            str((node.params.get("threshold") if node else None) or Severity.HIGH).lower()
        )

        skipped = [
            a.id
            for a in register.ambiguities
            if not a.is_disposed and a.severity.rank < threshold.rank
        ]
        if skipped:
            return False, (
                f"{len(skipped)} ambiguities below the '{threshold}' threshold were "
                f"left undisposed — the policy skipped them: {_listed(skipped)}"
            )

        open_items = [a.id for a in register.ambiguities if not a.is_disposed]
        if open_items:
            return True, (
                f"{len(open_items)} ambiguities at or above '{threshold}' await a person "
                f"({_listed(open_items)}); the rest carry recorded assumptions"
            )
        return True, f"all {len(register.ambiguities)} ambiguities disposed by policy"

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
    # the API contract
    # ------------------------------------------------------------------ #

    @reg.register(
        "contract_is_valid", "the API contract the design declares is well-formed"
    )
    def contract_is_valid(context: PredicateContext) -> tuple[bool, str]:
        """A predicate, not an expression, because nothing can produce the fact.

        `openapi.valid == true` was in this gate for a while and could never
        hold: the design node is an agent, and a fact an agent produced about its
        own output is inadmissible (D4). The contract exists only as the
        artifact, so the check has to read the artifact — which is a predicate's
        job. Everything downstream depends on it: the documentation gate compares
        the README against these endpoints, and an endpoint nobody can parse
        matches nothing.
        """
        design = _load(context, "design.spec", Design)
        if not design.endpoints:
            return False, "the design declares no endpoints — there is no contract to keep"

        malformed = [e for e in design.endpoints if not ENDPOINT.fullmatch(e.strip())]
        if malformed:
            return False, (
                f"{len(malformed)} endpoints are not '<METHOD> /path': {_listed(malformed)}"
            )

        seen: set[str] = set()
        duplicates = sorted({e for e in design.endpoints if e in seen or seen.add(e)})
        if duplicates:
            return False, f"{len(duplicates)} endpoints declared twice: {_listed(duplicates)}"

        unbalanced = [e for e in design.endpoints if e.count("{") != e.count("}")]
        if unbalanced:
            return False, f"unbalanced path parameters: {_listed(unbalanced)}"

        return True, f"{len(design.endpoints)} endpoints, all well-formed and distinct"

    # ------------------------------------------------------------------ #
    # the module contract — what makes parallel implementation safe (D24)
    # ------------------------------------------------------------------ #

    @reg.register(
        "every_module_has_an_interface",
        "every module the implementation fans out over declares what it exports",
    )
    def every_module_has_an_interface(context: PredicateContext) -> tuple[bool, str]:
        """A module fanned out with no contract is an author inventing names.

        This is the check that would have caught the failure directly: `errors`
        had no declared exports, so `links` invented three of them and the
        mismatch surfaced two nodes later as an ImportError attributed to nobody.
        """
        design = _load(context, "design.spec", Design)
        if not design.modules:
            return False, "the design declares no modules — there is nothing to implement"

        interfaces = design.interface_for
        missing = sorted(m.name for m in design.modules if m.name not in interfaces)
        if missing:
            return False, (
                f"{len(missing)} modules have no interface: {_listed(missing)} — "
                f"their callers would have to invent the names"
            )

        silent = sorted(
            m.name for m in design.modules if not interfaces[m.name].exports
        )
        if silent:
            return False, (
                f"{len(silent)} modules export nothing: {_listed(silent)} — "
                f"a module nobody can call is not a module"
            )

        exports = sum(len(i.exports) for i in design.interfaces)
        return True, f"{len(design.modules)} modules declare {exports} exports between them"

    @reg.register(
        "module_dependencies_are_acyclic",
        "the declared module dependency graph is a DAG, and names only real modules",
    )
    def module_dependencies_are_acyclic(context: PredicateContext) -> tuple[bool, str]:
        """Parallel implementation is only safe over a DAG.

        Two modules that must change together are one module. Shipping them as
        separate parallel work means neither can be written against a contract
        that is settled, which is the condition this whole design depends on.
        """
        design = _load(context, "design.spec", Design)
        known = {module.name for module in design.modules}

        dangling = sorted(
            f"{interface.module} -> {dependency}"
            for interface in design.interfaces
            for dependency in interface.depends_on
            if dependency not in known
        )
        if dangling:
            return False, f"dependencies on modules that do not exist: {_listed(dangling)}"

        cycles = design.dependency_cycles()
        if cycles:
            trails = [" -> ".join([*cycle, cycle[0]]) for cycle in cycles]
            return False, f"{len(cycles)} dependency cycles: {_listed(trails)}"

        edges = sum(len(i.depends_on) for i in design.interfaces)
        return True, f"{edges} module dependencies, acyclic"

    # ------------------------------------------------------------------ #
    # traceability
    # ------------------------------------------------------------------ #

    @reg.register(
        "requirement_design_matrix_complete",
        "every requirement is satisfied by at least one design element",
    )
    def requirement_design_matrix_complete(context: PredicateContext) -> tuple[bool, str]:
        register = _load(context, "intake.register", RequirementRegister)
        design = _load(context, "design.spec", Design)

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
        design = _load(context, "design.spec", Design)

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
    # brownfield — prior state exists, so it can be broken
    # ------------------------------------------------------------------ #

    @reg.register("baseline_is_green", "the target's suite passed before anything changed")
    def baseline_is_green(context: PredicateContext) -> tuple[bool, str]:
        """A red baseline is a stop, not a caveat.

        Proceeding from one means no later failure can be attributed to this
        run's change — which disables every regression control downstream while
        leaving them looking enforced.
        """
        baseline = _load(context, "baseline.snapshot", Baseline)
        if not baseline.green:
            return False, (
                f"the target's suite was already failing before this run: "
                f"{len(baseline.failing)} tests red ({_listed(baseline.failing)}). "
                f"Nothing downstream can distinguish a regression from an "
                f"inherited failure until this is resolved."
            )
        return True, f"baseline green at {baseline.snapshot_ref} ({len(baseline.files)} files)"

    @reg.register(
        "every_referenced_symbol_exists",
        "the impact analysis names code that is actually there",
    )
    def every_referenced_symbol_exists(context: PredicateContext) -> tuple[bool, str]:
        """The anti-hallucination check, and the reason the code map is derived.

        An impact analysis is prose an agent produced; the map is parsed from the
        tree by a different producer. Checking one against the other is D4 applied
        to the one artifact most able to be confidently wrong.
        """
        analysis = _load(context, "impact-analysis.report", ImpactAnalysis)
        code_map = _load(context, "codebase.map", CodeMap)

        known = code_map.symbol_refs
        invented = [ref for ref in analysis.referenced_symbols if ref not in known]
        if invented:
            return False, (
                f"{len(invented)} referenced symbols do not exist in the target: "
                f"{_listed(invented)}"
            )

        missing_modules = [m.path for m in analysis.affected_modules
                           if not any(path.startswith(f"{code_map.root}/{m.path}")
                                      or m.path in path for path in code_map.files)]
        if missing_modules:
            return False, f"affected modules not present: {_listed(missing_modules)}"

        return True, (
            f"all {len(analysis.referenced_symbols)} referenced symbols and "
            f"{len(analysis.affected_modules)} modules exist"
        )

    @reg.register(
        "has_breaking_contract_change",
        "the change alters the published interface (D13 escalation)",
    )
    def has_breaking_contract_change(context: PredicateContext) -> tuple[bool, str]:
        analysis = _load(context, "impact-analysis.report", ImpactAnalysis)
        diff = analysis.contract_diff
        if diff.breaking:
            return True, (
                f"breaking contract change — removed: {_listed(diff.removed) or 'none'}; "
                f"changed: {_listed(diff.changed) or 'none'}"
            )
        return False, "no breaking contract change declared"

    @reg.register(
        "no_pre_existing_test_regressed",
        "no test that passed before this run fails now",
    )
    def no_pre_existing_test_regressed(context: PredicateContext) -> tuple[bool, str]:
        """Reads the comparison; does not perform it.

        The set difference is a tool node's job, so the evidence carries a
        recorded result rather than a check that re-ran itself at gate time and
        agreed with itself.
        """
        recorded = context.facts.get("regression.new_failures")
        if recorded is None:
            raise LookupError(
                "no regression comparison was recorded — the verification node "
                "must compare against the baseline before this gate can hold"
            )
        if recorded.value:
            ids = context.facts.get("regression.ids")
            listed = _listed(list(ids.value)) if ids else "(ids not recorded)"
            return False, f"{recorded.value} tests regressed: {listed}"

        inherited = context.facts.get("regression.inherited")
        note = f", {inherited.value} still red from before" if inherited else ""
        return True, f"no test regressed{note}"

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
        """Nothing this gate depends on is still in flight.

        Deliberately not "no node anywhere is unfinished", which cannot hold when
        the node asking is itself running and the checkpoint that follows it is
        pending by definition. Written that way the check was unsatisfiable, and
        being the last gate in the plan, nothing ever reached it to find out.

        So the question is about *upstream*: everything this node waits on, and
        everything they waited on, must have finished. What comes after has not
        started, which is not the same as unfinished.
        """
        session, run = context.require("session", "run")
        unfinished = store.nodes_in_nonterminal_state(session, run)

        node, plan = context.node, context.plan
        if node is not None and plan is not None:
            inserted = {
                row.node_id: list(row.extra_needs or ())
                for row in store.all_nodes(session, run)
                if row.extra_needs
            }
            graph = runtime_graph(plan, inserted)
            upstream = nx.ancestors(graph, node.id) if node.id in graph else set()
            unfinished = [row for row in unfinished if row.node_id in upstream]

        if unfinished:
            ids = [f"{n.node_id} ({n.status})" for n in unfinished]
            return False, f"{len(ids)} nodes unfinished: {_listed(ids)}"
        return True, "everything this gate depends on reached a terminal state"

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
            detail = context.facts.get("setup.detail")
            because = f" — {detail.value}" if detail is not None else ""
            return False, (
                f"documented setup steps failed with exit code {fact.value}{because}"
            )
        return True, "documented setup steps executed successfully"

    @reg.register(
        "documented_endpoints_match_openapi", "the docs describe the API that exists"
    )
    def documented_endpoints_match_openapi(context: PredicateContext) -> tuple[bool, str]:
        design = _load(context, "design.spec", Design)
        session, run, artifacts = context.require("session", "run", "artifacts")

        readme = recorder.latest(session, run, "docs.readme")
        if readme is None:
            return False, "no documentation artifact has been recorded"

        prose = artifacts.read(readme)
        # Method *and* path. Capturing the path alone compared `/api/links`
        # against the contract's `POST /api/links`, so the two sets could never
        # intersect: every endpoint reported as both undocumented and invented,
        # and no README could have satisfied this check. Trailing markdown —
        # a backtick closing an inline span — is not part of a path.
        documented = {
            f"{method} {path.rstrip('`.,;:)')}"
            for method, path in re.findall(
                r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)", prose
            )
        }
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
