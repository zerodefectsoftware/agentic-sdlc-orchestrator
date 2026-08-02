"""The scheduler: the control plane's loop.

Executes a plan graph in **waves**. Each pass collects every node whose
dependencies are satisfied and runs them together; the next wave cannot start
until the whole current one has been recorded. That is where "parallel paths
with synchronization" comes from — the join is not a node (there is no `join`
kind), it is the wave boundary, and `needs` edges are what put a node in a later
wave.

Workers run concurrently in threads because they block on subprocesses and HTTP.
**State is recorded on one thread.** Dispatch is the expensive, parallel part;
bookkeeping is serial, so the session is never shared across threads and the
recorded order of a run is deterministic even when execution was not.

The loop itself is small. Almost every decision it makes is delegated:

- *what work is acceptable* → `gates`
- *what to do about a verdict* → `policy`
- *what happened* → `state` and `lineage`

That is deliberate. A scheduler that also decided policy would be the one place
where all the governance semantics could quietly drift apart.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
from sqlalchemy.orm import Session

from orchestrator.config import get_settings
from orchestrator.engine.loader import dependency_graph
from orchestrator.engine.plan import ExpressionCheck, Gate, GateCheck, Node, NodeKind, Plan
from orchestrator.evidence.render import render_json
from orchestrator.gates import CheckResult, GateResult, Verdict, evaluate_gate, expressions
from orchestrator.gates.facts import FactSet
from orchestrator.gates.registry import PredicateContext, PredicateRegistry
from orchestrator.gates.registry import registry as default_registry
from orchestrator.lineage import recorder
from orchestrator.policy.failure import Action, escalation_node, respond_to
from orchestrator.state import store
from orchestrator.state.artifacts import ArtifactStore
from orchestrator.state.models import Artifact, Decision, NodeStatus, Run, RunStatus
from orchestrator.workers.base import Worker, WorkerError, WorkerResult, WorkScope

TERMINAL = (NodeStatus.PASSED, NodeStatus.SKIPPED)
VERIFY_SCOPE = WorkScope()  # a check verifies; it does not write


class SchedulerError(Exception):
    """The run cannot proceed for a structural reason, not a gate verdict."""


@dataclass(frozen=True, slots=True)
class _Produced:
    """The shape `_store` expects, for artifacts the engine itself produces."""

    name: str
    content: str
    path: str | None = None


@dataclass(slots=True)
class Outcome:
    """What one node execution produced, before any of it is recorded."""

    node: Node
    result: WorkerResult | None = None
    error: str | None = None
    facts: FactSet = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.facts is None:
            self.facts = dict(self.result.facts) if self.result else {}


class Scheduler:
    def __init__(
        self,
        plan: Plan,
        worker: Worker,
        *,
        registry: PredicateRegistry | None = None,
        artifacts: ArtifactStore | None = None,
        max_workers: int | None = None,
    ) -> None:
        self.plan = plan
        self.worker = worker
        self.registry = registry if registry is not None else default_registry
        self.artifacts = artifacts if artifacts is not None else ArtifactStore()
        self.max_workers = max_workers if max_workers is not None else get_settings().max_workers
        self._graph = dependency_graph(plan)
        self._runtime: dict[str, Node] = {}       # nodes materialised during the run
        self._extra_needs: dict[str, list[str]] = {}  # edges added at runtime
        self._materialised: set[str] = set()          # fanouts already expanded

    # ----------------------------------------------------------------- #
    # lifecycle
    # ----------------------------------------------------------------- #

    def start(
        self, session: Session, *, requirement_path: str, target_profile: str
    ) -> Run:
        """Create a run and materialise the plan's shape.

        Optional nodes start SKIPPED rather than PENDING: `clarify-with-human`
        has no dependencies, so a PENDING optional node would be "ready"
        immediately and every run would stop to ask a question nobody raised.
        Escalation activates them.
        """
        run = store.start_run(
            session,
            plan_name=self.plan.name,
            plan_version=self.plan.version,
            requirement_path=requirement_path,
            target_profile=target_profile,
            nodes=[(n.id, str(n.kind), str(n.stage)) for n in self.plan.nodes],
        )
        for node in self.plan.nodes:
            if node.optional:
                store.get_node(session, run, node.id).status = NodeStatus.SKIPPED
        session.flush()
        return run

    def advance(self, session: Session, run: Run) -> Run:
        """Run until the graph blocks, finishes, or stops.

        Returns rather than waiting. A blocked run persists and the process can
        exit; `orchestrator approve` resumes it (§6).
        """
        while run.status is RunStatus.RUNNING:
            wave = self._ready(session, run)
            if not wave:
                break
            # Inputs are resolved on the scheduler's thread; workers run on others.
            material = {node.id: self._inputs(session, run, node) for node in wave}

            # Say what is executing, and commit it, before anything long starts.
            # Until this existed the whole run was one transaction: no status, no
            # attempt and no gate record was visible until the process exited, so
            # a forty-minute run was a black box and every diagnosis had to wait
            # for it to end. A wave is the natural commit boundary — it is
            # already the unit the loop records.
            for node in wave:
                execution = store.get_node(session, run, node.id)
                if execution is not None and node.kind not in (
                    NodeKind.HUMAN,
                    NodeKind.FANOUT,
                ):
                    execution.status = NodeStatus.RUNNING
            session.commit()

            # Every outcome of the wave is recorded, including the ones that
            # arrive after something blocks the run. `_execute` has already run
            # the whole wave — the sessions happened, the files are on disk —
            # so stopping here would leave work in the target with no attempt,
            # no changeset and no gate verdict behind it. Five implementers were
            # lost that way: real code, no lineage. The `while` condition is
            # what stops the *next* wave; this loop only writes down what
            # already happened.
            for outcome in self._execute(wave, material):
                self._record(session, run, outcome)
            session.commit()   # the wave is history now; make it readable

        return self._settle(session, run)

    # ----------------------------------------------------------------- #
    # readiness and dispatch
    # ----------------------------------------------------------------- #

    def _ready(self, session: Session, run: Run) -> list[Node]:
        """Every node that can run now — the wave.

        STALE counts as re-enterable. It means "your result was computed from
        something that has since been withdrawn", which is a statement about the
        recorded result, not a terminal state — the work has to happen again.
        Collecting only PENDING left an invalidated node with no way back into
        the graph: it never ran, and `_settle` then failed the run as stuck. The
        cascade existed and the re-entry did not, so re-planning was a one-way
        door onto a wedged run.
        """
        ready: list[Node] = []
        for execution in store.all_nodes(session, run):
            if execution.status not in (NodeStatus.PENDING, NodeStatus.STALE):
                continue
            node = self._node(execution.node_id)
            if node is None:
                continue
            needs = [*node.needs, *self._extra_needs.get(node.id, ())]
            if all(self._is_satisfied(session, run, dep) for dep in needs):
                ready.append(node)
        return ready

    def _is_satisfied(self, session: Session, run: Run, node_id: str) -> bool:
        execution = store.get_node(session, run, node_id)
        return execution is not None and execution.status in TERMINAL

    def _inputs(self, session: Session, run: Run, node: Node) -> dict[str, str]:
        """The material a node works from: the requirement, plus upstream artifacts.

        A live agent given nothing has nothing to reason about — the scheduler
        passed an empty dict until the agent worker existed to notice.
        """
        material: dict[str, str] = {}

        requirement = Path(run.requirement_path)
        if requirement.exists():
            material["requirement"] = requirement.read_text()

        for dependency in [*node.needs, *node.inputs]:
            upstream = self._node(dependency.split(".", 1)[0])
            if upstream is None:
                continue
            for output in upstream.outputs:
                artifact = recorder.latest(session, run, f"{upstream.id}.{output}")
                if artifact is not None:
                    material[artifact.name] = self.artifacts.read(artifact)

            if upstream.kind is NodeKind.HUMAN:
                decision = _decision_text(session, run, upstream.id)
                if decision:
                    material[f"{upstream.id}.decision"] = decision
        return material

    def _execute(self, wave: list[Node], material: dict[str, dict[str, str]]) -> list[Outcome]:
        """Dispatch a wave concurrently; return outcomes in wave order.

        Order is stabilised deliberately: a run's recorded history should not
        depend on which thread finished first.
        """
        if len(wave) == 1:
            return [self._execute_one(wave[0], material.get(wave[0].id, {}))]

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(lambda n: self._execute_one(n, material.get(n.id, {})), wave))

    def _execute_one(self, node: Node, inputs: dict[str, str]) -> Outcome:
        if node.kind in (NodeKind.HUMAN, NodeKind.FANOUT):
            # Neither does work: one blocks on a person, the other shapes the graph.
            return Outcome(node=node)

        try:
            result = self.worker.run(node, inputs, WorkScope.for_node(node))
        except WorkerError as exc:
            # The work could not be performed. No facts, so the gate will ERROR —
            # which is the correct verdict, and not something to pre-empt here.
            return Outcome(node=node, error=str(exc))

        try:
            observed = self._verify(node, inputs)
        except WorkerError as exc:
            # The check could not be performed. Also ERROR: an unrunnable check
            # must never resolve to a pass, and it is not a failed gate either.
            return Outcome(node=node, result=result, error=str(exc))

        return Outcome(node=node, result=result, facts={**result.facts, **observed})

    def _verify(self, node: Node, inputs: dict[str, str]) -> FactSet:
        """Run the node's declared checks and collect what they observed.

        This is D4 made operational. A gate expression like `ruff.exit_code == 0`
        needs a fact only a tool can produce, and the node that wrote the code is
        not that tool — an agent reporting its own lint result is an assertion.
        So the plan names the checks, the engine runs them after the work, and
        their facts are what the gate reads. Verified facts win over the node's
        own on collision, for the same reason.

        The checks go through the same worker as everything else, so a replayed
        or stubbed run stays hermetic instead of shelling out.
        """
        observed: FactSet = {}
        for probe in verify_probes(node):
            observed.update(self.worker.run(probe, inputs, VERIFY_SCOPE).facts)
        return observed

    # ----------------------------------------------------------------- #
    # recording
    # ----------------------------------------------------------------- #

    def _record(self, session: Session, run: Run, outcome: Outcome) -> None:
        node = outcome.node
        execution = store.get_node(session, run, node.id)

        if node.kind is NodeKind.HUMAN:
            self._block_for_human(session, run, node, execution)
            return

        if node.kind is NodeKind.FANOUT:
            self._expand(session, run, node, execution)
            return

        if node.emits == "evidence_bundle":
            self._emit_evidence(session, run, node, execution)
            return

        attempt = store.begin_attempt(
            session,
            execution,
            worker=self.worker.name,
            model=node.model,
            effort=str(node.effort) if node.effort else None,
        )

        produced: list[Artifact] = []
        if outcome.result:
            produced = [self._store(session, run, attempt, a) for a in outcome.result.artifacts]

        verdict, gate_result = self._evaluate(session, run, node, outcome)
        if gate_result is not None:
            store.record_gate(
                session,
                attempt,
                verdict=str(gate_result.verdict),
                evaluator=gate_result.evaluator,
                checks=[
                    {
                        "check": check.check,
                        "verdict": str(check.verdict),
                        "detail": check.detail,
                        "observed": check.observed,
                    }
                    for check in gate_result.checks
                ],
            )

        store.finish_attempt(
            session,
            attempt,
            status=_status_for(verdict),
            error=outcome.error,
        )

        if produced:
            self._invalidate_downstream(session, run, node)

        self._apply_policy(
            session, run, node, execution, verdict, attempt.number, outcome.facts
        )

    def _store(self, session: Session, run: Run, attempt, produced) -> Artifact:
        """Record an artifact's identity, and write its body where it can be read."""
        artifact = recorder.record_artifact(
            session, run, name=produced.name, content=produced.content, produced_by=attempt
        )
        path = self.artifacts.write(run.id, artifact.name, artifact.version, produced.content)
        artifact.path = str(path)
        session.flush()
        return artifact

    # ----------------------------------------------------------------- #
    # fanout
    # ----------------------------------------------------------------- #

    def _expand(self, session: Session, run: Run, node: Node, execution) -> None:
        """Materialise a fanout's children from an upstream artifact (§5.1).

        Runs in two passes. The first reads the source artifact, creates the
        children, and makes the fanout node wait for them. The second — once the
        children have passed — marks the fanout done.

        Waiting matters: downstream nodes say `needs: [impl]`, and if the fanout
        completed the moment it materialised, verification would start before a
        single module had been written.

        **A fanout's children are identified by its source artifact, never by
        history.** Resuming a run treated "this fanout has recorded children" as
        "this fanout is done" — so a run whose design had since been re-derived
        found seven children from the previous decomposition, every one of them
        SKIPPED, and passed on the spot. Nothing was implemented, `impl` recorded
        PASSED with no attempts, and verification ran against a tree of stubs.

        Two rules stop that, and both are evaluated every time rather than once:

        - the expected children come from the source artifact, so a changed
          decomposition re-materialises and obsolete edges are dropped;
        - **the fanout passes only when every expected child has PASSED.**
          Anything else — skipped, errored, never created — is work still to do,
          so that child is put back to PENDING and the fanout waits again.

        The status a child already carries is never taken as an answer. A name
        that survives a re-decomposition keeps the status of the run that
        abandoned it, and six of eight modules stayed SKIPPED from a previous
        shape that way: two implementers ran, six never did, and the fanout
        counted all eight as its children.
        """
        expected = self._children_of(session, run, node)
        expected_ids = [child.id for child in expected]

        outstanding: list[str] = []
        for child in expected:
            self._runtime[child.id] = child
            row = store.get_node(session, run, child.id)
            if row is None:
                store.insert_node(
                    session, run, child.id, str(child.kind), str(child.stage), _config(child)
                )
                outstanding.append(child.id)
            elif row.status is not NodeStatus.PASSED:
                # Refresh the stored definition, not just the status. A child is
                # dispatched from its persisted config when a previous process
                # created it, so a child re-entered directly — by an escalation
                # approval, say — runs the template as it was *then*. Two
                # implementers failed twice on a param the template had already
                # been given, because nothing rewrote what they were dispatched
                # from.
                row.config = _config(child)
                row.status = NodeStatus.PENDING
                outstanding.append(child.id)

        # Assigned, not appended: re-materialising after a changed decomposition
        # must drop the children that no longer exist, or the fanout waits on
        # work nobody will do.
        self._extra_needs[node.id] = expected_ids
        self._persist_edges(session, run, node.id)
        self._materialised.add(node.id)

        if not outstanding:
            execution.status = NodeStatus.PASSED
            session.flush()
            return
        execution.status = NodeStatus.PENDING  # re-enters once its children pass
        session.flush()

    def _children_of(self, session: Session, run: Run, node: Node) -> list[Node]:
        source = recorder.latest(session, run, _artifact_name(node.from_ or ""))
        if source is None:
            raise SchedulerError(
                f"fanout '{node.id}' reads '{node.from_}', which no node has produced"
            )

        items = parse_fanout_items(self.artifacts.read(source), source.ref)
        template = node.template
        return [
            Node(
                id=f"{node.id}:{item.get('name', index)}",
                kind=template.kind,
                stage=node.stage,           # the children are the same phase of work
                role=template.role,
                write_scope=[_substitute(scope, item) for scope in template.write_scope],
                verify=[_substitute(check, item) for check in template.verify],
                # Children have no `needs`, so without this an implementer is
                # handed the raw requirement and nothing else — not the design
                # it is implementing, nor the tests it has to satisfy.
                inputs=list(template.inputs),
                # A child session needs its own turn and time limits. Without
                # these the worker falls back to a default tuned for nothing in
                # particular, and a per-module budget cannot be set at all.
                # Passed through as authored: the item's own fields belong in
                # `write_scope` and `verify`, where `{item.*}` is substituted.
                params={
                    key: _substitute(value, item) if isinstance(value, str) else value
                    for key, value in template.params.items()
                },
                freeze_paths=list(template.freeze_paths),
                gate=template.gate,
                model=template.model,
                effort=template.effort,
                # Without this a child's budget is None, which reads as zero:
                # any gate failure escalates to a human immediately instead of
                # retrying, on work that is often retryable.
                retry_budget=template.retry_budget or self.plan.defaults.retry_budget,
                autonomy=template.autonomy,
            )
            for index, item in enumerate(items)
        ]

    def _emit_evidence(self, session: Session, run: Run, node: Node, execution) -> None:
        """Assemble the bundle, then gate on it.

        Not a worker task: assembly reads run state, lineage, gate records, and
        artifacts, and handing a worker thread the session to do that would move
        database access off the one thread that owns it for no gain.
        """
        from orchestrator.evidence import assemble

        attempt = store.begin_attempt(session, execution, worker="engine")
        bundle = assemble(session, run)

        verdict, gate_result = self._evaluate(session, run, node, Outcome(node=node))
        if gate_result is not None:
            store.record_gate(
                session,
                attempt,
                verdict=str(gate_result.verdict),
                evaluator=gate_result.evaluator,
                checks=[
                    {
                        "check": check.check,
                        "verdict": str(check.verdict),
                        "detail": check.detail,
                        "observed": check.observed,
                    }
                    for check in gate_result.checks
                ],
            )

        if verdict is Verdict.PASS:
            self._store(
                session,
                run,
                attempt,
                _Produced(f"{node.id}.evidence", render_json(bundle)),
            )

        store.finish_attempt(session, attempt, status=_status_for(verdict))
        self._apply_policy(session, run, node, execution, verdict, attempt.number, {})

    def _evaluate(
        self, session: Session, run: Run, node: Node, outcome: Outcome
    ) -> tuple[Verdict, GateResult | None]:
        """A node without a gate is accepted on completion; with one, the gate decides."""
        if node.gate is None:
            return (Verdict.ERROR if outcome.error else Verdict.PASS), None

        result = evaluate_gate(
            node.gate,
            outcome.facts,
            registry=self.registry,
            context=self._context(session, run, node),
        )
        return result.verdict, result

    def _context(self, session: Session, run: Run, node: Node) -> PredicateContext:
        """What predicates are allowed to look at.

        Without this, every predicate that reads run state — stale approvals,
        traceability matrices, lineage completeness — raises and the gate ERRORs.
        The unit tests never saw it because they call predicates directly.
        """
        return PredicateContext(session=session, run=run, artifacts=self.artifacts, node=node)

    # ----------------------------------------------------------------- #
    # consequences
    # ----------------------------------------------------------------- #

    def _apply_policy(
        self,
        session: Session,
        run: Run,
        node: Node,
        execution,
        verdict: Verdict,
        attempt_number: int,
        facts: FactSet,
    ) -> None:
        response = respond_to(verdict, node, attempt=attempt_number)

        match response.action:
            case Action.PROCEED:
                self._check_escalation_condition(session, run, node, facts)
            case Action.RETRY:
                execution.status = NodeStatus.PENDING
            case Action.INSERT_FIX:
                self._insert_repair(session, run, node, execution)
            case Action.ESCALATE:
                self._insert_escalation(session, run, node, response, attempt_number, verdict)
            case Action.SAFE_STOP:
                store.finish_run(session, run, status=RunStatus.STOPPED,
                                 stop_reason=response.reason)
            case Action.ROLLBACK:
                store.finish_run(session, run, status=RunStatus.ROLLED_BACK,
                                 stop_reason=response.reason)
        session.flush()

    def _check_escalation_condition(
        self, session: Session, run: Run, node: Node, facts: FactSet
    ) -> None:
        """`escalate_when` on a *passing* node — the ambiguity path.

        Distinct from failure escalation: this activates a node the plan already
        declared (`on_escalate`) rather than inserting a new one.

        An escalation condition that cannot be evaluated escalates anyway. The
        alternative is deciding on the system's behalf that no human is needed,
        on the strength of a check that did not run — which is the failure mode
        the ERROR verdict exists to prevent.
        """
        if node.escalate_when is None or node.on_escalate is None:
            return

        condition = Gate(all_checks=[node.escalate_when])
        verdict = evaluate_gate(
            condition, facts, registry=self.registry, context=self._context(session, run, node)
        ).verdict
        if verdict is Verdict.FAIL:
            return

        self._activate(session, run, node.on_escalate)

    def _activate(self, session: Session, run: Run, node_id: str) -> None:
        """Wake an optional node, and the optional work that follows it.

        Transitive on purpose. A checkpoint usually exists to be *processed* —
        `clarify-with-human` is followed by the node that folds the answer back
        into the register — and activating only the checkpoint leaves that node
        skipped forever. The result is a run that stops, gets an answer, and
        then behaves as though nobody had answered.

        Only SKIPPED optional nodes are touched: everything else is already in
        the graph's ordinary flow, and forcing a status on it would be the
        scheduler overruling the plan.
        """
        for candidate in [node_id, *nx.descendants(self._graph, node_id)]:
            node = self._node(candidate)
            if node is None or not node.optional:
                continue
            execution = store.get_node(session, run, candidate)
            if execution is not None and execution.status is NodeStatus.SKIPPED:
                execution.status = NodeStatus.PENDING
        session.flush()

    def _insert_repair(self, session: Session, run: Run, node: Node, execution) -> None:
        """Insert a fix node, then re-enter the failed node (§6)."""
        repair = node.on_fail
        fix_id = f"{repair.insert}:{node.id}"

        if store.get_node(session, run, fix_id) is None:
            fix = Node(
                id=fix_id,
                kind=NodeKind.CODEAGENT,
                stage=node.stage,
                role="fixer",
                needs=[],
                # D6: the repair may change the code, never the tests that judge it.
                write_scope=list(node.write_scope) or ["target/shortener/**"],
                freeze_paths=list(node.freeze_paths),
            )
            self._runtime[fix_id] = fix
            store.insert_node(
                session, run, fix_id, str(NodeKind.CODEAGENT), str(node.stage), _config(fix)
            )

        # The failed node re-enters only after the fix has run. Without this edge
        # both are PENDING with no dependency between them, so the wave would
        # re-run the failed node alongside its own repair.
        if fix_id not in self._extra_needs.setdefault(node.id, []):
            self._extra_needs[node.id].append(fix_id)
        self._persist_edges(session, run, node.id)

        execution.status = NodeStatus.PENDING
        session.flush()

    def _insert_escalation(
        self,
        session: Session,
        run: Run,
        node: Node,
        response,
        attempt_number: int,
        verdict: Verdict,
    ) -> None:
        """Hand off to a human as a node, not a status (§6)."""
        escalation = escalation_node(
            node, response, attempt=attempt_number, verdict=verdict
        )
        self._runtime[escalation.id] = escalation

        store.insert_node(
            session,
            run,
            escalation.id,
            str(NodeKind.HUMAN),
            str(escalation.stage),
            _config(escalation),
        )
        recorder.request_approval(session, run, node_id=escalation.id, artifacts=[])
        store.finish_run(session, run, status=RunStatus.BLOCKED, stop_reason=response.reason)

    def _block_for_human(self, session: Session, run: Run, node: Node, execution) -> None:
        """A human node stops the run and records what is being asked.

        The approval binds to the artifact versions it covers, so a later
        re-derivation makes it stale rather than silently keeping it valid (D10).
        """
        bound = [
            artifact
            for ref in node.binds_to
            if (artifact := recorder.latest(session, run, _artifact_name(ref))) is not None
        ]
        recorder.request_approval(session, run, node_id=node.id, artifacts=bound)

        execution.status = NodeStatus.BLOCKED
        store.finish_run(
            session, run, status=RunStatus.BLOCKED, stop_reason=f"awaiting {node.id}"
        )

    # ----------------------------------------------------------------- #
    # re-checking without re-doing
    # ----------------------------------------------------------------- #

    def revalidate(
        self,
        session: Session,
        run: Run,
        node_id: str,
        *,
        reason: str | None = None,
        by: str | None = None,
    ) -> GateResult:
        """Re-run the checks that could not be performed, and re-decide the gate.

        The recovery an ERROR actually needs. `retry` re-enters the node and
        re-does the work, which is right for a FAIL — the work was wrong — and
        wrong for an ERROR, where the verdict says in as many words that the
        harness needs attention and the work does not. A twelve-minute code
        agent session should not be repeated because a plan omitted a param.

        **After an ERROR, only the ERRORED checks are re-evaluated.** The others
        were performed; their verdicts stand and are carried forward unchanged,
        so this cannot turn a "no" into a "yes".

        **After a FAIL it re-evaluates everything, and demands a justification.**
        A check that answered "no" is only worth asking again when the question
        changed — a verify command scoped to the wrong tree, a threshold read
        from the wrong profile. That is a real and recurring situation, and the
        alternative is worse: waiving past a gate that was simply wrong records
        a human accepting a defect that never existed.

        The safeguard is not refusal, it is the record. `reason` and `by` are
        required, both land in the gate record's evaluator, and the superseded
        verdict keeps its own attempt — so the evidence bundle shows attempt 5
        FAIL and attempt 6 PASS with the argument between them, rather than one
        tidy green.
        """
        execution = store.get_node(session, run, node_id)
        node = self._node(node_id)
        if execution is None or node is None:
            raise SchedulerError(f"run has no node '{node_id}'")
        if execution.status not in (NodeStatus.ERRORED, NodeStatus.FAILED):
            raise SchedulerError(
                f"'{node_id}' is {execution.status}, not errored or failed. There is no "
                f"verdict to re-check"
            )
        if execution.status is NodeStatus.FAILED and not (reason and by):
            raise SchedulerError(
                f"'{node_id}' FAILED — its checks were performed and answered. Re-checking "
                f"that needs --by and --why on the record, naming what changed about the "
                f"question. Without one, redo the work with `retry`"
            )

        previous = _latest_gate(execution)
        if previous is None or node.gate is None:
            raise SchedulerError(f"'{node_id}' has no recorded gate to re-evaluate")

        inputs = self._inputs(session, run, node)
        facts = self._verify(node, inputs)

        if execution.status is NodeStatus.FAILED:
            # Every check the re-run checks can actually speak to. The worker is
            # not re-run, so its facts — `session.files_written`, an exit code
            # from the work itself — are gone; a check reading one of those would
            # ERROR on a technicality and destroy the verdict it already had.
            reconsider = [c for c in node.gate.checks if _answerable(c, facts)]
            evaluator = f"orchestrator.gates (re-checked by {by}: {reason})"
        else:
            unperformed = {c["check"] for c in previous.checks if c["verdict"] == "error"}
            reconsider = [c for c in node.gate.checks if str(c) in unperformed]
            evaluator = "orchestrator.gates (re-checked)"
            if not reconsider:
                raise SchedulerError(f"'{node_id}' recorded no unperformed checks")
        fresh = evaluate_gate(
            Gate(all_checks=reconsider),
            facts,
            registry=self.registry,
            context=self._context(session, run, node),
        )

        merged = _merge(previous.checks, fresh.checks)
        verdict = (
            Verdict.ERROR
            if any(c.verdict is Verdict.ERROR for c in merged)
            else Verdict.FAIL
            if any(c.verdict is Verdict.FAIL for c in merged)
            else Verdict.PASS
        )

        attempt = store.begin_attempt(session, execution, worker="engine")
        store.record_gate(
            session,
            attempt,
            verdict=str(verdict),
            evaluator=evaluator,
            checks=[
                {
                    "check": check.check,
                    "verdict": str(check.verdict),
                    "detail": check.detail,
                    "observed": check.observed,
                }
                for check in merged
            ],
        )
        store.finish_attempt(session, attempt, status=_status_for(verdict))
        self._apply_policy(session, run, node, execution, verdict, attempt.number, facts)

        if verdict is Verdict.PASS:
            self._retire_escalations(session, run, node_id, evaluator)
        session.flush()

        return GateResult(verdict=verdict, checks=merged, evaluator=evaluator)

    def _retire_escalations(
        self, session: Session, run: Run, node_id: str, why: str
    ) -> None:
        """Close the handoffs this node's failures opened, once it passes.

        An escalation asks a person to decide about a verdict. When that verdict
        is superseded the question is moot, and leaving it pending would block
        release on a decision nobody can meaningfully make — G10 counts open
        approvals, and the operator would be approving a state that no longer
        exists.
        """
        for execution in store.all_nodes(session, run):
            if not execution.node_id.startswith(f"escalate:{node_id}#"):
                continue
            execution.status = NodeStatus.SKIPPED
            for approval in run.approvals:
                if approval.node_id == execution.node_id and approval.decision is Decision.PENDING:
                    recorder.decide(
                        session,
                        approval,
                        decision=Decision.REJECTED,
                        decided_by="engine",
                        note=f"moot: {node_id} passed on re-check. {why}",
                    )

    # ----------------------------------------------------------------- #
    # invalidation
    # ----------------------------------------------------------------- #

    def _invalidate_downstream(self, session: Session, run: Run, node: Node) -> None:
        """Mark transitively dependent work stale when an artifact is re-derived.

        Only fires from the *second* version onward: the first production of an
        artifact is the graph running forwards, not a change to something
        downstream already consumed.
        """
        if node.id not in self._graph:
            return

        re_derived = any(
            recorder.latest_version_number(session, run, name) > 1 for name in node.outputs
        )
        if not re_derived:
            return

        for descendant in nx.descendants(self._graph, node.id):
            execution = store.get_node(session, run, descendant)
            if execution is not None and execution.status is NodeStatus.PASSED:
                execution.status = NodeStatus.STALE
        session.flush()

    # ----------------------------------------------------------------- #
    # helpers
    # ----------------------------------------------------------------- #

    def rehydrate(self, session: Session, run: Run) -> None:
        """Rebuild in-memory state for a run that a previous process started.

        Without this, `orchestrator resume` would see an inserted node in the
        database and have no definition to dispatch it with — making safe-stop
        resumable only for runs that never inserted anything.
        """
        for execution in store.all_nodes(session, run):
            if execution.config:
                self._runtime[execution.node_id] = Node.model_validate(execution.config)
            if execution.extra_needs:
                self._extra_needs[execution.node_id] = list(execution.extra_needs)
            if execution.kind == NodeKind.FANOUT and execution.extra_needs:
                self._materialised.add(execution.node_id)

    def _persist_edges(self, session: Session, run: Run, node_id: str) -> None:
        execution = store.get_node(session, run, node_id)
        if execution is not None:
            execution.extra_needs = list(self._extra_needs.get(node_id, []))
            session.flush()

    def _node(self, node_id: str) -> Node | None:
        try:
            return self.plan.node(node_id)
        except KeyError:
            return self._runtime.get(node_id)

    def _settle(self, session: Session, run: Run) -> Run:
        if run.status is not RunStatus.RUNNING:
            return run

        unfinished = store.nodes_in_nonterminal_state(session, run)
        if not unfinished:
            store.finish_run(session, run, status=RunStatus.COMPLETED)
        else:
            store.finish_run(
                session,
                run,
                status=RunStatus.FAILED,
                stop_reason=f"no node can advance; stuck: {[n.node_id for n in unfinished]}",
            )
        return run


