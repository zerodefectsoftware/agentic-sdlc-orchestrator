"""Scheduler tests.

Every one of these runs against `StubWorker`, so the loop, the gates, the
policy, and the invalidation cascade are all exercised without a model call.
That is the payoff D18 was designed for — these complete in milliseconds and
give the same answer every time.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from orchestrator.engine.loader import load_plan
from orchestrator.engine.plan import Stage
from orchestrator.engine.scheduler import Scheduler, SchedulerError, parse_fanout_items
from orchestrator.gates import Verdict, tool_facts
from orchestrator.gates.registry import PredicateRegistry
from orchestrator.lineage import query, recorder
from orchestrator.state import store
from orchestrator.state.artifacts import ArtifactStore
from orchestrator.state.models import Decision, NodeStatus, RunStatus
from orchestrator.workers import StubWorker, WorkerError, WorkerResult, WorkScope
from orchestrator.workers import stub as scripts

LINEAR = """
plan: t
version: 1
nodes:
  - id: build
    kind: tool
    stage: implementation
    run: sh:make
    gate: {all: ["make.exit_code == 0"]}
  - id: verify
    kind: tool
    stage: verification
    needs: [build]
    run: sh:pytest
    gate: {all: ["pytest.exit_code == 0"]}
"""

PARALLEL = """
plan: t
version: 1
nodes:
  - id: build
    kind: tool
    stage: implementation
    run: sh:make
  - id: tests
    kind: tool
    stage: verification
    needs: [build]
    run: sh:pytest
  - id: docs
    kind: tool
    stage: documentation
    needs: [build]
    run: sh:mkdocs
  - id: security
    kind: tool
    stage: verification
    needs: [build]
    run: sh:scan
  - id: release
    kind: derive
    stage: release
    needs: [tests, docs, security]
    run: py:orchestrator.evidence.assemble
"""


@pytest.fixture
def db():
    return store.Store.in_memory()


@pytest.fixture
def session(db):
    with db.session() as session:
        yield session


def plan_from(tmp_path: Path, body: str):
    path = tmp_path / "plan.yaml"
    path.write_text(textwrap.dedent(body))
    return load_plan(path)


def run_plan(session, plan, worker, **kwargs) -> tuple:
    scheduler = Scheduler(plan, worker, **kwargs)
    run = scheduler.start(
        session, requirement_path="requirements/x.md", target_profile="config/t.yaml"
    )
    return scheduler, scheduler.advance(session, run)


def statuses(session, run) -> dict[str, NodeStatus]:
    return {n.node_id: n.status for n in store.all_nodes(session, run)}


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


def test_a_clean_run_completes(session, tmp_path):
    worker = StubWorker(
        {"build": scripts.passing("make"), "verify": scripts.passing("pytest")}
    )
    _, run = run_plan(session, plan_from(tmp_path, LINEAR), worker)

    assert run.status is RunStatus.COMPLETED
    assert statuses(session, run) == {
        "build": NodeStatus.PASSED,
        "verify": NodeStatus.PASSED,
    }


def test_dependencies_are_respected(session, tmp_path):
    worker = StubWorker(default=scripts.passing("make"))
    run_plan(session, plan_from(tmp_path, LINEAR), worker)
    assert worker.calls == ["build", "verify"]


def test_parallel_branches_run_in_one_wave(session, tmp_path):
    """The join is the wave boundary, not a node — there is no `join` kind."""
    worker = StubWorker(default=scripts.passing("make"))
    _, run = run_plan(session, plan_from(tmp_path, PARALLEL), worker)

    assert run.status is RunStatus.COMPLETED
    assert worker.calls[0] == "build"
    assert set(worker.calls[1:4]) == {"tests", "docs", "security"}
    assert worker.calls[4] == "release"


def test_a_node_without_a_gate_passes_on_completion(session, tmp_path):
    worker = StubWorker(default=scripts.unevaluable())
    _, run = run_plan(session, plan_from(tmp_path, PARALLEL), worker)
    assert run.status is RunStatus.COMPLETED


# --------------------------------------------------------------------------- #
# failure, retry, escalation
# --------------------------------------------------------------------------- #

RETRYING = """
plan: t
version: 1
nodes:
  - id: verify
    kind: tool
    stage: verification
    run: sh:pytest
    retry_budget: 2
    gate: {all: ["pytest.exit_code == 0"]}
