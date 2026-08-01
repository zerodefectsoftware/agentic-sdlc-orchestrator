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
from orchestrator.engine.plan import Gate, Node, NodeKind, Plan
from orchestrator.gates import GateResult, Verdict, evaluate_gate
from orchestrator.gates.facts import FactSet
from orchestrator.gates.registry import PredicateContext, PredicateRegistry
from orchestrator.gates.registry import registry as default_registry
from orchestrator.lineage import recorder
from orchestrator.policy.failure import Action, escalation_node, respond_to
from orchestrator.state import store
from orchestrator.state.artifacts import ArtifactStore
from orchestrator.state.models import Artifact, NodeStatus, Run, RunStatus
from orchestrator.workers.base import Worker, WorkerError, WorkerResult, WorkScope

TERMINAL = (NodeStatus.PASSED, NodeStatus.SKIPPED)


class SchedulerError(Exception):
    """The run cannot proceed for a structural reason, not a gate verdict."""


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
            for outcome in self._execute(wave, material):
                self._record(session, run, outcome)
                if run.status is not RunStatus.RUNNING:
                    break

        return self._settle(session, run)

    # ----------------------------------------------------------------- #
    # readiness and dispatch
    # ----------------------------------------------------------------- #

    def _ready(self, session: Session, run: Run) -> list[Node]:
        """Every node that can run now — the wave."""
        ready: list[Node] = []
        for execution in store.all_nodes(session, run):
            if execution.status is not NodeStatus.PENDING:
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
            names = upstream.outputs if upstream else []
            for output in names:
                artifact = recorder.latest(session, run, f"{upstream.id}.{output}")
                if artifact is not None:
                    material[artifact.name] = self.artifacts.read(artifact)
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
        return Outcome(node=node, result=result)

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
        """
        if node.id in self._materialised:
            execution.status = NodeStatus.PASSED
            session.flush()
            return

        for child in self._children_of(session, run, node):
            self._runtime[child.id] = child
            if store.get_node(session, run, child.id) is None:
                store.insert_node(
                    session, run, child.id, str(child.kind), str(child.stage), _config(child)
                )
            self._extra_needs.setdefault(node.id, []).append(child.id)
        self._persist_edges(session, run, node.id)

        self._materialised.add(node.id)
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
                freeze_paths=list(template.freeze_paths),
                gate=template.gate,
                model=template.model,
                effort=template.effort,
                retry_budget=template.retry_budget,
                autonomy=template.autonomy,
            )
            for index, item in enumerate(items)
        ]

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
                self._insert_escalation(session, run, node, response, attempt_number)
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

        target = store.get_node(session, run, node.on_escalate)
        if target is not None and target.status is NodeStatus.SKIPPED:
            target.status = NodeStatus.PENDING
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
        self, session: Session, run: Run, node: Node, response, attempt_number: int
    ) -> None:
        """Hand off to a human as a node, not a status (§6)."""
        escalation = escalation_node(node, response, attempt=attempt_number)
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


def _status_for(verdict: Verdict) -> NodeStatus:
    return {
        Verdict.PASS: NodeStatus.PASSED,
        Verdict.FAIL: NodeStatus.FAILED,
        Verdict.ERROR: NodeStatus.ERRORED,
    }[verdict]


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