def _answerable(check: GateCheck, facts: FactSet) -> bool:
    """Whether re-running the node's checks can speak to this one.

    A predicate reads run state, so it always can. An expression can only be
    re-decided if the re-run produced the fact it names — otherwise the check
    was answered by the worker, which has not run again.
    """
    if not isinstance(check, ExpressionCheck):
        return True
    try:
        path, _, _ = expressions.parse(check.expression)
    except expressions.ExpressionError:
        return False
    return path in facts


def _latest_gate(execution):
    """The last exit-gate record this node produced, across all its attempts."""
    records = [
        record
        for attempt in execution.attempts
        for record in attempt.gate_records
        if record.gate == "exit"
    ]
    return records[-1] if records else None


def _merge(previous: list[dict], fresh: list[CheckResult]) -> list[CheckResult]:
    """Fresh verdicts for the checks that were re-run; recorded ones for the rest.

    Order follows the original record, so the re-checked gate reads as the same
    gate rather than a different one that happens to share checks.
    """
    replacement = {check.check: check for check in fresh}
    return [
        replacement.get(
            recorded["check"],
            CheckResult(
                check=recorded["check"],
                verdict=Verdict(recorded["verdict"]),
                detail=recorded["detail"],
                observed=recorded.get("observed"),
            ),
        )
        for recorded in previous
    ]