"""


def test_a_failing_gate_retries_within_budget(session, tmp_path):
    worker = StubWorker({"verify": [scripts.failing(), scripts.passing()]})
    _, run = run_plan(session, plan_from(tmp_path, RETRYING), worker)

    assert run.status is RunStatus.COMPLETED
    assert worker.calls == ["verify", "verify"]
    assert store.get_node(session, run, "verify").attempt_count == 2


def test_exhausted_retries_insert_an_escalation_node(session, tmp_path):
    """§6: escalation is a node, not a status."""
    worker = StubWorker(default=scripts.failing())
    _, run = run_plan(session, plan_from(tmp_path, RETRYING), worker)

    assert run.status is RunStatus.BLOCKED
    escalations = [
        n.node_id for n in store.all_nodes(session, run) if n.node_id.startswith("escalate:")
    ]
    assert escalations == ["escalate:verify#3"]
    assert store.get_node(session, run, escalations[0]).inserted


def test_an_error_escalates_without_consuming_the_retry_budget(session, tmp_path):
    """A missing fact is exactly as missing on attempt two."""
    worker = StubWorker(default=scripts.unevaluable())
    _, run = run_plan(session, plan_from(tmp_path, RETRYING), worker)

    assert run.status is RunStatus.BLOCKED
    assert worker.calls == ["verify"]  # not retried
    assert store.get_node(session, run, "verify").status is NodeStatus.ERRORED


def test_a_worker_crash_is_an_error_not_a_failure(session, tmp_path):
    worker = StubWorker({"verify": WorkerError("sandbox died")})
    _, run = run_plan(session, plan_from(tmp_path, RETRYING), worker)

    assert store.get_node(session, run, "verify").status is NodeStatus.ERRORED
    assert worker.calls == ["verify"]


def test_an_agent_self_report_cannot_satisfy_a_gate(session, tmp_path):
    """D4 through the whole stack: worker → facts → gate → status."""
    body = RETRYING.replace('"pytest.exit_code == 0"', '"impl.complete == true"')
    worker = StubWorker(default=scripts.self_reported())
    _, run = run_plan(session, plan_from(tmp_path, body), worker)

    assert store.get_node(session, run, "verify").status is NodeStatus.ERRORED


REPAIRING = """
plan: t
version: 1
nodes:
  - id: verify
    kind: tool
    stage: verification
    run: sh:pytest
    write_scope: ["target/shortener/**"]
    freeze_paths: ["target/tests/**"]
    gate: {all: ["pytest.exit_code == 0"]}
    on_fail: {insert: fix, max_attempts: 2}
"""


def test_a_failing_gate_inserts_a_fix_node(session, tmp_path):
    worker = StubWorker(
        {"verify": [scripts.failing(), scripts.passing()], "fix:verify": scripts.passing()}
    )
    scheduler, run = run_plan(session, plan_from(tmp_path, REPAIRING), worker)

    assert run.status is RunStatus.COMPLETED
    assert "fix:verify" in worker.calls
    assert store.get_node(session, run, "fix:verify").inserted


def test_the_fix_node_cannot_touch_the_tests_that_judge_it(session, tmp_path):
    """D6: the cheapest route to a green suite is a weakened test."""
    worker = StubWorker(
        {"verify": [scripts.failing(), scripts.passing()], "fix:verify": scripts.passing()}
    )
    scheduler, _ = run_plan(session, plan_from(tmp_path, REPAIRING), worker)

    fix = scheduler._runtime["fix:verify"]
    assert fix.freeze_paths == ["target/tests/**"]


# --------------------------------------------------------------------------- #
# human checkpoints
# --------------------------------------------------------------------------- #

APPROVING = """
plan: t
version: 1
nodes:
  - id: design
    kind: agent
    stage: design
    role: architect
    outputs: [openapi]
  - id: design-approval
    kind: human
    stage: design
    needs: [design]
    autonomy: APPROVE
    binds_to: [design.artifacts.openapi]
  - id: build
    kind: tool
    stage: implementation
    needs: [design-approval]
    run: sh:make