def _status_for(verdict: Verdict) -> NodeStatus:
    return {
        Verdict.PASS: NodeStatus.PASSED,
        Verdict.FAIL: NodeStatus.FAILED,
        Verdict.ERROR: NodeStatus.ERRORED,
    }[verdict]


def verify_probes(node: Node) -> list[Node]:
    """The nodes a node's declared checks would run as.

    Shared with `--dry-run`, so the commands it prints are the ones that would
    actually execute — a preview built from a second code path is a preview of
    something else.
    """
    return [
        Node(
            id=f"{node.id}#verify{index}",
            kind=NodeKind.TOOL,
            stage=node.stage,
            run=check,
            params=node.params,
        )
        for index, check in enumerate(node.verify)
    ]


def _config(node: Node) -> dict:
    """Serialise an inserted node so a later process can dispatch it."""
    return node.model_dump(mode="json", by_alias=True, exclude_none=True)


def _substitute(pattern: str, item: dict) -> str:
    """`target/shortener/{item.path}/**` → `target/shortener/api/**`.

    Deliberately narrow: only `{item.key}` is substituted, so a plan cannot
    smuggle arbitrary formatting into a write scope — the one field where a
    surprise would defeat blast-radius containment (D7).
    """
    for key, value in item.items():
        pattern = pattern.replace(f"{{item.{key}}}", str(value))
    return pattern


def _decision_text(session: Session, run: Run, node_id: str) -> str | None:
    """What a person actually said at a checkpoint.

    Without this a clarification checkpoint changes nothing: the run stops, a
    human answers, and the answer stays in the audit trail while the node that
    asked the question is handed the same material it had before. The decision
    is context, and §4.4 of the brief asks for context to cross stages.
    """
    decided = [
        approval
        for approval in run.approvals
        if approval.node_id == node_id and approval.decision is not Decision.PENDING
    ]
    if not decided:
        return None

    approval = decided[-1]
    lines = [f"decision: {approval.decision}", f"by: {approval.decided_by}"]
    if approval.note:
        lines.append(f"note:\n{approval.note}")
    return "\n".join(lines)


def _artifact_name(ref: str) -> str:
    """`design.artifacts.openapi` → `design.openapi`.

    The plan addresses artifacts through the node that produced them; lineage
    stores them by name.
    """
    return ref.replace(".artifacts.", ".", 1)


def parse_fanout_items(content: str, ref: str = "<artifact>") -> list[dict]:
    """Read the item list a fanout materialises children from.

    Takes content rather than an `Artifact` because the database stores a hash
    and a path, not the body — keeping run state small and greppable means the
    content is resolved from disk by the caller.

    Strict on purpose: a fanout whose source is malformed must fail loudly. The
    quiet alternative is zero children and a graph that proceeds as though
    implementation were complete.
    """
    try:
        items = json.loads(content or "[]")
    except json.JSONDecodeError as exc:
        raise SchedulerError(f"fanout source '{ref}' is not valid JSON: {exc}") from exc

    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise SchedulerError(
            f"fanout source '{ref}' must be a list of objects, got {type(items).__name__}"
        )
    return items