"""


def test_a_human_node_blocks_the_run(session, tmp_path):
    """It stops rather than waits — a blocked run holding a process open for
    three hours is a hostage situation, not a checkpoint."""
    worker = StubWorker(default=scripts.passing("make", **{"design.openapi": "paths: {}"}))
    _, run = run_plan(session, plan_from(tmp_path, APPROVING), worker)

    assert run.status is RunStatus.BLOCKED
    assert store.get_node(session, run, "design-approval").status is NodeStatus.BLOCKED
    assert "build" not in worker.calls


def test_the_approval_binds_to_the_artifact_version_it_covers(session, tmp_path):
    """D10 — and the reason a later re-derivation makes it stale."""
    worker = StubWorker(default=scripts.passing("make", **{"design.openapi": "paths: {}"}))
    _, run = run_plan(session, plan_from(tmp_path, APPROVING), worker)

    approval = run.approvals[0]
    assert approval.node_id == "design-approval"
    assert [b.artifact.ref for b in approval.bindings] == ["design.openapi@v1"]

    recorder.decide(session, approval, decision=Decision.APPROVED, decided_by="alice")
    recorder.record_artifact(session, run, name="design.openapi", content="paths: {/x}")
    assert len(query.stale_approvals(session, run)) == 1


# --------------------------------------------------------------------------- #
# the ambiguity path
# --------------------------------------------------------------------------- #

ESCALATING = """
plan: t
version: 1
nodes:
  - id: intake
    kind: agent
    stage: requirements
    role: analyst
    escalate_when:
      predicate: has_high_severity_ambiguity
    on_escalate: clarify
  - id: clarify
    kind: human
    stage: requirements
    optional: true
    autonomy: APPROVE
"""


def test_an_optional_node_stays_dormant_until_something_escalates(session, tmp_path):
    """A PENDING optional node with no dependencies would be ready immediately,
    and every run would stop to ask a question nobody raised."""
    registry = PredicateRegistry()
    registry.register("has_high_severity_ambiguity", "…")(lambda facts: (False, "none"))

    worker = StubWorker(default=scripts.passing())
    plan = plan_from(tmp_path, ESCALATING)
    scheduler = Scheduler(plan, worker, registry=registry)
    run = scheduler.advance(
        session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
    )

    assert run.status is RunStatus.COMPLETED
    assert statuses(session, run)["clarify"] is NodeStatus.SKIPPED


def test_escalate_when_activates_the_declared_node(session, tmp_path):
    """Distinct from failure escalation: this wakes a node the plan declared."""
    registry = PredicateRegistry()
    registry.register("has_high_severity_ambiguity", "…")(lambda facts: (True, "A1 is HIGH"))

    worker = StubWorker(default=scripts.passing())
    plan = plan_from(tmp_path, ESCALATING)
    scheduler = Scheduler(plan, worker, registry=registry)
    run = scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")

    # intake has no gate, so it passes; the escalation condition then fires.
    scheduler.advance(session, run)
    assert statuses(session, run)["clarify"] is not NodeStatus.SKIPPED


# --------------------------------------------------------------------------- #
# re-checking without re-doing
# --------------------------------------------------------------------------- #

UNCHECKABLE = """
plan: t
version: 1
nodes:
  - id: build
    kind: tool
    stage: implementation
    run: sh:make
    verify:
      - sh:coverage
    gate:
      all:
        - "make.exit_code == 0"
        - "coverage.percent >= 80"
"""


def observed(percent: float) -> WorkerResult:
    """What the check would report once the harness can run it."""
    return WorkerResult(facts=tool_facts("coverage", **{"coverage.percent": percent}))


def errored_build(session, tmp_path, worker):
    """A run whose gate could not be evaluated: the check produced no fact."""
    scheduler, run = run_plan(session, plan_from(tmp_path, UNCHECKABLE), worker)
    assert statuses(session, run)["build"] is NodeStatus.ERRORED
    return scheduler, run


def test_recheck_answers_what_could_not_be_checked_without_redoing_the_work(
    session, tmp_path
):
    """The recovery an ERROR needs.

    `retry` redoes the work, which is right for a FAIL and wrong here: an ERROR
    says in as many words that the harness needs attention and the work does
    not. A twelve-minute code agent session should not be repeated because a
    plan omitted a param.
    """
    worker = StubWorker(
        {"build": scripts.passing("make"), "build#verify0": scripts.unevaluable()}
    )
    scheduler, run = errored_build(session, tmp_path, worker)

    scheduler.worker = StubWorker(
        {"build#verify0": observed(91)}
    )
    result = scheduler.revalidate(session, run, "build")

    assert result.verdict is Verdict.PASS
    assert statuses(session, run)["build"] is NodeStatus.PASSED
    assert worker.calls == ["build", "build#verify0"]   # the work ran exactly once


def test_recheck_carries_forward_the_verdicts_that_were_reached(session, tmp_path):
    """Only the ERRORED checks are re-evaluated; the others were performed.

    So this cannot manufacture green: a check that answered "no" keeps its
    answer, and only "could not tell" is asked again.
    """
    worker = StubWorker(
        {"build": scripts.passing("make"), "build#verify0": scripts.unevaluable()}
    )
    scheduler, run = errored_build(session, tmp_path, worker)

    scheduler.worker = StubWorker(
        {"build#verify0": observed(12)}
    )
    result = scheduler.revalidate(session, run, "build")

    assert result.verdict is Verdict.FAIL
    assert [str(check.verdict) for check in result.checks] == ["pass", "fail"]


def test_recheck_refuses_a_node_that_failed(session, tmp_path):
    """It can turn "could not tell" into an answer, never "no" into "yes"."""
    worker = StubWorker({"build": scripts.failing("make")})
    scheduler, run = run_plan(session, plan_from(tmp_path, LINEAR), worker)
    assert statuses(session, run)["build"] is NodeStatus.FAILED

    with pytest.raises(SchedulerError, match="failed work has to be redone"):
        scheduler.revalidate(session, run, "build")


def test_recheck_that_still_cannot_check_stays_an_error(session, tmp_path):
    """Fixing nothing changes nothing — and must not read as progress."""
    worker = StubWorker(
        {"build": scripts.passing("make"), "build#verify0": scripts.unevaluable()}
    )
    scheduler, run = errored_build(session, tmp_path, worker)

    result = scheduler.revalidate(session, run, "build")
    assert result.verdict is Verdict.ERROR
    assert statuses(session, run)["build"] is NodeStatus.ERRORED


# --------------------------------------------------------------------------- #
# invalidation
# --------------------------------------------------------------------------- #


def test_re_deriving_an_artifact_marks_descendants_stale(session, tmp_path):
    """§6 re-planning. The first version is the graph running forwards; the
    second is a change to something downstream already consumed."""
    worker = StubWorker(
        {"build": scripts.passing("make"), "verify": scripts.passing("pytest")}
    )
    scheduler, run = run_plan(session, plan_from(tmp_path, LINEAR), worker)
    assert statuses(session, run)["verify"] is NodeStatus.PASSED

    build = scheduler.plan.node("build")
    object.__setattr__(build, "outputs", ["design.openapi"])
    recorder.record_artifact(session, run, name="design.openapi", content="v1")
    recorder.record_artifact(session, run, name="design.openapi", content="v2")

    scheduler._invalidate_downstream(session, run, build)
    assert statuses(session, run)["verify"] is NodeStatus.STALE


def test_a_stale_node_re_enters_the_graph(session, tmp_path):
    """The cascade is only half of re-planning; this is the other half.

    Marking downstream STALE and never collecting it again is a one-way door:
    the node never runs, and `_settle` fails the run as stuck. STALE says the
    recorded result was computed from something withdrawn — which is a reason to
    do the work again, not a terminal state.
    """
    worker = StubWorker(
        {"build": scripts.passing("make"), "verify": scripts.passing("pytest")}
    )
    scheduler, run = run_plan(session, plan_from(tmp_path, LINEAR), worker)

    store.get_node(session, run, "verify").status = NodeStatus.STALE
    session.flush()

    run.status = RunStatus.RUNNING
    scheduler.advance(session, run)

    assert statuses(session, run)["verify"] is NodeStatus.PASSED
    assert run.status is RunStatus.COMPLETED


# --------------------------------------------------------------------------- #
# fanout
# --------------------------------------------------------------------------- #

FANOUT = """
plan: t
version: 1
nodes:
  - id: design
    kind: agent
    stage: design
    role: architect
    outputs: [modules]
  - id: impl
    kind: fanout
    stage: implementation
    needs: [design]
    from: design.artifacts.modules
    template:
      kind: codeagent
      role: implementer
      write_scope: ["target/shortener/{item.path}/**"]
  - id: tests
    kind: tool
    stage: verification
    needs: [impl]
    run: sh:pytest
"""

MODULES = '[{"name": "api", "path": "api"}, {"name": "storage", "path": "storage"}]'


@pytest.fixture
def fanout_worker():
    return StubWorker(
        {"design": scripts.passing("agent", **{"design.modules": MODULES})},
        default=scripts.passing("pytest"),
    )


def fanout_scheduler(plan, worker, tmp_path):
    return Scheduler(plan, worker, artifacts=ArtifactStore(tmp_path / "runs"))


def test_fanout_materialises_one_child_per_item(session, tmp_path, fanout_worker):
    """§5.1: the graph's shape depends on an upstream agent's output."""
    plan = plan_from(tmp_path, FANOUT)
    scheduler = fanout_scheduler(plan, fanout_worker, tmp_path)
    run = scheduler.advance(
        session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
    )

    assert run.status is RunStatus.COMPLETED
    children = [n.node_id for n in store.all_nodes(session, run) if n.node_id.startswith("impl:")]
    assert children == ["impl:api", "impl:storage"]
    assert all(store.get_node(session, run, child).inserted for child in children)


def test_verification_waits_for_every_module(session, tmp_path, fanout_worker):
    """If the fanout completed the moment it materialised, verification would
    start before a single module had been written."""
    plan = plan_from(tmp_path, FANOUT)
    scheduler = fanout_scheduler(plan, fanout_worker, tmp_path)
    scheduler.advance(
        session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
    )

    calls = fanout_worker.calls
    assert calls.index("impl:api") < calls.index("tests")
    assert calls.index("impl:storage") < calls.index("tests")


def test_children_run_in_the_same_wave(session, tmp_path, fanout_worker):
    plan = plan_from(tmp_path, FANOUT)
    scheduler = fanout_scheduler(plan, fanout_worker, tmp_path)
    scheduler.advance(
        session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
    )
    calls = fanout_worker.calls
    assert set(calls[1:3]) == {"impl:api", "impl:storage"}


def test_each_child_is_scoped_to_its_own_module(session, tmp_path, fanout_worker):
    """D7: a module that could write its neighbour's directory has no blast radius."""
    plan = plan_from(tmp_path, FANOUT)
    scheduler = fanout_scheduler(plan, fanout_worker, tmp_path)
    scheduler.advance(
        session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
    )

    api = scheduler._runtime["impl:api"]
    storage = scheduler._runtime["impl:storage"]
    assert api.write_scope == ["target/shortener/api/**"]
    assert storage.write_scope == ["target/shortener/storage/**"]
    assert not WorkScope.for_node(api).permits("target/shortener/storage/db.py")


def test_a_child_carries_the_template_session_limits(session, tmp_path, fanout_worker):
    """A per-module budget is only settable if the template can carry one.

    Without it every child falls back to a default tuned for nothing in
    particular — and a ceiling sized for one module is the wrong ceiling for a
    node that used to write seven.
    """
    plan = plan_from(tmp_path, FANOUT.replace(
        '      write_scope: ["target/shortener/{item.path}/**"]',
        '      write_scope: ["target/shortener/{item.path}/**"]\n'
        "      params:\n        max_turns: 60\n        timeout_s: 1800",
    ))
    scheduler = fanout_scheduler(plan, fanout_worker, tmp_path)
    scheduler.advance(
        session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
    )

    api = scheduler._runtime["impl:api"]
    assert api.params["max_turns"] == 60
    assert api.params["timeout_s"] == 1800


def test_children_inherit_the_stage_of_the_fanout(session, tmp_path, fanout_worker):
    plan = plan_from(tmp_path, FANOUT)
    scheduler = fanout_scheduler(plan, fanout_worker, tmp_path)
    scheduler.advance(
        session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
    )
    assert scheduler._runtime["impl:api"].stage is Stage.IMPLEMENTATION


def test_a_fanout_whose_source_was_never_produced_fails_loudly(session, tmp_path):
    worker = StubWorker(default=scripts.passing("agent"))  # design produces nothing
    plan = plan_from(tmp_path, FANOUT)
    scheduler = fanout_scheduler(plan, worker, tmp_path)

    with pytest.raises(SchedulerError, match="which no node has produced"):
        scheduler.advance(
            session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
        )


def test_artifact_bodies_are_written_where_they_can_be_read(session, tmp_path, fanout_worker):
    """A reviewer should be able to `cat` an artifact, not query for a hash."""
    plan = plan_from(tmp_path, FANOUT)
    scheduler = fanout_scheduler(plan, fanout_worker, tmp_path)
    run = scheduler.advance(
        session, scheduler.start(session, requirement_path="r.md", target_profile="t.yaml")
    )

    modules = recorder.latest(session, run, "design.modules")
    assert Path(modules.path).read_text() == MODULES
    assert Path(modules.path).name == "v1"


def test_a_missing_artifact_body_is_an_error(session, tmp_path):
    """Silently reading '' would produce a fanout with zero children."""
    artifacts = ArtifactStore(tmp_path / "runs")
    run = store.start_run(
        session,
        plan_name="t",
        plan_version=1,
        requirement_path="r.md",
        target_profile="t.yaml",
        nodes=[("a", "tool", "verification")],
    )
    orphan = recorder.record_artifact(session, run, name="ghost", content="x")
    with pytest.raises(FileNotFoundError, match="body is missing"):
        artifacts.read(orphan)


# --------------------------------------------------------------------------- #
# fanout source parsing
# --------------------------------------------------------------------------- #


def test_fanout_items_parse():
    items = parse_fanout_items('[{"name": "api", "path": "api"}]')
    assert items[0]["name"] == "api"


@pytest.mark.parametrize("content", ["not json", '{"name": "api"}', "[1, 2]"])
def test_a_malformed_fanout_source_fails_loudly(content):
    """Zero children would let the graph proceed as though implementation were done."""
    with pytest.raises(SchedulerError):
        parse_fanout_items(content, "design.modules@v1")


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #


def test_gate_verdicts_are_recorded_with_their_checks(session, tmp_path):
    worker = StubWorker(default=scripts.failing())
    _, run = run_plan(session, plan_from(tmp_path, RETRYING), worker)

    attempt = store.get_node(session, run, "verify").attempts[0]
    record = attempt.gate_records[0]
    assert record.verdict == "fail"
    assert record.checks[0]["check"] == "pytest.exit_code == 0"
    assert "1" in record.checks[0]["observed"]


def test_artifacts_are_recorded_against_the_attempt_that_produced_them(session, tmp_path):
    worker = StubWorker(default=scripts.passing("make", report="coverage: 91%"))
    _, run = run_plan(session, plan_from(tmp_path, LINEAR), worker)

    report = recorder.latest(session, run, "report")
    assert report is not None
    assert query.provenance(session, report).produced_by_node in {"build", "verify"}


def test_the_worker_name_is_recorded_on_every_attempt(session, tmp_path):
    """An evidence bundle that cannot tell live from stubbed is not evidence."""
    worker = StubWorker(
        {"build": scripts.passing("make"), "verify": scripts.passing("pytest")}
    )
    _, run = run_plan(session, plan_from(tmp_path, LINEAR), worker)
    attempts = [a for n in store.all_nodes(session, run) for a in n.attempts]
    assert {a.worker for a in attempts} == {"stub"}


# --------------------------------------------------------------------------- #
# verify — where a gate's facts come from
# --------------------------------------------------------------------------- #

VERIFIED = """
plan: t
version: 1
nodes:
  - id: scaffold
    kind: derive
    stage: implementation
    run: py:orchestrator.derive.scaffold_from_design
    verify:
      - sh:ruff
    gate: {all: ["ruff.exit_code == 0"]}
"""


def test_a_gate_reads_what_the_check_observed_not_what_the_node_claimed(session, tmp_path):
    """D4, operationally.

    `ruff.exit_code` is not something the node that wrote the code can report.
    The plan names the check, the engine runs it, and the gate reads that.
    """
    worker = StubWorker(
        {
            "scaffold": scripts.self_reported("ruff.exit_code"),   # the node says it is fine
            "scaffold#verify0": scripts.failing("ruff"),           # the tool says otherwise
        }
    )
    _, run = run_plan(session, plan_from(tmp_path, VERIFIED), worker)

    assert statuses(session, run)["scaffold"] is NodeStatus.FAILED
    assert worker.calls[:2] == ["scaffold", "scaffold#verify0"]   # work, then check


def test_a_passing_check_lets_the_gate_through(session, tmp_path):
    worker = StubWorker(
        {"scaffold": scripts.unevaluable(), "scaffold#verify0": scripts.passing("ruff")}
    )
    _, run = run_plan(session, plan_from(tmp_path, VERIFIED), worker)

    assert run.status is RunStatus.COMPLETED
    assert statuses(session, run)["scaffold"] is NodeStatus.PASSED


def test_a_check_that_cannot_run_errors_rather_than_failing(session, tmp_path):
    """The distinction the whole verdict vocabulary exists for: a check that did
    not happen is not a check that said no, and must not enter the repair loop."""
    worker = StubWorker(
        {
            "scaffold": scripts.unevaluable(),
            "scaffold#verify0": WorkerError("command not found: ruff"),
        }
    )
    _, run = run_plan(session, plan_from(tmp_path, VERIFIED), worker)

    assert statuses(session, run)["scaffold"] is NodeStatus.ERRORED


FANNED_OUT_CHECKS = """
plan: t
version: 1
nodes:
  - id: design
    kind: agent
    stage: design
    role: architect
    outputs: [modules]
  - id: impl
    kind: fanout
    stage: implementation
    needs: [design]
    from: design.artifacts.modules
    template:
      kind: codeagent
      role: implementer
      write_scope: ["target/{item.path}/**"]
      verify:
        - "sh:ruff check target/{item.path}"
      gate: {all: ["ruff.exit_code == 0"]}
"""


def test_each_fanout_child_checks_only_its_own_module(session, tmp_path):
    """A child failing because a sibling's directory is dirty is not a signal."""
    modules = '[{"name": "api", "path": "api"}]'
    worker = StubWorker(
        {
            "design": scripts.passing("agent", **{"design.modules": modules}),
            "impl:api": scripts.unevaluable(),
            "impl:api#verify0": scripts.passing("ruff"),
        }
    )
    scheduler, run = run_plan(session, plan_from(tmp_path, FANNED_OUT_CHECKS), worker)

    child = scheduler._node("impl:api")
    assert child.verify == ["sh:ruff check target/api"]
    assert statuses(session, run)["impl:api"] is NodeStatus.PASSED


BLOCKING_WAVE = """
plan: t
version: 1
nodes:
  - id: seed
    kind: tool
    stage: implementation
    run: sh:make
  - id: alpha
    kind: tool
    stage: verification
    needs: [seed]
    run: sh:pytest
    gate: {all: ["pytest.exit_code == 0"]}
  - id: beta
    kind: tool
    stage: verification
    needs: [seed]
    run: sh:ruff
    gate: {all: ["ruff.exit_code == 0"]}
"""


def test_every_outcome_of_a_wave_is_recorded_even_after_one_blocks_the_run(session, tmp_path):
    """The wave has already run when recording starts.

    Five implementers once wrote real code into the target and had their
    attempts, changesets and gate verdicts discarded because a sibling escalated
    first — code in the tree with no lineage behind it, which is the one thing
    the evidence bundle may not contain.
    """
    worker = StubWorker(
        {
            "seed": scripts.passing("make"),
            "alpha": scripts.failing("pytest"),   # escalates: no retry budget, no on_fail
            "beta": scripts.passing("ruff"),
        }
    )
    _, run = run_plan(session, plan_from(tmp_path, BLOCKING_WAVE), worker)

    assert run.status is RunStatus.BLOCKED          # alpha escalated
    recorded = statuses(session, run)
    assert recorded["alpha"] is NodeStatus.FAILED
    assert recorded["beta"] is NodeStatus.PASSED    # not silently dropped

    beta = store.get_node(session, run, "beta")
    assert beta.attempts, "beta ran but no attempt was recorded"
